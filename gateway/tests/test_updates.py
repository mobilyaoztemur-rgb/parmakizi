import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gateway.updater import NAMES, UpdateClient, verify_manifest, public_key_fingerprint
from gateway.update_helper import apply_update, update_lock
from gateway.attendance_cache import AttendanceCache
from gateway.connector import Connector


def signing_fixture(content=None, version='0.2.0'):
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    content = content or {name: ('MZ-new-' + name).encode() for name in NAMES}
    files = []
    for name in NAMES:
        digest = hashlib.sha256(content[name]).hexdigest()
        files.append({'name': name, 'path': f'/updates/files/{name[:-4]}-{version}-{digest[:12]}.exe', 'sha256': digest, 'size': len(content[name])})
    data = {'version': version, 'notes': 'Test', 'publishedAt': '2026-10-30T00:00:00Z', 'files': files}
    def sign(data):
        payload = json.dumps(data, separators=(',', ':'))
        return {'payload': payload, 'signature': base64.b64encode(key.sign(payload.encode())).decode()}
    return public, content, data, sign


class ManifestTests(unittest.TestCase):
    def test_key_fingerprint_is_identical_on_windows_and_linux(self):
        public, _, _, _ = signing_fixture()
        self.assertEqual(public_key_fingerprint(public), public_key_fingerprint(public.replace(b'\n', b'\r\n')))

    def test_valid_signature_and_tampering(self):
        public, _, data, sign = signing_fixture()
        envelope = sign(data)
        self.assertEqual(verify_manifest(envelope, public)['version'], '0.2.0')
        envelope['payload'] = envelope['payload'].replace('0.2.0', '0.3.0')
        with self.assertRaises(ValueError):
            verify_manifest(envelope, public)

    def test_signed_cross_origin_and_path_traversal_still_rejected(self):
        for path in ('https://other.example/app.exe', '/updates/files/../../app.exe', '//other.example/app.exe'):
            public, _, data, sign = signing_fixture()
            data['files'][0]['path'] = path
            with self.assertRaises(ValueError):
                verify_manifest(sign(data), public)

    def test_older_or_current_release_not_offered(self):
        public, _, data, sign = signing_fixture()
        client = UpdateClient('https://example.com', public_key=public)
        with patch.object(client.client, 'request', return_value=sign(data)):
            self.assertIsNone(client.latest(current='0.2.0'))
            self.assertIsNone(client.latest(current='0.3.0'))
            self.assertIsNotNone(client.latest(current='0.1.0'))

    def test_incomplete_download_and_existing_partial_file_are_preserved_safely(self):
        public, content, data, sign = signing_fixture()
        client = UpdateClient('https://example.com', public_key=public)
        response = io.BytesIO(content[NAMES[0]][:-1])
        response.status = 200
        with tempfile.TemporaryDirectory() as folder:
            client.opener = Mock()
            client.opener.open.return_value = response
            with self.assertRaises(ValueError):
                client.download(sign(data), folder)
            self.assertEqual(list(Path(folder).iterdir()), [])
            partial = Path(folder) / (NAMES[0] + '.part')
            partial.write_bytes(b'pre-existing file')
            response = io.BytesIO(content[NAMES[0]])
            response.status = 200
            client.opener.open.return_value = response
            with self.assertRaises(FileExistsError):
                client.download(sign(data), folder)
            self.assertEqual(partial.read_bytes(), b'pre-existing file')


class InstallationTests(unittest.TestCase):
    def prepare(self, root):
        public, content, data, sign = signing_fixture()
        stage = root / '.parmakizi-updates' / 'test-release'
        stage.mkdir(parents=True)
        for name in NAMES:
            (root / name).write_bytes(('old-' + name).encode())
            (stage / name).write_bytes(content[name])
        (root / 'user-data.txt').write_text('Preserve this')
        job = stage / 'job.json'
        job.write_text(json.dumps({'envelope': sign(data), 'target_dir': str(root), 'stage_dir': str(stage), 'parent_pid': os.getpid()}))
        return public, content, job

    def test_successful_replacement_requires_matching_boot_ack_and_preserves_user_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public, content, job = self.prepare(root)
            def boot(args, **kwargs):
                Path(args[-1]).write_text(json.dumps({'version': '0.2.0', 'pid': 42}))
                return Mock(pid=42)
            with patch('gateway.update_helper.subprocess.Popen', side_effect=boot):
                self.assertTrue(apply_update(job, wait_parent=False, public_key=public))
            for name in NAMES:
                self.assertEqual((root / name).read_bytes(), content[name])
            self.assertEqual((root / 'user-data.txt').read_text(), 'Preserve this')
            result = json.loads((job.parent / 'result.json').read_text())
            self.assertTrue((Path(result['backup']) / 'Parmakizi.exe').read_bytes().startswith(b'old-'))

    def test_onefile_launcher_and_python_child_acknowledgement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public, _, job = self.prepare(root)
            def boot(args, **kwargs):
                Path(args[-1]).write_text(json.dumps({'version': '0.2.0', 'pid': 43, 'launcher_pid': 42}))
                return Mock(pid=42)
            with patch('gateway.update_helper.subprocess.Popen', side_effect=boot):
                self.assertTrue(apply_update(job, wait_parent=False, public_key=public))

    def test_failed_startup_restores_both_executables(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public, _, job = self.prepare(root)
            with patch('gateway.update_helper.subprocess.Popen', side_effect=OSError('Bad executable')):
                self.assertFalse(apply_update(job, wait_parent=False, public_key=public))
            for name in NAMES:
                self.assertEqual((root / name).read_bytes(), ('old-' + name).encode())
            self.assertEqual((root / 'user-data.txt').read_text(), 'Preserve this')

    def test_modified_download_is_rejected_before_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public, _, job = self.prepare(root)
            (job.parent / 'Parmakizi.exe').write_bytes(b'wrong')
            with self.assertRaises(ValueError):
                apply_update(job, wait_parent=False, public_key=public)
            self.assertEqual((root / 'Parmakizi.exe').read_bytes(), b'old-Parmakizi.exe')

    def test_parallel_installation_is_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            public, _, job = self.prepare(root)
            with update_lock(root), self.assertRaises(OSError):
                apply_update(job, wait_parent=False, public_key=public)


class SyncCacheTests(unittest.TestCase):
    def test_cache_persists_only_acknowledged_hashes(self):
        row = {'userId': '1', 'recordedAt': '2026-10-30T09:00:00+03:00', 'status': 1, 'punch': 0}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.sqlite'
            with AttendanceCache(path) as cache:
                self.assertEqual(cache.unseen('d', [row]), [row])
                cache.acknowledge('d', [row])
            with AttendanceCache(path) as restored:
                self.assertEqual(restored.unseen('d', [row]), [])
                self.assertEqual(restored.unseen('other-device', [row]), [row])

    def test_updater_cannot_interrupt_a_device_command(self):
        connector = Connector({'cloud_url': 'https://example.com', 'gateway_token': 'test', 'allowed_subnets': ['192.168.1.0/24']})
        with connector.job_lock:
            self.assertFalse(connector.quiesce())
            self.assertFalse(connector.stop.is_set())
        self.assertTrue(connector.quiesce())
        self.assertTrue(connector.stop.is_set())


if __name__ == '__main__':
    unittest.main()

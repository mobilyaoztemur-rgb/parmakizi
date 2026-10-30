"""Functional tests against Wrangler's real local Worker and D1 engine."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid

from gateway.client import CloudClient, ApiError
from gateway.connector import Connector

BASE = os.environ.get('TEST_API_URL', 'http://127.0.0.1:8787')


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        settings = dict(line.split('=', 1) for line in Path('.dev.vars').read_text().splitlines() if '=' in line)
        cls.admin = CloudClient(BASE, allow_local=True)
        cls.admin.request('/api/auth/login', {'password': json.loads(settings['ADMIN_PASSWORD'])})

    def setUp(self):
        self.gateway = self.admin.request('/api/gateways', {'name': 'Integration ' + str(uuid.uuid4())[:8]})
        self.worker = CloudClient(BASE, self.gateway['token'], allow_local=True)
        self.device = self.admin.request('/api/devices', {'name': 'Test simulator', 'brand': 'Demo', 'model': 'test', 'ip': '192.168.1.201', 'port': 4370, 'driver': 'simulator', 'gatewayId': self.gateway['id']})

    def queue(self, kind, **params):
        payload = dict(type=kind, deviceId=self.device['id'], requestId=str(uuid.uuid4()), **params)
        return self.admin.request('/api/jobs', payload)

    def claim(self):
        return self.worker.request('/api/gateway/poll', {})['job']

    def upload(self, job, **data):
        return self.worker.request(f'/api/gateway/jobs/{job["id"]}/result', dict(claimToken=job['claimToken'], **data))

    def test_authentication_scope_and_revocation(self):
        with self.assertRaises(ApiError) as ctx:
            CloudClient(BASE, allow_local=True).request('/api/overview')
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(ApiError) as ctx:
            self.worker.request('/api/overview')
        self.assertEqual(ctx.exception.status, 401)
        self.queue('device.info')
        job = self.claim()
        other = self.admin.request('/api/gateways', {'name': 'Different gateway'})
        with self.assertRaises(ApiError) as ctx:
            CloudClient(BASE, other['token'], True).request(f'/api/gateway/jobs/{job["id"]}/result', {'claimToken': job['claimToken'], 'final': True})
        self.assertEqual(ctx.exception.status, 404)
        self.admin.request(f'/api/gateways/{self.gateway["id"]}/revoke', {})
        with self.assertRaises(ApiError) as ctx:
            self.worker.request('/api/gateway/poll', {})
        self.assertEqual(ctx.exception.status, 401)
        overview = self.admin.request('/api/overview')
        row = next(g for g in overview['gateways'] if g['id'] == self.gateway['id'])
        self.assertNotIn('token_hash', row)

    def test_origin_and_payload_validation(self):
        request = urllib.request.Request(BASE + '/api/gateways', data=b'{"name":"bad"}', headers={'Content-Type': 'application/json', 'Origin': 'https://other.example'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.admin.opener.open(request)
        self.assertEqual(ctx.exception.code, 403)
        with self.assertRaises(ApiError) as ctx:
            self.admin.request('/api/devices', {'name': 'Invalid', 'brand': 'x', 'ip': '169.254.169.254', 'port': 4370, 'driver': 'zkteco', 'gatewayId': self.gateway['id']})
        self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(ApiError):
            self.queue('unsupported.command')

    def test_idempotent_requests_and_atomic_claims(self):
        payload = {'type': 'device.info', 'deviceId': self.device['id'], 'requestId': str(uuid.uuid4())}
        first = self.admin.request('/api/jobs', payload)
        again = self.admin.request('/api/jobs', payload)
        self.assertEqual(first['id'], again['id'])
        self.assertTrue(again['duplicate'])
        with self.assertRaises(ApiError) as ctx:
            self.admin.request('/api/jobs', dict(payload, type='users.read'))
        self.assertEqual(ctx.exception.status, 409)
        def poll(_):
            return CloudClient(BASE, self.gateway['token'], True).request('/api/gateway/poll', {})['job']
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(poll, range(4)))
        self.assertEqual(sum(j is not None for j in claims), 1)
        self.queue('users.read')
        self.assertIsNone(self.claim(), 'One gateway must not run two device operations concurrently')

    def test_logout_revokes_server_session(self):
        settings = dict(line.split('=', 1) for line in Path('.dev.vars').read_text().splitlines() if '=' in line)
        client = CloudClient(BASE, allow_local=True)
        client.request('/api/auth/login', {'password': json.loads(settings['ADMIN_PASSWORD'])})
        saved_cookie = next(iter(client.cookies))
        client.request('/api/auth/logout', {})
        client.cookies.set_cookie(saved_cookie)
        with self.assertRaises(ApiError) as ctx:
            client.request('/api/me')
        self.assertEqual(ctx.exception.status, 401)

    def test_simulator_roundtrip_write_read_and_attendance(self):
        connector = Connector({'cloud_url': BASE, 'gateway_token': self.gateway['token'], 'allowed_subnets': ['192.168.1.0/24']}, allow_local=True)
        self.queue('users.upsert', uid=2, userId='1002', name='Deneme', card=42)
        connector.run(once=True)
        self.queue('users.read')
        connector.run(once=True)
        users = self.admin.request('/api/users?deviceId=' + self.device['id'])['records']
        self.assertTrue(any(u['user_id'] == '1002' and u['card'] == 42 for u in users))
        self.queue('attendance.read')
        connector.run(once=True)
        records = self.admin.request('/api/attendance?deviceId=' + self.device['id'])['records']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['recorded_at'], '2026-10-29T06:00:00.000Z')
        self.queue('attendance.read')
        connector.run(once=True)
        self.assertEqual(len(self.admin.request('/api/attendance?deviceId=' + self.device['id'])['records']), 1)

    def test_batches_are_idempotent_and_all_pages_accessible(self):
        self.queue('attendance.read')
        job = self.claim()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        records = [{'userId': '1', 'recordedAt': (start + timedelta(seconds=i)).isoformat(), 'localTime': (start + timedelta(seconds=i)).replace(tzinfo=None).isoformat(), 'timezone': 'UTC', 'status': 1, 'punch': 0} for i in range(205)]
        for offset in range(0, len(records), 40):
            self.upload(job, final=False, records=records[offset:offset + 40])
        self.upload(job, final=False, records=records[:40])
        self.upload(job, final=True, summary={'count': len(records)})
        self.assertTrue(self.upload(job, final=True, summary={})['duplicate'])
        found = []
        cursor = ''
        while True:
            response = self.admin.request('/api/attendance?' + urllib.parse.urlencode({'deviceId': self.device['id'], 'before': cursor}))
            found.extend(r['id'] for r in response['records'])
            cursor = response['next']
            if not cursor:
                break
        self.assertEqual(len(found), 205)
        self.assertEqual(len(set(found)), 205)

    def test_bad_batch_writes_nothing_and_error_is_visible(self):
        self.queue('users.read')
        job = self.claim()
        with self.assertRaises(ApiError) as ctx:
            self.upload(job, final=False, records=[{'uid': 1, 'userId': '1', 'name': 'Valid'}, {'uid': -1, 'userId': '2'}])
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(self.admin.request('/api/users?deviceId=' + self.device['id'])['records'], [])
        self.upload(job, final=True, error='Cihaz bağlantısı sağlanamadı.')
        row = next(j for j in self.admin.request('/api/overview')['jobs'] if j['id'] == job['id'])
        self.assertEqual(row['status'], 'failed')

    def test_uncertain_write_is_not_automatically_requeued(self):
        self.queue('users.upsert', uid=2, userId='1002', name='Test', card=0)
        job = self.claim()
        self.upload(job, final=True, error='Yazma yanıtı kayboldu.')
        self.assertIsNone(self.claim())
        row = next(j for j in self.admin.request('/api/overview')['jobs'] if j['id'] == job['id'])
        self.assertEqual(row['status'], 'uncertain')

    def test_device_edit_and_removal_preserve_running_work(self):
        self.queue('users.read')
        job = self.claim()
        self.upload(job, final=False, records=[{'uid': 1, 'userId': '1', 'name': 'Retained'}])
        self.upload(job, final=True, summary={'count': 1})
        self.queue('device.info')
        patch = {'name': 'Edited', 'brand': 'Demo', 'model': 'Updated', 'ip': '192.168.1.202', 'port': 4370}
        path = '/api/devices/' + self.device['id']
        for method in ('PATCH', 'DELETE'):
            with self.assertRaises(ApiError) as ctx:
                self.admin.request(path, patch, method=method)
            self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.admin.request('/api/users?deviceId=' + self.device['id'])['records'][0]['name'], 'Retained')
        job = self.claim()
        self.upload(job, final=True, summary={'simulated': True})
        self.admin.request(path, patch, method='PATCH')
        changed = next(d for d in self.admin.request('/api/overview')['devices'] if d['id'] == self.device['id'])
        self.assertEqual(changed['ip'], '192.168.1.202')
        self.assertIsNone(changed['last_contact'])
        self.assertEqual(self.admin.request('/api/users?deviceId=' + self.device['id'])['records'][0]['name'], 'Retained')
        self.admin.request(path, {}, method='DELETE')
        overview = self.admin.request('/api/overview')
        self.assertFalse(any(d['id'] == self.device['id'] for d in overview['devices']))
        self.assertFalse(any(j['device_id'] == self.device['id'] for j in overview['jobs']))
        self.assertEqual(self.admin.request('/api/users?deviceId=' + self.device['id'])['records'], [])

    def test_device_edit_rejects_duplicate_network_address(self):
        self.admin.request('/api/devices', {'name': 'Other', 'brand': 'Demo', 'ip': '192.168.1.202', 'port': 4370, 'driver': 'simulator', 'gatewayId': self.gateway['id']})
        with self.assertRaises(ApiError) as ctx:
            self.admin.request('/api/devices/' + self.device['id'], {'name': 'Clash', 'brand': 'Demo', 'ip': '192.168.1.202', 'port': 4370}, method='PATCH')
        self.assertEqual(ctx.exception.status, 409)
        device = next(d for d in self.admin.request('/api/overview')['devices'] if d['id'] == self.device['id'])
        self.assertEqual(device['ip'], '192.168.1.201')

    def test_automatic_sync_schedules_once_and_can_be_disabled(self):
        route = '/api/devices/' + self.device['id'] + '/sync'
        self.admin.request(route, {'enabled': True, 'intervalSeconds': 30}, method='PATCH')
        job = self.claim()
        self.assertEqual((job['type'], job['origin']), ('attendance.read', 'scheduled'))
        self.assertIsNone(self.claim())
        self.upload(job, final=True, summary={'count': 0})
        self.assertIsNone(self.claim())
        device = next(d for d in self.admin.request('/api/overview')['devices'] if d['id'] == self.device['id'])
        self.assertIsNotNone(device['last_sync_at'])
        self.admin.request(route, {'enabled': False, 'intervalSeconds': 30}, method='PATCH')
        self.assertIsNone(self.claim())

    def test_manual_commands_have_priority_over_scheduled_reads(self):
        self.queue('device.settings.read')
        self.admin.request('/api/devices/' + self.device['id'] + '/sync', {'enabled': True, 'intervalSeconds': 60}, method='PATCH')
        job = self.claim()
        self.assertEqual(job['type'], 'device.settings.read')
        self.upload(job, final=True, summary={'deviceTime': '2026-10-30T09:00:00'})
        self.assertEqual(self.claim()['type'], 'attendance.read')

    def test_remote_clock_sync_and_restart_have_safe_write_outcomes(self):
        connector = Connector({'cloud_url': BASE, 'gateway_token': self.gateway['token'], 'allowed_subnets': ['192.168.1.0/24']}, allow_local=True)
        queued = self.queue('device.time.sync')
        connector.run(once=True)
        job = next(j for j in self.admin.request('/api/overview')['jobs'] if j['id'] == queued['id'])
        self.assertEqual(job['status'], 'succeeded')
        self.assertTrue(json.loads(job['result'])['verified'])
        self.queue('device.restart')
        restart = self.claim()
        self.upload(restart, final=True, error='Command response lost')
        job = next(j for j in self.admin.request('/api/overview')['jobs'] if j['id'] == restart['id'])
        self.assertEqual(job['status'], 'uncertain')

    def test_published_manifest_is_signed_and_downloads_require_authentication(self):
        import base64, hashlib
        from cryptography.hazmat.primitives import serialization
        from gateway.updater import verify_manifest, UpdateClient
        settings = dict(line.split('=', 1) for line in Path('.dev.vars').read_text().splitlines() if '=' in line)
        key = serialization.load_der_private_key(base64.b64decode(json.loads(settings['UPDATES_SIGNING_KEY'])), password=None)
        public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        files = []
        for stem in ('Parmakizi', 'ParmakiziUpdater'):
            content = ('MZ-local-test-fixture-' + stem).encode()
            digest = hashlib.sha256(content).hexdigest()
            files.append({'name': stem + '.exe', 'path': f'/updates/files/{stem}-0.0.1-{digest[:12]}.exe', 'sha256': digest, 'size': len(content)})
        envelope = self.admin.request('/api/updates/publish', {'version': '0.0.1', 'files': files})
        self.assertEqual(verify_manifest(envelope, public)['version'], '0.0.1')
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(BASE + files[0]['path'])
        self.assertEqual(ctx.exception.code, 401)
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            UpdateClient(BASE, auth_client=self.admin, public_key=public, allow_local=True).download(envelope, folder)
            self.assertTrue((Path(folder) / 'Parmakizi.exe').read_bytes().startswith(b'MZ-local-test-fixture'))
        with self.assertRaises(ValueError):
            verify_manifest(envelope)  # Production verification key never trusts the local identity.


if __name__ == '__main__':
    unittest.main()

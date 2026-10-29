from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from gateway.client import CloudClient
from gateway.connector import networks, allowed_ip, discover, Connector, ZktecoAdapter
from gateway.storage import dpapi, load_settings, save_settings


class SafetyTests(unittest.TestCase):
    def test_restrict_networks(self):
        allowed = networks(['192.168.1.0/24'])
        self.assertEqual(allowed_ip('192.168.1.20', allowed), '192.168.1.20')
        for ip in ['127.0.0.1', '169.254.169.254', '8.8.8.8', '192.168.2.1', '::1']:
            with self.assertRaises(ValueError):
                allowed_ip(ip, allowed)
        for subnet in ['0.0.0.0/0', '127.0.0.0/8', '169.254.0.0/16', '8.8.8.0/24']:
            with self.assertRaises(ValueError):
                networks([subnet])

    def test_discovery_bounds_before_any_connection(self):
        with patch('socket.create_connection') as connection:
            for subnet in ['192.168.0.0/16', '192.168.2.0/24', '8.8.8.0/24', '192.168.1.1/24']:
                with self.assertRaises(ValueError):
                    discover(subnet, 4370, networks(['192.168.1.0/24']))
            connection.assert_not_called()

    def test_https_required_and_no_redirects(self):
        for url in ['http://example.com', 'https://user:password@example.com', 'https://example.com/path', 'https://example.com?key=test']:
            with self.assertRaises(ValueError):
                CloudClient(url)
        client = CloudClient('http://127.0.0.1:8787', allow_local=True)
        self.assertIsNone(next(h for h in client.opener.handlers if hasattr(h, 'redirect_request')).redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_linux_does_not_save_plaintext_secrets(self):
        if os.name == 'nt':
            self.skipTest('Windows uses DPAPI; covered separately')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            save_settings({'cloud_url': 'https://example.com'}, {'gateway_token': 'sensitive-value'}, path)
            self.assertNotIn('sensitive-value', path.read_text())
            self.assertEqual(load_settings(path)[1], {})

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI only')
    def test_windows_dpapi_roundtrip(self):
        plaintext = b'local-credential-test'
        encrypted = dpapi(plaintext)
        self.assertNotIn(plaintext, encrypted)
        self.assertEqual(dpapi(encrypted, decrypt=True), plaintext)


class AdapterTests(unittest.TestCase):
    def test_writing_preserves_existing_privilege_password_and_group(self):
        old = SimpleNamespace(uid=7, user_id='70', name='Old', card=0, privilege=14, password='local-only-test', group_id='2')
        after = SimpleNamespace(uid=7, user_id='71', name='Yeni', card=77)
        connection = Mock()
        connection.get_users.side_effect = [[old], [after]]
        adapter = ZktecoAdapter({})
        @contextmanager
        def fake(_):
            yield connection
        with patch.object(adapter, 'connection', fake):
            _, summary = adapter.execute({}, 'users.upsert', {'uid': 7, 'userId': '71', 'name': 'Yeni', 'card': 77})
        args = connection.set_user.call_args.kwargs
        self.assertEqual((args['privilege'], args['password'], args['group_id']), (14, 'local-only-test', '2'))
        self.assertTrue(summary['verified'])

    def test_write_requires_readback(self):
        adapter = ZktecoAdapter({})
        connection = Mock()
        connection.get_users.side_effect = [[], []]
        @contextmanager
        def fake(_):
            yield connection
        with patch.object(adapter, 'connection', fake), self.assertRaises(ValueError):
            adapter.execute({}, 'users.upsert', {'uid': 1, 'userId': '1', 'name': 'Test', 'card': 0})

    def test_attendance_timezone_conversion(self):
        adapter = ZktecoAdapter({'timezone': 'Europe/Istanbul'})
        connection = Mock()
        connection.get_attendance.return_value = [SimpleNamespace(user_id='1', timestamp=datetime(2026, 10, 29, 9), status=1, punch=0)]
        @contextmanager
        def fake(_):
            yield connection
        with patch.object(adapter, 'connection', fake):
            records, _ = adapter.execute({}, 'attendance.read', {})
        self.assertEqual(records[0]['recordedAt'], '2026-10-29T09:00:00+03:00')
        connection.disable_device.assert_not_called()

    def test_upload_failure_does_not_repeat_device_write(self):
        connector = Connector({'cloud_url': 'https://example.com', 'gateway_token': 'test', 'allowed_subnets': ['192.168.1.0/24']})
        job = {'id': 'job', 'type': 'users.upsert', 'claimToken': 'claim'}
        with patch.object(connector, 'execute', return_value=([], {'written': True})) as execute:
            with patch.object(connector.client, 'request', side_effect=ConnectionError), self.assertRaises(ConnectionError):
                connector.process(job)
            execute.assert_called_once_with(job)


if __name__ == '__main__':
    unittest.main()

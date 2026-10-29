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


if __name__ == '__main__':
    unittest.main()

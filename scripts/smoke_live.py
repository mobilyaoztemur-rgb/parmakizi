"""Verify the deployed API with a temporary simulator and remove only its records.

Run from the repository root: python -m scripts.smoke_live
Requires ADMIN_PASSWORD, CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID.
Never reads local development credentials or connects to a physical device.
"""
import json
import os
from pathlib import Path
import urllib.request
import uuid

from gateway.client import ApiError, CloudClient, NoRedirect
from gateway.connector import Connector


def main():
    base = os.environ.get('TEST_API_URL') or json.loads(Path('gateway/defaults.json').read_text())['cloud_url']
    password = os.environ.get('ADMIN_PASSWORD')
    if not password:
        raise SystemExit('ADMIN_PASSWORD ortamda bulunmalı; yerel geliştirme parolası kullanılmaz.')
    account = os.environ.get('CLOUDFLARE_ACCOUNT_ID')
    token = os.environ.get('CLOUDFLARE_API_TOKEN')
    if not account or not token:
        raise SystemExit('Geçici test kayıtlarını temizlemek için Cloudflare erişimi gerekiyor.')
    config = json.loads(Path('wrangler.jsonc').read_text())
    database_id = next(x['database_id'] for x in config['d1_databases'] if x['binding'] == 'DB')
    endpoint = f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database/{database_id}/query'
    opener = urllib.request.build_opener(NoRedirect())

    def sql(statement, params):
        req = urllib.request.Request(endpoint, data=json.dumps({'sql': statement, 'params': params}).encode(),
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with opener.open(req, timeout=30) as response:
            result = json.load(response)
        if not result.get('success'):
            raise RuntimeError('Canlı test verisi temizliği başarısız; Cloudflare yanıtını kontrol edin.')

    admin = CloudClient(base)
    gateway = None
    job_ids = []
    try:
        if admin.request('/api/health').get('ok') is not True:
            raise RuntimeError('Canlı API hazır değil.')
        try:
            CloudClient(base).request('/api/overview')
        except ApiError as error:
            if error.status != 401:
                raise
        else:
            raise RuntimeError('Kimliksiz istek reddedilmedi.')
        admin.request('/api/auth/login', {'password': password})
        gateway = admin.request('/api/gateways', {'name': 'Geçici canlı bağlantı kontrolü'})
        device = admin.request('/api/devices', {'name': 'Geçici simülatör kontrolü', 'brand': 'Demo', 'model': 'simulator',
                                               'ip': '192.168.254.254', 'port': 4370, 'driver': 'simulator', 'gatewayId': gateway['id']})
        connector = Connector({'cloud_url': base, 'gateway_token': gateway['token'], 'allowed_subnets': ['192.168.254.0/24']})
        admin.request('/api/devices/' + device['id'], {'name': 'Geçici düzenleme kontrolü', 'brand': 'Demo', 'model': 'simulator', 'ip': '192.168.254.254', 'port': 4370}, method='PATCH')

        def run(kind, **params):
            job = admin.request('/api/jobs', dict(type=kind, deviceId=device['id'], requestId=str(uuid.uuid4()), **params))
            job_ids.append(job['id'])
            connector.run(once=True)
            result = next(j for j in admin.request('/api/overview')['jobs'] if j['id'] == job['id'])
            if result['status'] != 'succeeded':
                raise RuntimeError('Canlı simülatör görevi tamamlanmadı: ' + kind)
            return result

        info = json.loads(run('device.info')['result'])
        if info.get('simulated') is not True:
            raise RuntimeError('Test hedefi simülatör değil.')
        run('users.upsert', uid=2, userId='1002', name='Bulut Kontrol', card=42)
        run('users.read')
        users = admin.request('/api/users?deviceId=' + device['id'])['records']
        if not any(u['user_id'] == '1002' and u['name'] == 'Bulut Kontrol' and u['card'] == 42 for u in users):
            raise RuntimeError('Personel yazma/okuma doğrulanamadı.')
        run('attendance.read')
        run('attendance.read')
        records = admin.request('/api/attendance?deviceId=' + device['id'])['records']
        if len(records) != 1 or records[0]['recorded_at'] != '2026-10-29T06:00:00.000Z':
            raise RuntimeError('Geçiş aktarımı veya tekrar engelleme doğrulanamadı.')
        admin.request('/api/devices/' + device['id'], {}, method='DELETE')
        if any(d['id'] == device['id'] for d in admin.request('/api/overview')['devices']):
            raise RuntimeError('Geçici cihaz kaldırılamadı.')
        admin.request(f'/api/gateways/{gateway["id"]}/revoke', {})
        try:
            connector.client.request('/api/gateway/poll', {})
        except ApiError as error:
            if error.status != 401:
                raise
        else:
            raise RuntimeError('İptal edilen bağlantı anahtarı hâlâ çalışıyor.')
        print('Canlı D1/API: giriş, anahtar eşleştirme, cihaz düzenleme/kaldırma, simülatör bilgisi, personel yazma/okuma, geçiş aktarımı, tekrar engelleme ve anahtar iptali başarılı.')
    finally:
        if gateway:
            # All deletes are scoped to the random gateway created by this run.
            sql('DELETE FROM attendance WHERE device_id IN (SELECT id FROM devices WHERE gateway_id=?)', [gateway['id']])
            sql('DELETE FROM device_users WHERE device_id IN (SELECT id FROM devices WHERE gateway_id=?)', [gateway['id']])
            for job_id in job_ids:
                sql('DELETE FROM audit WHERE target=?', [job_id])
            sql('DELETE FROM jobs WHERE gateway_id=?', [gateway['id']])
            sql('DELETE FROM audit WHERE actor=? OR target=?', [gateway['id'], gateway['id']])
            sql('DELETE FROM audit WHERE target IN (SELECT id FROM devices WHERE gateway_id=?)', [gateway['id']])
            sql('DELETE FROM devices WHERE gateway_id=?', [gateway['id']])
            sql('DELETE FROM gateways WHERE id=?', [gateway['id']])
            print('Geçici simülatör ve test kayıtları temizlendi.')
        try:
            admin.request('/api/auth/logout', {})
        except ApiError:
            pass


if __name__ == '__main__':
    main()

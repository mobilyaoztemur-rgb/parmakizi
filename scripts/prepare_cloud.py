"""Idempotently bind this checkout to an account's D1 database.

Credentials come only from the environment; responses and credentials are never dumped.
Run in the repository root. Existing non-placeholder database IDs are preserved.
"""
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

PLACEHOLDER = '00000000-0000-0000-0000-000000000000'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    account = os.environ.get('CLOUDFLARE_ACCOUNT_ID', '')
    token = os.environ.get('CLOUDFLARE_API_TOKEN', '')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', account) or not token:
        raise SystemExit('CLOUDFLARE_ACCOUNT_ID (32 hex karakter) ve CLOUDFLARE_API_TOKEN gerekli.')
    path = Path('wrangler.jsonc')
    config = json.loads(path.read_text(encoding='utf-8'))
    db = next(d for d in config['d1_databases'] if d['binding'] == 'DB')
    if db['database_id'] != PLACEHOLDER:
        print('Mevcut D1 bağlantısı korundu.')
        return
    base = f'https://api.cloudflare.com/client/v4/accounts/{account}/d1/database'
    opener = urllib.request.build_opener(NoRedirect())

    def api(url, data=None):
        req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            with opener.open(req, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise SystemExit(f'Cloudflare HTTP {exc.code}; hesap kimliğini, ağ erişimini ve token yetkilerini kontrol edin.') from None
        except (OSError, ValueError):
            raise SystemExit('Cloudflare bağlantısı başarısız; ağ erişimini kontrol edin.') from None
        if not result.get('success'):
            raise SystemExit('Cloudflare işlemi reddedildi; hesap ve D1 yetkilerini kontrol edin.')
        return result['result']

    # Inspect all pages before creating anything; never assume absence from a partial list.
    found = []
    page = 1
    while True:
        items = api(base + f'?page={page}&per_page=100')
        found.extend(d for d in items if d['name'] == db['database_name'])
        if len(items) < 100:
            break
        page += 1
    if len(found) > 1:
        raise SystemExit('Aynı isimde birden fazla D1 bulundu; database_id değerini açıkça yapılandırın.')
    chosen = found[0] if found else api(base, {'name': db['database_name']})
    database_id = chosen['uuid']
    if not re.fullmatch(r'[a-fA-F0-9-]{36}', database_id):
        raise SystemExit('Beklenmeyen D1 kimliği; yapılandırma değiştirilmedi.')
    db['database_id'] = database_id
    path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    print('D1 bulundu/oluşturuldu ve wrangler.jsonc bağlantısı güncellendi. Veritabanı verileri korunmuştur.')


if __name__ == '__main__':
    main()

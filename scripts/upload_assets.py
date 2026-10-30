"""Use Cloudflare's normal upload-session API, with one small asset per request.

Session credentials stay in memory; the inherited proxy and TLS trust are preserved.
"""
import base64
import json
import mimetypes
import os
from pathlib import Path
import urllib.error
import urllib.request
import uuid

import blake3


def upload_assets(directory=Path('public')):
    account, token = os.environ['CLOUDFLARE_ACCOUNT_ID'], os.environ['CLOUDFLARE_API_TOKEN']
    script = json.loads(Path('wrangler.jsonc').read_text())['name']
    base = f'https://api.cloudflare.com/client/v4/accounts/{account}/workers'
    manifest, lookup = {}, {}
    for path in directory.rglob('*'):
        if not path.is_file():
            continue
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Küçük varlık yükleyicisi 1 MiB altındaki dosyaları kabul eder.')
        content = base64.b64encode(path.read_bytes())
        extension = path.suffix.removeprefix('.')
        digest = blake3.blake3(content + extension.encode()).hexdigest()[:32]
        manifest['/' + path.relative_to(directory).as_posix()] = {'hash': digest, 'size': path.stat().st_size}
        lookup[digest] = path

    def api(url, body, credential, content_type):
        request = urllib.request.Request(url, data=body, headers={'Authorization': 'Bearer ' + credential, 'Content-Type': content_type, 'User-Agent': 'Parmakizi-Publisher/0.2.0'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f'Cloudflare küçük varlık yüklemesi HTTP {error.code}; kimlik doğrulaması veya API aracısını kontrol edin.') from None
        if not result.get('success'):
            raise RuntimeError('Cloudflare varlık işlemini kabul etmedi.')
        return result['result']

    session = api(base + f'/scripts/{script}/assets-upload-session', json.dumps({'manifest': manifest}).encode(), token, 'application/json')
    hashes = list(dict.fromkeys(h for bucket in session['buckets'] for h in bucket))
    for number, digest in enumerate(hashes, 1):
        path = lookup[digest]
        boundary = 'Parmakizi' + uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or 'application/null'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="{digest}"; filename="{digest}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body += base64.b64encode(path.read_bytes()) + f'\r\n--{boundary}--\r\n'.encode()
        api(base + '/assets/upload?base64=true', body, session['jwt'], 'multipart/form-data; boundary=' + boundary)
        print(f'Küçük güncelleme varlığı yüklendi: {number}/{len(hashes)}', flush=True)
    print('Varlık ön yüklemesi tamamlandı; Wrangler mevcut içerikleri kullanacak.', flush=True)


if __name__ == '__main__':
    upload_assets()

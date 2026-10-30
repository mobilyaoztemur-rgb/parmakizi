"""Stage a tested Windows artifact, deploy Cloudflare assets, then sign/publish its manifest."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.client import CloudClient
from gateway.updater import MAX_FILE, NAMES, verify_manifest, version_tuple
from scripts.prepare_updates import main as retain_updates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifact', required=True)
    parser.add_argument('--notes', default='')
    args = parser.parse_args()
    version = json.loads(Path('package.json').read_text())['version']
    version_tuple(version)
    source = Path(args.artifact)
    password = os.environ.get('ADMIN_PASSWORD')
    if not password:
        raise SystemExit('ADMIN_PASSWORD güvenli ortam değişkeni gerekli.')
    files = []
    for name in NAMES:
        path = source / name
        content = path.read_bytes()
        if not content.startswith(b'MZ') or not 1 <= len(content) <= MAX_FILE:
            raise SystemExit('Geçerli ve 25 MiB altında Windows EXE dosyaları gerekiyor.')
        digest = hashlib.sha256(content).hexdigest()
        checksum = (source / (name + '.sha256')).read_text().split()[0].lower()
        if digest != checksum:
            raise SystemExit('Derleme çıktısının SHA-256 dosyası eşleşmiyor.')
        target = f'/updates/files/{name[:-4]}-{version}-{digest[:12]}.exe'
        files.append({'name': name, 'path': target, 'sha256': digest, 'size': len(content)})
    report = json.loads((source / 'self-test.json').read_text(encoding='utf-8'))
    fingerprint = hashlib.sha256(Path('gateway/update-key.pem').read_bytes()).hexdigest()
    if report.get('ok') is not True or report.get('windows_dpapi') is not True or report.get('version') != version or report.get('public_key_sha256') != fingerprint:
        raise SystemExit('Başarılı Windows EXE kontrol raporu gerekli.')
    helper_report = json.loads((source / 'helper-self-test.json').read_text(encoding='utf-8'))
    if helper_report.get('ok') is not True or helper_report.get('version') != version or helper_report.get('public_key_sha256') != fingerprint:
        raise SystemExit('Doğru imza anahtarını taşıyan güncelleyici kontrol raporu gerekli.')
    retain_updates()
    for file in files:
        destination = Path('public') / file['path'].lstrip('/')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != file['sha256']:
            raise SystemExit('Mevcut varlık beklenen dosyayla uyuşmuyor; üzerine yazılmadı.')
        shutil.copyfile(source / file['name'], destination)
    subprocess.run(['npm', 'run', 'build'], check=True)
    subprocess.run(['npx', '--no-install', 'wrangler', 'deploy'], check=True)
    origin = json.loads(Path('gateway/defaults.json').read_text())['cloud_url']
    admin = CloudClient(origin)
    admin.request('/api/auth/login', {'password': password})
    try:
        for file in files:
            for attempt in range(30):
                request = urllib.request.Request(origin + file['path'], headers={'User-Agent': 'Parmakizi/0.2.0'})
                try:
                    with admin.opener.open(request, timeout=15) as response:
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := response.read(128 * 1024):
                            size += len(chunk)
                            if size > file['size']:
                                raise RuntimeError('Yayımlanan dosya beklenen boyutu aşıyor.')
                            digest.update(chunk)
                        if response.status == 200 and size == file['size'] and digest.hexdigest() == file['sha256']:
                            break
                except urllib.error.HTTPError as error:
                    if error.code not in (404, 503):
                        raise
                time.sleep(1)
            else:
                raise RuntimeError('Yeni dosyalar Cloudflare kenar sunucularına henüz yayılmadı; sürüm yayımlanmadı.')
        envelope = admin.request('/api/updates/publish', {'version': version, 'files': files, 'notes': args.notes})
        result = verify_manifest(envelope)
        if result['version'] != version:
            raise RuntimeError('Yayımlanan sürüm doğrulanamadı.')
        print('İmzalı Windows güncellemesi Cloudflare üzerinde yayımlandı:', version)
    finally:
        admin.request('/api/auth/logout', {})


if __name__ == '__main__':
    main()

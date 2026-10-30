"""Retain the current signed release's assets when redeploying the Worker."""
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gateway.client import ApiError, CloudClient
from gateway.updater import UpdateClient, version_tuple


def main():
    root = Path(__file__).resolve().parents[1]
    # Remove only exact, known local test fixtures, never arbitrary release files.
    for name in ('Parmakizi', 'ParmakiziUpdater'):
        content = ('MZ-local-test-fixture-' + name).encode()
        digest = hashlib.sha256(content).hexdigest()
        path = root / 'public' / 'updates' / 'files' / f'{name}-0.0.1-{digest[:12]}.exe'
        if path.exists() and path.read_bytes() == content:
            path.unlink()
    origin = json.loads((root / 'gateway/defaults.json').read_text())['cloud_url']
    client = UpdateClient(origin)
    try:
        envelope = client.client.request('/api/updates/latest')
    except ApiError as error:
        if error.status == 404:  # Initial upgrade from the pre-update server.
            print('Henüz dağıtılmış güncelleme hizmeti yok; ilk yayın hazırlanıyor.')
            return
        if error.status == 401 and version_tuple(client.client.request('/api/health').get('version', '0.0.0')) < (0, 2, 0):
            print('0.2 öncesi sunucu doğrulandı; ilk güncelleme dağıtımı hazırlanıyor.')
            return
        raise
    if envelope.get('update', False) is None:
        print('Korunacak mevcut güncelleme paketi yok.')
        return
    from gateway.updater import verify_manifest
    data = verify_manifest(envelope)
    destination = root / 'public/updates/files'
    existing = all((destination / Path(file['path']).name).exists() for file in data['files'])
    if existing:
        from gateway.updater import check_file
        for file in data['files']:
            check_file(destination / Path(file['path']).name, file)
        print('Mevcut imzalı güncelleme dosyaları korundu.')
        return
    password = os.environ.get('ADMIN_PASSWORD')
    if not password:
        raise SystemExit('Mevcut güncelleme paketini korumak için ADMIN_PASSWORD gerekiyor; boş varlıklarla yayın yapılmadı.')
    admin = CloudClient(origin)
    admin.request('/api/auth/login', {'password': password})
    try:
        staging = root / '.wrangler/retained-release' / data['version']
        UpdateClient(origin, auth_client=admin).download(envelope, staging)
        destination.mkdir(parents=True, exist_ok=True)
        import shutil
        for file in data['files']:
            shutil.copyfile(staging / file['name'], destination / Path(file['path']).name)
        print('Mevcut güncelleme paketi Cloudflare’dan alındı ve yeni yayına dahil edildi.')
    finally:
        admin.request('/api/auth/logout', {})


if __name__ == '__main__':
    main()

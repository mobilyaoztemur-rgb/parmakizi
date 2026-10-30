"""Generate ignored local secrets; never print them or overwrite existing settings."""
import argparse
import getpass
from pathlib import Path
import secrets
import json
import base64
import hashlib

parser = argparse.ArgumentParser()
parser.add_argument('--generate', action='store_true', help='Generate a random local admin password for automated checks')
args = parser.parse_args()
target = Path(__file__).resolve().parents[1] / '.dev.vars'
if target.exists():
    print('.dev.vars zaten mevcut; korundu.')
else:
    password = secrets.token_urlsafe(24) if args.generate else getpass.getpass('Yerel yönetici parolası (en az 12 karakter): ')
    if len(password) < 12:
        raise SystemExit('En az 12 karakter gerekli.')
    with target.open('x', encoding='utf-8') as file:
        file.write('ADMIN_PASSWORD=' + json.dumps(password) + '\nSESSION_SECRET=' + json.dumps(secrets.token_urlsafe(48)) + '\n')
    target.chmod(0o600)
    print('Yerel ayarlar .dev.vars içine kaydedildi; Git tarafından yok sayılır.')

# Local signing identity is independent from the production key held in Cloudflare.
keys = {line.partition('=')[0].strip() for line in target.read_text(encoding='utf-8').splitlines()}
if 'UPDATES_SIGNING_KEY' not in keys:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    private = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    with target.open('a', encoding='utf-8') as file:
        file.write('\nUPDATES_SIGNING_KEY=' + json.dumps(base64.b64encode(private).decode()) + '\n')
    print('Ayrı yerel test imzalama anahtarı eklendi; üretim anahtarı korunur.')

for name in ('Parmakizi', 'ParmakiziUpdater'):
    content = ('MZ-local-test-fixture-' + name).encode()
    digest = hashlib.sha256(content).hexdigest()
    fixture = target.parent / 'public' / 'updates' / 'files' / f'{name}-0.0.1-{digest[:12]}.exe'
    fixture.parent.mkdir(parents=True, exist_ok=True)
    if not fixture.exists():
        fixture.write_bytes(content)

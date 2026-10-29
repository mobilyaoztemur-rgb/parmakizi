"""Generate ignored local secrets; never print them or overwrite existing settings."""
import argparse
import getpass
from pathlib import Path
import secrets
import json

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

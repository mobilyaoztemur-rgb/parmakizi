"""Set one release version consistently before building and publishing."""
import argparse
import json
from pathlib import Path
import re

parser = argparse.ArgumentParser()
parser.add_argument('version')
args = parser.parse_args()
if not re.fullmatch(r'(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})', args.version):
    raise SystemExit('Sürüm major.minor.patch biçiminde olmalı.')
root = Path(__file__).resolve().parents[1]
version_file = root / 'gateway/version.py'
if not re.fullmatch(r'VERSION = [\'\"][0-9.]+[\'\"]\s*', version_file.read_text()):
    raise SystemExit('Beklenmeyen sürüm dosyası; mevcut dosya değiştirilmedi.')
for name in ('package.json', 'package-lock.json'):
    path = root / name
    data = json.loads(path.read_text())
    data['version'] = args.version
    if name == 'package-lock.json':
        data['packages']['']['version'] = args.version
    path.write_text(json.dumps(data, indent=2) + '\n')
version_file.write_text('VERSION = ' + repr(args.version) + '\n')
print('Sürüm güncellendi:', args.version)

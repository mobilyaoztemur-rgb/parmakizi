"""Pass runtime secrets over stdin, never shell arguments or logs."""
import json
import os
import subprocess

values = {name: os.environ.get(name, '') for name in ('ADMIN_PASSWORD', 'SESSION_SECRET')}
if len(values['ADMIN_PASSWORD']) < 12 or len(values['SESSION_SECRET']) < 32:
    raise SystemExit('GitHub production secrets: ADMIN_PASSWORD en az 12, SESSION_SECRET en az 32 karakter olmalı.')
subprocess.run(['npx', '--no-install', 'wrangler', 'secret', 'bulk'], input=json.dumps(values), text=True, check=True)

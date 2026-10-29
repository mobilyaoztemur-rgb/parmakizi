import time
import urllib.request

for attempt in range(60):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=2) as response:
            if response.status == 200:
                print('Yerel API ve D1 hazır.')
                break
    except OSError:
        pass
    time.sleep(1)
else:
    raise SystemExit('API 60 saniye içinde hazır olmadı.')

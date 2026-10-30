"""Read-only inventory from manufacturer catalogues; catalogue presence is never device support."""
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self.links.append(href)


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Parmakizi/0.2.0'})
    with urllib.request.urlopen(request, timeout=25) as response:
        content = response.read(3000000)
        encoding = response.headers.get_content_charset() or ('windows-1254' if 'perkotek.com' in url else 'utf-8')
        return content.decode(encoding, errors='replace')


def plain(value):
    return re.sub(r'\s+', ' ', unescape(re.sub('<[^>]+>', ' ', value))).strip()


def main():
    sources = {
        'Perkotek': ['https://www.perkotek.com/markalar/magic-pass', 'https://www.perkotek.com/parmak-izi-okuyucu'],
        'ZKTeco': ['https://www.zkteco.com/en/TimeAttendanceTerminal', 'https://www.zkteco.com/en/iClockSeries', 'https://www.zkteco.com/en/TimeAttendance', 'https://www.zkteco.com/en/Standalone_Device']
    }
    products = {}
    failures = []
    try:
        sitemap = ET.fromstring(fetch('https://www.zkteco.com/en/sitemap.xml'))
        for element in sitemap.iter():
            if element.tag.endswith('loc') and element.text:
                parsed = urllib.parse.urlsplit(element.text)
                if parsed.hostname == 'www.zkteco.com' and re.fullmatch(r'/en/[^/]*(?:TimeAttendance|iClock|Fingerprint)[^/]*', parsed.path, re.I):
                    category = 'https://www.zkteco.com' + parsed.path
                    if category not in sources['ZKTeco']:
                        sources['ZKTeco'].append(category)
    except Exception as error:
        failures.append({'source': 'https://www.zkteco.com/en/sitemap.xml', 'error': type(error).__name__})
    for brand, categories in sources.items():
        for url in categories:
            try:
                parser = Links()
                parser.feed(fetch(url))
                for link in parser.links:
                    target = urllib.parse.urljoin(url, link)
                    parsed = urllib.parse.urlsplit(target)
                    if brand == 'Perkotek' and parsed.hostname == 'www.perkotek.com' and re.match(r'^/(magic-pass-|perkotek-p\d)', parsed.path):
                        products[target] = brand
                    if brand == 'ZKTeco' and parsed.hostname == 'www.zkteco.com' and re.match(r'^/en/[^/]*(TimeAttendance|iClock|Fingerprint|Standalone_Device|HybridBiometricLock|SensePalm)[^/]*/[^/]+/?$', parsed.path, re.I):
                        products[target] = brand
            except Exception as error:
                failures.append({'source': url, 'error': type(error).__name__})

    def inspect(pair):
        url, brand = pair
        try:
            html = fetch(url)
            heading = re.search(r'<h1\b[^>]*>(.*?)</h1>', html, re.S | re.I) or re.search(r'<title\b[^>]*>(.*?)</title>', html, re.S | re.I)
            model = plain(heading.group(1))[:160] if heading else url.rsplit('/', 1)[-1]
            body = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', ' ', html, flags=re.S | re.I)
            text = plain(body)
            network = re.search(r'.{0,70}TCP[-/ ]?IP.{0,160}', text, re.I)
            evidence = network.group(0) if network else 'Ürün sayfası okundu; TCP/IP komut protokolü bu sayfadan doğrulanamadı.'
            return {'brand': brand, 'family': model, 'models': model, 'status': 'research',
                    'protocol': 'TCP/IP · SDK belirsiz' if network else 'Model / SDK doğrulaması gerekli',
                    'note': 'Üretici katalog girdisi; ZK uyumluluğu ve saha desteği doğrulanmış değildir.',
                    'source': url, 'evidence': '2026-10-30 resmî ürün sayfası: ' + evidence}
        except Exception as error:
            failures.append({'source': url, 'error': type(error).__name__})
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        models = [model for model in pool.map(inspect, sorted(products.items())) if model]
    root = Path(__file__).resolve().parents[1]
    (root / 'shared/model_inventory.json').write_text(json.dumps(models, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report = ['# Cihaz araştırma envanteri', '', 'Araştırma: 30 Ekim 2026. Üreticilerin erişilebilen güncel kategori sayfalarından derlendi; tüm tarihî modellerin eksiksiz listesi veya çalışma garantisi değildir.', '',
              'ZK TCP/UDP adaptöründeki komutlar uygulanmıştır. Perkotek kataloglarında TCP/IP yazması, ZK protokolü kullandığını kanıtlamaz. USB / RS485 terminalleri veya farklı SDK kullanan cihazlar için ayrı adaptör gerekir. Araştırma kayıtları uygulamanın Uyumluluk sekmesinde de görünür.', '',
              '| Marka | Model | Belgelenen bağlantı | Kaynak |', '| --- | --- | --- | --- |']
    for model in models:
        report.append(f'| {model["brand"]} | {model["models"].replace("|", "/")} | {model["protocol"]} | [Üretici]({model["source"]}) |')
    report += ['', '## Araştırma sınırları', '', 'Her model için firmware ve üretici SDK erişimi ayrıca gerekir. ZKTeco sitesinin dinamik listelemeleri ve katalogdan kaldırılmış cihazlar bu envanterde eksik kalabilir. SDK indirme sayfalarındaki üyelik/lisans gereksinimleri aşılmadı.']
    if failures:
        report += ['', 'Okunamayan kaynaklar:', ''] + [f'- {row["source"]} — {row["error"]}' for row in failures]
    (root / 'docs/device-compatibility.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('Resmî kaynaklardan araştırılan modeller:', len(models), {brand: sum(m['brand'] == brand for m in models) for brand in sources}, 'okunamayan kaynak:', len(failures))


if __name__ == '__main__':
    main()

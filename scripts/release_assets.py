"""Stage immutable 1 MiB parts; the public download remains one hash-verified EXE."""
import hashlib
import json
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def test_chunk_file(stem):
    content = ('MZ-chunk-fixture-' + stem).encode() + b'x' * CHUNK_SIZE
    digest = hashlib.sha256(content).hexdigest()
    return content, {'name': stem + '.exe', 'path': f'/updates/files/{stem}-0.0.2-{digest[:12]}.exe', 'size': len(content), 'sha256': digest}


def clear_test_assets(root=Path('public')):
    for stem in ('Parmakizi', 'ParmakiziUpdater'):
        content, file = test_chunk_file(stem)
        directory = root / 'updates/chunks' / Path(file['path']).name.removesuffix('.exe')
        index = directory / 'index.json'
        if not index.exists():
            continue
        plan = json.loads(index.read_text())
        if plan.get('sha256') != file['sha256'] or plan.get('size') != len(content):
            raise ValueError('Yerel test varlığı beklenen içerik değil; silinmedi.')
        for number in range(plan['parts']):
            part = directory / f'{number:06d}.bin'
            if part.read_bytes() != content[number * CHUNK_SIZE:(number + 1) * CHUNK_SIZE]:
                raise ValueError('Yerel test parçası beklenen içerik değil; silinmedi.')
        for number in range(plan['parts']):
            (directory / f'{number:06d}.bin').unlink()
        index.unlink()


def stage_file(source, file, root=Path('public')):
    source = Path(source)
    prefix = Path(file['path']).name.removesuffix('.exe')
    directory = root / 'updates/chunks' / prefix
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    count = 0
    with source.open('rb') as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
            path = directory / f'{count:06d}.bin'
            if path.exists() and path.read_bytes() != chunk:
                raise ValueError('Mevcut güncelleme parçasının üzerine yazılmadı.')
            if not path.exists():
                path.write_bytes(chunk)
            count += 1
    if digest.hexdigest() != file['sha256'] or source.stat().st_size != file['size']:
        raise ValueError('Kaynak güncelleme dosyası değişmiş.')
    plan = {'format': 1, 'size': file['size'], 'sha256': file['sha256'], 'chunkSize': CHUNK_SIZE, 'parts': count}
    index = directory / 'index.json'
    if index.exists() and json.loads(index.read_text()) != plan:
        raise ValueError('Mevcut güncelleme planının üzerine yazılmadı.')
    if not index.exists():
        index.write_text(json.dumps(plan), encoding='utf-8')
    # Convert only the identical flat asset previously staged by our publisher.
    flat = root / file['path'].lstrip('/')
    if flat.exists():
        if hashlib.sha256(flat.read_bytes()).hexdigest() != file['sha256']:
            raise ValueError('Mevcut düz dosya beklenen varlık değil; korunuyor.')
        flat.unlink()


def retained(file, root=Path('public')):
    prefix = Path(file['path']).name.removesuffix('.exe')
    directory = root / 'updates/chunks' / prefix
    index = directory / 'index.json'
    if not index.exists():
        return False
    plan = json.loads(index.read_text())
    if plan.get('sha256') != file['sha256'] or plan.get('size') != file['size']:
        raise ValueError('Mevcut güncelleme planı doğrulanamadı.')
    digest = hashlib.sha256()
    size = 0
    for part in range(plan['parts']):
        path = directory / f'{part:06d}.bin'
        if not path.exists():
            return False
        content = path.read_bytes()
        size += len(content)
        digest.update(content)
    if digest.hexdigest() != file['sha256'] or size != file['size']:
        raise ValueError('Mevcut güncelleme parçaları doğrulanamadı.')
    return True

"""Signed updates from the configured Cloudflare origin, with no executable URLs from elsewhere."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gateway.client import ApiError, CloudClient, NoRedirect
from gateway.version import VERSION

NAMES = ('Parmakizi.exe', 'ParmakiziUpdater.exe')
MAX_FILE = 25 * 1024 * 1024


def application_launcher_pid():
    """PyInstaller one-file apps have a launcher and a Python child; wait for both."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return os.getpid()
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    parent = os.getppid()
    handle = kernel.OpenProcess(0x1000, False, parent)
    if not handle:
        return os.getpid()
    try:
        size = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) and Path(image.value).resolve() == Path(sys.executable).resolve():
            return parent
        return os.getpid()
    finally:
        kernel.CloseHandle(handle)


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})\.(0|[1-9]\d{0,2})', value):
        raise ValueError('Güncelleme sürüm numarası geçersiz.')
    return tuple(map(int, value.split('.')))


def public_key_bytes():
    return Path(__file__).with_name('update-key.pem').read_bytes()


def verify_manifest(envelope, public_key=None):
    try:
        payload = envelope['payload']
        if not isinstance(payload, str) or len(payload.encode()) > 16000:
            raise ValueError()
        key = serialization.load_pem_public_key(public_key or public_key_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError()
        key.verify(base64.b64decode(envelope['signature'], validate=True), payload.encode())
        data = json.loads(payload)
        version_tuple(data['version'])
        files = data['files']
        if len(files) != 2 or {f['name'] for f in files} != set(NAMES):
            raise ValueError()
        for file in files:
            digest = file['sha256']
            if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
                raise ValueError()
            expected = f'/updates/files/{file["name"][:-4]}-{data["version"]}-{digest[:12]}.exe'
            if file['path'] != expected or type(file['size']) is not int or not 1 <= file['size'] <= MAX_FILE:
                raise ValueError()
        return data
    except (KeyError, TypeError, ValueError, InvalidSignature):
        raise ValueError('Güncelleme imzası veya paket bilgileri doğrulanamadı; dosyalar çalıştırılmadı.') from None


def check_file(path, file):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != file['size']:
        raise ValueError('Güncelleme dosyasının boyutu veya konumu doğrulanamadı.')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b''):
            digest.update(chunk)
    if digest.hexdigest() != file['sha256']:
        raise ValueError('Güncelleme dosyasının SHA-256 özeti eşleşmiyor.')


class UpdateClient:
    def __init__(self, origin, token=None, auth_client=None, public_key=None, allow_local=False):
        self.client = CloudClient(origin, token=token, allow_local=allow_local)
        self.public_key = public_key
        handlers = [NoRedirect()]
        if auth_client:
            if auth_client.base_url != self.client.base_url:
                raise ValueError('Güncelleme ve oturum adresi aynı olmalı.')
            handlers.append(urllib.request.HTTPCookieProcessor(auth_client.cookies))
        self.opener = urllib.request.build_opener(*handlers)
        self.token = token

    def latest(self, current=VERSION):
        envelope = self.client.request('/api/updates/latest')
        if not isinstance(envelope, dict):
            raise ValueError('Güncelleme yanıtı geçersiz.')
        if envelope.get('update', False) is None:
            return None
        data = verify_manifest(envelope, self.public_key)
        return envelope if version_tuple(data['version']) > version_tuple(current) else None

    def download(self, envelope, directory):
        data = verify_manifest(envelope, self.public_key)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        for file in data['files']:
            target = directory / file['name']
            if target.exists():
                check_file(target, file)
                continue
            temporary = directory / (file['name'] + '.part')
            headers = {'Accept': 'application/octet-stream', 'User-Agent': 'Parmakizi/' + VERSION}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            request = urllib.request.Request(self.client.base_url + file['path'], headers=headers)
            created = False
            try:
                with self.opener.open(request, timeout=30) as response:
                    # Redirects are rejected by the opener; only this origin receives credentials.
                    if response.status != 200:
                        raise ValueError('Güncelleme indirme isteği başarısız.')
                    with temporary.open('xb') as output:
                        created = True
                        size = 0
                        while chunk := response.read(128 * 1024):
                            size += len(chunk)
                            if size > file['size']:
                                raise ValueError('Güncelleme dosyası bildirilen boyutu aşıyor.')
                            output.write(chunk)
                check_file(temporary, file)
                temporary.replace(target)
            finally:
                if created:
                    temporary.unlink(missing_ok=True)
        return data


def stage_update(client, envelope, target_executable):
    target = Path(target_executable).resolve()
    if target.name != 'Parmakizi.exe':
        raise ValueError('Klasör içi güncelleme için uygulama dosyasının adı Parmakizi.exe olmalı.')
    data = verify_manifest(envelope, client.public_key)
    if version_tuple(data['version']) <= version_tuple(VERSION):
        raise ValueError('Eski veya aynı sürüm kurulmaz.')
    stage = target.parent / '.parmakizi-updates' / (data['version'] + '-' + uuid.uuid4().hex)
    if stage.parent.is_symlink():
        raise ValueError('Güncelleme klasörü bir bağlantı olamaz.')
    client.download(envelope, stage)
    job = {'envelope': envelope, 'target_dir': str(target.parent), 'parent_pid': application_launcher_pid(), 'stage_dir': str(stage)}
    job_path = stage / 'job.json'
    job_path.write_text(json.dumps(job), encoding='utf-8')
    return job_path


def launch_update(job_path):
    path = Path(job_path).resolve()
    # This helper was downloaded and verified as part of the signed manifest.
    job = json.loads(path.read_text(encoding='utf-8'))
    data = verify_manifest(job['envelope'])
    helper = next(f for f in data['files'] if f['name'] == 'ParmakiziUpdater.exe')
    check_file(path.parent / helper['name'], helper)
    subprocess.Popen([str(path.parent / helper['name']), '--job', str(path)], close_fds=True, env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'})

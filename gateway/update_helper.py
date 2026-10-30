"""Standalone Windows update helper: wait, install two verified files, acknowledge boot, roll back."""
import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from gateway.updater import NAMES, check_file, verify_manifest
from gateway.updater import public_key_bytes
from gateway.version import VERSION


def wait_for_parent(pid, timeout=60):
    if os.name != 'nt':
        raise OSError('Güncelleyici Windows gerektirir.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # The parent already exited.
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, timeout * 1000) != 0:
            raise TimeoutError('Uygulama kapanmadığı için güncelleme uygulanmadı.')
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def update_lock(target):
    path = target / '.parmakizi-updates' / '.lock'
    if path.is_symlink():
        raise ValueError('Güncelleme kilidi bir bağlantı olamaz.')
    with path.open('a+b') as lock:
        if lock.tell() == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == 'nt':
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def apply_update(job_path, wait_parent=True, public_key=None, boot_timeout=30):
    path = Path(job_path).resolve()
    if path.parent.parent.name != '.parmakizi-updates':
        raise ValueError('Güncelleme klasörü doğrulanamadı.')
    with update_lock(path.parent.parent.parent):
        return install_update(path, wait_parent, public_key, boot_timeout)


def install_update(job_path, wait_parent=True, public_key=None, boot_timeout=30):
    path = Path(job_path).resolve()
    job = json.loads(path.read_text(encoding='utf-8'))
    data = verify_manifest(job['envelope'], public_key)
    if data['version'] != VERSION and public_key is None:
        raise ValueError('Güncelleyici ile paket sürümü uyuşmuyor.')
    stage = Path(job['stage_dir']).resolve()
    target = Path(job['target_dir']).resolve()
    if stage != path.parent or stage.parent.name != '.parmakizi-updates' or stage.parent.parent != target:
        raise ValueError('Güncelleme klasörü doğrulanamadı.')
    for file in data['files']:
        check_file(stage / file['name'], file)
    if wait_parent:
        wait_for_parent(int(job['parent_pid']))
    # Verify again after waiting; no different content may be installed.
    for file in data['files']:
        check_file(stage / file['name'], file)
    backup = target / '.parmakizi-updates' / ('backup-' + uuid.uuid4().hex)
    backup.mkdir()
    acknowledgement = stage / 'boot.json'
    result_path = stage / 'result.json'
    installed = []
    moved = []
    temporaries = []
    child = None
    try:
        for name in NAMES:
            current = target / name
            if current.is_symlink():
                raise ValueError('Uygulama dosyası bir bağlantı olamaz.')
            if current.exists():
                current.replace(backup / name)
                moved.append(name)
            temporary = target / ('.' + name + '.installing')
            if temporary.exists():
                raise ValueError('Önceki bir kurulum dosyası mevcut; değişiklik yapılmadı.')
            temporaries.append(temporary)
            shutil.copyfile(stage / name, temporary)
            temporary.replace(current)
            installed.append(name)
        child = subprocess.Popen([str(target / 'Parmakizi.exe'), '--update-ack', str(acknowledgement)], close_fds=True)
        deadline = time.monotonic() + boot_timeout
        while time.monotonic() < deadline:
            if acknowledgement.exists():
                ack = json.loads(acknowledgement.read_text(encoding='utf-8'))
                if ack.get('version') == data['version'] and ack.get('pid') == child.pid:
                    result_path.write_text(json.dumps({'ok': True, 'version': data['version'], 'pid': child.pid, 'backup': str(backup)}), encoding='utf-8')
                    return True
            if child.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError('Yeni uygulama açılışını doğrulamadı.')
    except Exception as error:
        if child and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
        for name in installed:
            (target / name).unlink(missing_ok=True)
        for name in moved:
            (backup / name).replace(target / name)
        result_path.write_text(json.dumps({'ok': False, 'error_type': type(error).__name__, 'restored': bool(moved)}), encoding='utf-8')
        if (target / 'Parmakizi.exe').exists() and wait_parent:
            subprocess.Popen([str(target / 'Parmakizi.exe')], close_fds=True)
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job')
    parser.add_argument('--self-test-report')
    args = parser.parse_args()
    if args.self_test_report:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        import hashlib
        public = public_key_bytes()
        valid = isinstance(serialization.load_pem_public_key(public), Ed25519PublicKey)
        Path(args.self_test_report).write_text(json.dumps({'ok': valid, 'version': VERSION, 'public_key_sha256': hashlib.sha256(public).hexdigest()}), encoding='utf-8')
        raise SystemExit(0 if valid else 1)
    if not args.job:
        parser.error('--job gerekiyor')
    raise SystemExit(0 if apply_update(args.job) else 1)


if __name__ == '__main__':
    main()

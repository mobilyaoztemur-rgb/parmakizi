"""Use Windows DPAPI (current user), without embedding a reusable encryption key."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]


def dpapi(data, decrypt=False):
    if os.name != "nt":
        raise OSError("Kalıcı anahtar saklama Windows DPAPI gerektirir.")
    buffer = ctypes.create_string_buffer(data)
    incoming = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    outgoing = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = (ctypes.byref(incoming), "Parmakizi", None, None, None, 1, ctypes.byref(outgoing))
    fn.restype = wintypes.BOOL
    if not fn(*args):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def config_path():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".config"))) / "Parmakizi" / "config.json"


def load_settings(path=None):
    path = path or config_path()
    if not path.exists():
        return {}, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    sealed = data.pop("protected", None)
    secrets = json.loads(dpapi(base64.b64decode(sealed), decrypt=True)) if sealed else {}
    return data, secrets


def save_settings(preferences, secrets, path=None):
    path = path or config_path()
    data = dict(preferences)
    if os.name == "nt" and secrets:
        data["protected"] = base64.b64encode(dpapi(json.dumps(secrets).encode())).decode()
    # Non-Windows development never falls back to writing plaintext credentials.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)

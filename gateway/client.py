"""HTTPS-only cloud client; never redirects credentials to another host."""
from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class CloudClient:
    def __init__(self, base_url, token=None, allow_local=False):
        parsed = urllib.parse.urlsplit(base_url)
        local = allow_local and parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
            raise ValueError("Bulut adresi HTTPS olmalı.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Bulut adresi yalnızca sunucu adresini içermeli.")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, data=None, retries=0):
        if not path.startswith("/api/") or path.startswith("//"):
            raise ValueError("Geçersiz API yolu.")
        payload = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        headers = {"Accept": "application/json", "Origin": self.base_url}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(self.base_url + path, data=payload, headers=headers)
                with self.opener.open(req, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                try:
                    message = json.load(exc).get("error", "API hatası")
                except (ValueError, AttributeError):
                    message = f"API HTTP {exc.code}"
                if exc.code >= 500 and attempt < retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise ApiError(exc.code, message) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                if attempt == retries:
                    raise ApiError(0, "Bulut sunucusuna ulaşılamadı; bağlantıyı ve HTTPS adresini kontrol edin.") from None
                time.sleep(min(2 ** attempt, 8))

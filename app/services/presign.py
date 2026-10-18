"""HMAC-signed public file URLs used by IM channel replies."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse


_DEFAULT_TTL = 2 * 60 * 60
_EXAMPLE_KEYS = {
    "weknora-system-aes-key-32bytes!!",
    "your-32-byte-long-encryption-key!",
}


def _key() -> bytes | None:
    value = os.getenv("SYSTEM_SIGNING_KEY") or os.getenv("SYSTEM_AES_KEY") or ""
    if len(value) < 16 or value in _EXAMPLE_KEYS:
        return None
    return value.encode("utf-8")


def _signature(file_path: str, tenant_id: int, expires: int) -> str:
    key = _key()
    if key is None:
        raise ValueError("presign signing key is not configured")
    payload = f"file_path={file_path}&tenant_id={int(tenant_id)}&expires={int(expires)}"
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_file_url(
    base_url: str, file_path: str, tenant_id: int, ttl_seconds: int = _DEFAULT_TTL
) -> str:
    key = _key()
    if key is None:
        raise ValueError("presign signing key is not configured")
    ttl = ttl_seconds if ttl_seconds > 0 else _DEFAULT_TTL
    expires = int(time.time()) + int(ttl)
    query = urllib.parse.urlencode(
        {
            "file_path": file_path,
            "tenant_id": int(tenant_id),
            "expires": expires,
            "sig": _signature(file_path, tenant_id, expires),
        }
    )
    return base_url.rstrip("/") + "/api/v1/files/presigned?" + query


def verify_file_url(file_path: str, tenant_id: int, expires: str, signature: str) -> bool:
    key = _key()
    if key is None:
        return False
    try:
        expires_at = int(expires)
    except (TypeError, ValueError):
        return False
    if time.time() > expires_at:
        return False
    try:
        expected = _signature(file_path, tenant_id, expires_at)
    except ValueError:
        return False
    return hmac.compare_digest(expected, str(signature or ""))

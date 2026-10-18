"""Password hashing and compact HS256 JWT support."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from . import config


PBKDF2_ITERATIONS = 260_000


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), _b64d(salt), int(rounds)
        )
        return hmac.compare_digest(digest, _b64d(expected))
    except (ValueError, TypeError):
        return False


def issue_token(
    subject: str,
    tenant_id: int | None,
    token_type: str = "access",
    ttl: int | None = None,
    extra: dict[str, Any] | None = None,
) -> tuple[str, int]:
    now = int(time.time())
    exp = now + (ttl or (config.ACCESS_TOKEN_TTL if token_type == "access" else config.REFRESH_TOKEN_TTL))
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": subject,
        "tenant_id": tenant_id,
        "typ": token_type,
        "iat": now,
        "exp": exp,
        "jti": os.urandom(12).hex(),
    }
    if extra:
        payload.update(extra)
    parts = [
        _b64e(json.dumps(header, separators=(",", ":")).encode()),
        _b64e(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(parts).encode("ascii")
    signature = hmac.new(config.jwt_secret().encode(), signing_input, hashlib.sha256).digest()
    return ".".join(parts + [_b64e(signature)]), exp


def decode_token(token: str, expected_type: str | None = None) -> dict[str, Any]:
    try:
        head, body, signature = token.split(".")
        signing_input = f"{head}.{body}".encode("ascii")
        expected = hmac.new(config.jwt_secret().encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64d(signature)):
            raise ValueError("bad signature")
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) <= int(time.time()):
            raise ValueError("expired")
        if expected_type and payload.get("typ") != expected_type:
            raise ValueError("wrong token type")
        return payload
    except Exception as exc:  # noqa: BLE001 - normalised into a security error
        raise ValueError("invalid or expired token") from exc

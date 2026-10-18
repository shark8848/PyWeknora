"""WeKnoraCloud credential storage helpers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..utils import loads


class CloudError(RuntimeError):
    pass


def base_url() -> str:
    return str(os.getenv("WEKNORA_CLOUD_BASE_URL") or "https://weknora.weixin.qq.com").rstrip("/")


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _signature_headers(app_id: str, app_secret: str, request_id: str, body: str = "{}") -> dict[str, str]:
    timestamp = str(int(time.time()))
    alphabet = string.ascii_letters + string.digits
    nonce = "".join(secrets.choice(alphabet) for _ in range(16))
    params = {
        "x-appid": app_id,
        "x-api-key": app_secret,
        "x-request-id": request_id,
        "x-timestamp": timestamp,
        "x-nonce": nonce,
        "body": _md5(body),
    }
    canonical = "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(params[key], safe='-_.~')}"
        for key in sorted(params)
    )
    return {
        "X-APPID": app_id,
        "X-API-Key": app_secret,
        "X-Request-ID": request_id,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": _md5(canonical),
    }


def verify_credentials(app_id: str, app_secret: str) -> None:
    request_id = f"verify-{time.time_ns()}"
    request = urllib.request.Request(
        f"{base_url()}/api/v1/health",
        headers=_signature_headers(app_id, app_secret, request_id),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status in {401, 403}:
                raise CloudError("invalid APPID or APPSECRET")
            if response.status != 200:
                raise CloudError(f"invalid response status code: {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise CloudError("invalid APPID or APPSECRET") from exc
        raise CloudError(f"invalid response status code: {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"service unreachable: {exc}") from exc


def credentials_from_tenant(tenant: dict[str, Any]) -> dict[str, str] | None:
    raw = loads(tenant.get("credentials"), {})
    if not isinstance(raw, dict):
        return None
    cloud = raw.get("weknoracloud")
    if not isinstance(cloud, dict):
        return None
    app_id = str(cloud.get("app_id") or "").strip()
    app_secret = str(cloud.get("app_secret") or "")
    if not app_id or not app_secret:
        return None
    return {"app_id": app_id, "app_secret": app_secret}


def merge_credentials(tenant: dict[str, Any], app_id: str, app_secret: str) -> str:
    raw = loads(tenant.get("credentials"), {})
    credentials = raw if isinstance(raw, dict) else {}
    credentials["weknoracloud"] = {"app_id": app_id, "app_secret": app_secret}
    return json.dumps(credentials, ensure_ascii=False)


def status(tenant: dict[str, Any]) -> dict[str, Any]:
    raw = loads(tenant.get("credentials"), {})
    if not isinstance(raw, dict) or not isinstance(raw.get("weknoracloud"), dict):
        return {"has_models": False, "needs_reinit": False}
    cloud = raw["weknoracloud"]
    if str(cloud.get("app_id") or "") and str(cloud.get("app_secret") or ""):
        return {"has_models": True, "needs_reinit": False}
    return {
        "has_models": False,
        "needs_reinit": True,
        "reason": "WeKnoraCloud 凭证不完整，请重新填写 APPID 和 APPSECRET",
    }

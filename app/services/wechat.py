"""WeChat iLink QR-code login helpers."""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
POLL_TIMEOUT = 38


class WeChatError(RuntimeError):
    pass


def _http_json(url: str, *, headers: dict[str, str] | None = None, timeout: float = 15) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (socket.timeout, TimeoutError) as exc:
        raise TimeoutError("wechat request timed out") from exc
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise WeChatError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise TimeoutError("wechat request timed out") from exc
        raise WeChatError(f"connection failed: {exc.reason}") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise WeChatError("wechat endpoint returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise WeChatError("wechat endpoint returned a non-object response")
    return value


def get_login_qrcode() -> dict[str, str]:
    query = urllib.parse.urlencode({"bot_type": BOT_TYPE})
    result = _http_json(f"{BASE_URL}/ilink/bot/get_bot_qrcode?{query}")
    qrcode = str(result.get("qrcode") or "")
    if not qrcode:
        raise WeChatError("wechat qrcode response is missing qrcode")
    return {
        "qrcode_url": str(result.get("qrcode_img_content") or ""),
        "qrcode": qrcode,
    }


def poll_qrcode_status(qrcode: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"qrcode": qrcode})
    try:
        result = _http_json(
            f"{BASE_URL}/ilink/bot/get_qrcode_status?{query}",
            headers={"iLink-App-ClientVersion": "1"},
            timeout=POLL_TIMEOUT,
        )
    except TimeoutError:
        return {"status": "wait"}
    status = str(result.get("status") or "wait")
    payload: dict[str, Any] = {"status": status}
    if status == "confirmed":
        payload["credentials"] = {
            "bot_token": str(result.get("bot_token") or ""),
            "ilink_bot_id": str(result.get("ilink_bot_id") or ""),
            "ilink_user_id": str(result.get("ilink_user_id") or ""),
        }
        if result.get("baseurl"):
            payload["baseurl"] = str(result["baseurl"])
    return payload

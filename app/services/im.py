"""IM callback parsing and reply shaping shared by webhook endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from typing import Any

from ..utils import loads
from . import url_guard


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "content", "query"):
            if isinstance(value.get(key), str):
                return str(value[key]).strip()
    return ""


def parse_message(platform: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    platform = str(platform or "webhook").lower()
    if platform in {"feishu", "lark"}:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = loads(message.get("content"), {})
        text = _text(content)
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        return {
            "content": text,
            "user_id": str(sender_id.get("open_id") or sender_id.get("user_id") or sender.get("sender_id") or ""),
            "user_name": str(sender.get("sender_type") or ""),
            "chat_id": str(message.get("chat_id") or ""),
            "thread_id": str(message.get("root_id") or ""),
            "message_id": str(message.get("message_id") or payload.get("uuid") or ""),
        }
    if platform == "slack":
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return None
        text = str(event.get("text") or "").strip()
        text = _strip_mentions(text)
        return {
            "content": text,
            "user_id": str(event.get("user") or ""),
            "user_name": str(event.get("username") or ""),
            "chat_id": str(event.get("channel") or ""),
            "thread_id": str(event.get("thread_ts") or ""),
            "message_id": str(event.get("ts") or payload.get("event_id") or ""),
        }
    if platform == "telegram":
        message = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        return {
            "content": str(message.get("text") or message.get("caption") or "").strip(),
            "user_id": str(sender.get("id") or ""),
            "user_name": str(sender.get("username") or sender.get("first_name") or ""),
            "chat_id": str(chat.get("id") or ""),
            "thread_id": str(message.get("message_thread_id") or ""),
            "message_id": str(message.get("message_id") or ""),
            "reply_url": "",
            "chat_type": str(chat.get("type") or ""),
        }
    if platform in {"wecom", "dingtalk", "mattermost", "wechat", "qqbot", "yunzhijia", "webhook", "generic"}:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        merged = {**event, **data, **payload}
        text = _text(merged.get("text") or merged.get("content") or merged.get("query") or merged.get("message"))
        if not text and isinstance(merged.get("message"), dict):
            text = _text(merged["message"])
        user = merged.get("user_id") or merged.get("from_user") or merged.get("sender_id") or merged.get("user")
        chat = merged.get("chat_id") or merged.get("conversation_id") or merged.get("room_id") or merged.get("channel_id")
        return {
            "content": text,
            "user_id": str(user.get("id") if isinstance(user, dict) else user or ""),
            "user_name": str(merged.get("user_name") or ""),
            "chat_id": str(chat.get("id") if isinstance(chat, dict) else chat or ""),
            "thread_id": str(merged.get("thread_id") or merged.get("root_id") or ""),
            "message_id": str(merged.get("message_id") or merged.get("event_id") or merged.get("id") or ""),
            "reply_url": str(merged.get("sessionWebhook") or merged.get("session_webhook") or merged.get("reply_url") or ""),
            "chat_type": str(merged.get("conversationType") or merged.get("chat_type") or ""),
        }
    return None


def _strip_mentions(text: str) -> str:
    parts = []
    for token in text.split():
        if token.startswith("<@") and token.endswith(">"):
            continue
        parts.append(token)
    return " ".join(parts).strip()


def challenge_response(platform: str, payload: dict[str, Any], query: dict[str, str]) -> Any | None:
    challenge = payload.get("challenge") or query.get("challenge") or query.get("echostr")
    if challenge:
        if str(platform or "").lower() in {"feishu", "lark", "slack"}:
            return {"challenge": challenge}
        return challenge
    if str(platform or "").lower() == "slack" and payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge") or ""}
    return None


def reply_payload(platform: str, content: str) -> dict[str, Any]:
    platform = str(platform or "").lower()
    if platform == "slack":
        return {"text": content}
    if platform in {"feishu", "lark"}:
        return {"msg_type": "text", "content": {"text": content}}
    return {"text": content}


def event_kind(platform: str, payload: dict[str, Any]) -> str:
    platform = str(platform or "").lower()
    if challenge_response(platform, payload, {}):
        return "challenge"
    if platform == "slack":
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        return "message" if event.get("type") in {"message", "app_mention"} else "ignored"
    if platform in {"feishu", "lark"}:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        return "message" if message else "ignored"
    return "message"


def dedupe_key(platform: str, message: dict[str, Any]) -> str:
    message_id = str(message.get("message_id") or "")
    return f"{platform}:{message_id}" if message_id else ""


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _header(headers: Any, name: str) -> str:
    lowered = name.lower()
    try:
        for key, value in headers.items():
            if str(key).lower() == lowered:
                return str(value)
    except AttributeError:
        return ""
    return ""


def verify_callback(
    platform: str,
    credentials: dict[str, Any],
    headers: Any,
    raw_body: bytes,
    payload: dict[str, Any],
    *,
    method: str = "POST",
) -> bool:
    platform = str(platform or "").lower()
    if platform in {"feishu", "lark"}:
        expected = str(credentials.get("verification_token") or "")
        if not expected:
            return False
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        supplied = str(header.get("token") or payload.get("token") or "")
        return bool(supplied) and hmac.compare_digest(supplied, expected)
    if platform == "slack":
        secret = str(credentials.get("signing_secret") or "")
        timestamp = _header(headers, "X-Slack-Request-Timestamp")
        signature = _header(headers, "X-Slack-Signature")
        if not secret or not timestamp or not signature:
            return False
        try:
            if abs(time.time() - int(timestamp)) > 300:
                return False
        except ValueError:
            return False
        base = b"v0:" + timestamp.encode() + b":" + raw_body
        expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    if platform == "telegram":
        expected = str(credentials.get("secret_token") or "")
        supplied = _header(headers, "X-Telegram-Bot-Api-Secret-Token")
        return bool(expected and supplied) and hmac.compare_digest(supplied, expected)
    if platform == "mattermost":
        expected = str(credentials.get("outgoing_token") or "")
        supplied = str(payload.get("token") or "")
        return bool(expected and supplied) and hmac.compare_digest(supplied, expected)
    if platform == "yunzhijia":
        secret = str(credentials.get("secret") or "")
        supplied = _header(headers, "sign")
        if not secret or not supplied:
            return False
        fields = [
            str(payload.get("robotId") or payload.get("robot_id") or ""),
            str(payload.get("robotName") or payload.get("robot_name") or ""),
            str(payload.get("operatorOpenid") or payload.get("operator_openid") or ""),
            str(payload.get("operatorName") or payload.get("operator_name") or ""),
            str(payload.get("time") or ""),
            str(payload.get("msgId") or payload.get("msg_id") or ""),
            str(payload.get("content") or ""),
        ]
        digest = hmac.new(secret.encode(), ",".join(fields).encode(), hashlib.sha1).digest()
        expected = base64.b64encode(digest).decode()
        return hmac.compare_digest(supplied, expected)
    if platform == "wecom":
        token = str(credentials.get("token") or "")
        query = payload.get("_query") if isinstance(payload.get("_query"), dict) else {}
        timestamp = str(query.get("timestamp") or "")
        nonce = str(query.get("nonce") or "")
        supplied = str(query.get("msg_signature") or "")
        encrypt = str(payload.get("Encrypt") or payload.get("encrypt") or query.get("echostr") or "")
        if not token or not timestamp or not nonce or not supplied:
            return False
        raw = "".join(sorted([token, timestamp, nonce, encrypt]))
        expected = hashlib.sha1(raw.encode()).hexdigest()
        return hmac.compare_digest(supplied, expected)
    if platform in {"dingtalk", "qqbot", "wechat"}:
        return False
    secret = str(credentials.get("signing_secret") or credentials.get("secret") or "")
    if not secret:
        return True
    supplied = _header(headers, "X-Webhook-Signature") or _header(headers, "X-Webhook-Token")
    if not supplied:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if supplied.startswith("sha256="):
        supplied = supplied.split("=", 1)[1]
    return hmac.compare_digest(supplied, expected) or hmac.compare_digest(supplied, secret)


def _http_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    try:
        status, _response_headers, raw, _final_url = url_guard.request_url(
            method,
            url,
            headers=request_headers,
            body=data,
            timeout=timeout,
        )
    except url_guard.URLGuardError as exc:
        raise RuntimeError(f"connection failed: {exc}") from exc
    if status >= 400:
        detail = raw.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {status}: {detail}")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def send_reply(
    platform: str,
    credentials: dict[str, Any],
    message: dict[str, Any],
    content: str,
) -> tuple[bool, str]:
    platform = str(platform or "").lower()
    try:
        if platform == "telegram":
            token = str(credentials.get("bot_token") or "")
            chat_id = str(message.get("chat_id") or message.get("user_id") or "")
            if not token or not chat_id:
                return False, "telegram bot_token/chat_id is not configured"
            _http_json(
                "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                {"chat_id": chat_id, "text": content, "parse_mode": "Markdown"},
            )
            return True, ""
        if platform == "slack":
            token = str(credentials.get("bot_token") or "")
            channel = str(message.get("chat_id") or message.get("user_id") or "")
            if not token or not channel:
                return False, "slack bot_token/channel is not configured"
            payload: dict[str, Any] = {"channel": channel, "text": content}
            if message.get("message_id"):
                payload["thread_ts"] = str(message["message_id"])
            _http_json("POST", "https://slack.com/api/chat.postMessage", payload, {"Authorization": "Bearer " + token})
            return True, ""
        if platform in {"feishu", "lark"}:
            app_id = str(credentials.get("app_id") or "")
            app_secret = str(credentials.get("app_secret") or "")
            base_url = str(credentials.get("api_base_url") or "https://open.feishu.cn").rstrip("/")
            receiver = str(message.get("chat_id") or message.get("user_id") or "")
            if not app_id or not app_secret or not receiver:
                return False, "feishu credentials/receiver is not configured"
            auth = _http_json(
                "POST",
                base_url + "/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": app_id, "app_secret": app_secret},
            )
            token = str(auth.get("tenant_access_token") or "")
            if not token:
                return False, "feishu tenant token was not returned"
            receiver_type = "chat_id" if message.get("chat_id") else "open_id"
            url = base_url + "/open-apis/im/v1/messages?" + urllib.parse.urlencode({"receive_id_type": receiver_type})
            _http_json(
                "POST",
                url,
                {"receive_id": receiver, "msg_type": "text", "content": json_text({"text": content})},
                {"Authorization": "Bearer " + token},
            )
            return True, ""
        if platform == "wecom":
            corp_id = str(credentials.get("corp_id") or "")
            secret = str(credentials.get("agent_secret") or credentials.get("corp_secret") or "")
            agent_id = credentials.get("corp_agent_id") or credentials.get("agent_id") or ""
            base_url = str(credentials.get("api_base_url") or "https://qyapi.weixin.qq.com").rstrip("/")
            if not corp_id or not secret or not agent_id:
                return False, "wecom credentials are not configured"
            auth = _http_json("GET", f"{base_url}/cgi-bin/gettoken?corpid={urllib.parse.quote(corp_id)}&corpsecret={urllib.parse.quote(secret)}")
            token = str(auth.get("access_token") or "")
            if not token:
                return False, "wecom access token was not returned"
            body: dict[str, Any] = {
                "msgtype": "text",
                "agentid": agent_id,
                "text": {"content": content},
            }
            if message.get("chat_id"):
                body["chatid"] = str(message["chat_id"])
            else:
                body["touser"] = str(message.get("user_id") or "@all")
            _http_json("POST", f"{base_url}/cgi-bin/message/send?access_token={urllib.parse.quote(token)}", body)
            return True, ""
        if platform == "mattermost":
            site_url = str(credentials.get("site_url") or "").rstrip("/")
            token = str(credentials.get("bot_token") or "")
            channel = str(message.get("chat_id") or message.get("user_id") or "")
            if not site_url or not token or not channel:
                return False, "mattermost credentials/channel is not configured"
            body = {"channel_id": channel, "message": content}
            if not bool(credentials.get("post_to_main")) and message.get("message_id"):
                body["root_id"] = str(message["message_id"])
            _http_json("POST", site_url + "/api/v4/posts", body, {"Authorization": "Bearer " + token})
            return True, ""
        if platform == "dingtalk":
            reply_url = str(message.get("reply_url") or "")
            parsed = urllib.parse.urlparse(reply_url)
            if parsed.scheme != "https" or parsed.netloc != "oapi.dingtalk.com" or parsed.path != "/robot/sendBySession":
                return False, "dingtalk session webhook is unavailable"
            _http_json(
                "POST",
                reply_url,
                {"msgtype": "markdown", "markdown": {"title": "Reply", "text": content}},
            )
            return True, ""
        if platform == "yunzhijia":
            send_url = str(credentials.get("send_msg_url") or "")
            parsed = urllib.parse.urlparse(send_url)
            allowed = str(credentials.get("allowed_webhook_host_suffix") or "yunzhijia.com")
            if parsed.scheme != "https" or not (parsed.hostname or "").endswith(allowed):
                return False, "yunzhijia send_msg_url is not trusted"
            _http_json(
                "POST",
                send_url,
                {
                    "msgType": "text",
                    "content": content,
                    "param": {"formatType": "markdown"},
                    "notifyParams": [{"type": "openIds", "values": [str(message.get("user_id") or "")]}],
                },
            )
            return True, ""
        reply_url = str(credentials.get("reply_url") or credentials.get("send_msg_url") or "")
        if reply_url:
            _http_json("POST", reply_url, {"text": content, "content": content})
            return True, ""
        return False, "no outbound reply transport is configured"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

"""Bridge the BrowserSkill extension and agent tool to one local daemon."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import db


AUTH_PROTOCOL = "bsk-auth."
MAX_FRAME = 8 * 1024 * 1024
LEASE_SECONDS = 45
DEFAULT_MAX_CONNECTIONS = 32
INTERNAL_PATH = "/api/v1/local-browser/internal"
NODE_ID = secrets.token_hex(16)
LOCAL_RPC_TIMEOUT = 35.0
CLUSTER_RPC_TIMEOUT = 120.0
HUMAN_STEP_TIMEOUT = 315.0
HUMAN_STEP_WAIT_MS = 300_000
HUMAN_STEP_METHODS = {"request_help", "tab_borrow"}

BROWSER_METHODS = {
    "observe",
    "snapshot",
    "screenshot",
    "navigate",
    "navigate_back",
    "navigate_forward",
    "reload",
    "click",
    "fill",
    "press",
    "hover",
    "wheel",
    "scroll_to",
    "focus",
    "blur",
    "select",
    "tab_list",
    "tab_create",
    "tab_select",
    "tab_close",
    "tab_borrow",
    "tab_return",
    "get_html",
    "evaluate",
    "console",
    "network",
    "wait_for_navigation",
    "wait_ms",
    "window_resize",
    "emulate",
    "request_help",
}


BROWSER_TOOL_PROPERTIES: dict[str, dict[str, Any]] = {
    "keep_open": {"type": "boolean"},
    "debug_surfaces": {"type": "boolean"},
    "max_depth": {"type": "integer", "minimum": 0},
    "probe_hover": {"type": "boolean"},
    "tab_id": {"type": "integer", "minimum": 1},
    "timeout_ms": {"type": "integer", "minimum": 1},
    "url": {"type": "string", "minLength": 1},
    "wait_until": {
        "type": "string",
        "enum": ["load", "domcontentloaded", "networkidle", "commit"],
    },
    "hard": {"type": "boolean"},
    "button": {"type": "string", "enum": ["left", "middle", "right"]},
    "click_count": {"type": "integer", "minimum": 1},
    "modifiers": {
        "type": "array",
        "items": {"type": "string", "enum": ["alt", "ctrl", "meta", "shift"]},
    },
    "ref": {"type": "string", "minLength": 1},
    "selector": {"type": "string", "minLength": 1},
    "clear_before": {"type": "boolean"},
    "value": {"type": "string"},
    "hold_ms": {"type": "integer", "minimum": 0},
    "key": {"type": "string", "minLength": 1},
    "settle_ms": {"type": "integer", "minimum": 0},
    "delta_x": {"type": "number"},
    "delta_y": {"type": "number"},
    "values": {"type": "array", "items": {"type": "string"}},
    "scope": {"type": "string", "enum": ["user", "agent", "all"]},
    "active": {"type": "boolean"},
    "index": {"type": "integer", "minimum": 0},
    "confirm": {"type": "boolean"},
    "max_bytes": {"type": "integer", "minimum": 0},
    "await_promise": {"type": "boolean"},
    "expression": {"type": "string", "minLength": 1},
    "return_by_value": {"type": "boolean"},
    "include_stack": {"type": "boolean"},
    "limit": {"type": "integer", "minimum": 1},
    "max_text_chars": {"type": "integer", "minimum": 1},
    "since": {"type": "integer", "minimum": 0},
    "duration_ms": {"type": "integer", "minimum": 0, "maximum": 10000},
    "height": {"type": "integer", "minimum": 100, "maximum": 7680},
    "width": {"type": "integer", "minimum": 100, "maximum": 7680},
    "off": {"type": "boolean"},
    "overrides": {
        "type": "object",
        "properties": {
            "accept_language": {"type": "string"},
            "device_scale_factor": {"type": "number"},
            "height": {"type": "integer", "minimum": 0},
            "max_touch_points": {"type": "integer", "minimum": 0},
            "mobile": {"type": "boolean"},
            "touch": {"type": "boolean"},
            "user_agent": {"type": "string"},
            "user_agent_metadata": {
                "type": "object",
                "properties": {
                    "architecture": {"type": "string"},
                    "brands": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["brand", "version"],
                            "properties": {
                                "brand": {"type": "string"},
                                "version": {"type": "string"},
                            },
                        },
                    },
                    "full_version": {"type": "string"},
                    "mobile": {"type": "boolean"},
                    "model": {"type": "string"},
                    "platform": {"type": "string"},
                    "platform_version": {"type": "string"},
                },
            },
            "width": {"type": "integer", "minimum": 0},
        },
    },
    "prompt": {"type": "string", "minLength": 1},
    "targets": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "selector": {"type": "string"}},
        },
    },
    "title": {"type": "string"},
}


BROWSER_ARGUMENT_RULES: dict[str, dict[str, set[str]]] = {
    "screenshot": {"fields": {"ref", "tab_id"}, "required": set()},
    "observe": {
        "fields": {"debug_surfaces", "max_depth", "max_text_chars", "probe_hover", "tab_id"},
        "required": set(),
    },
    "snapshot": {"fields": {"max_depth", "max_text_chars", "tab_id"}, "required": set()},
    "navigate": {
        "fields": {"tab_id", "timeout_ms", "url", "wait_until"},
        "required": {"url"},
    },
    "navigate_back": {"fields": {"tab_id", "timeout_ms", "wait_until"}, "required": set()},
    "navigate_forward": {"fields": {"tab_id", "timeout_ms", "wait_until"}, "required": set()},
    "reload": {"fields": {"hard", "tab_id", "timeout_ms", "wait_until"}, "required": set()},
    "click": {
        "fields": {"button", "click_count", "modifiers", "ref", "selector", "tab_id", "timeout_ms"},
        "required": set(),
    },
    "fill": {
        "fields": {"clear_before", "ref", "selector", "tab_id", "timeout_ms", "value"},
        "required": {"value"},
    },
    "press": {
        "fields": {"hold_ms", "key", "modifiers", "ref", "selector", "tab_id", "timeout_ms"},
        "required": {"key"},
    },
    "hover": {
        "fields": {"modifiers", "ref", "selector", "settle_ms", "tab_id", "timeout_ms"},
        "required": set(),
    },
    "wheel": {
        "fields": {"delta_x", "delta_y", "modifiers", "ref", "selector", "tab_id", "timeout_ms"},
        "required": set(),
    },
    "scroll_to": {"fields": {"ref", "selector", "tab_id", "timeout_ms"}, "required": set()},
    "focus": {"fields": {"ref", "selector", "tab_id", "timeout_ms"}, "required": set()},
    "blur": {"fields": {"ref", "selector", "tab_id", "timeout_ms"}, "required": set()},
    "select": {
        "fields": {"ref", "selector", "tab_id", "timeout_ms", "values"},
        "required": {"values"},
    },
    "tab_list": {"fields": {"scope"}, "required": set()},
    "tab_create": {"fields": {"active", "index", "url"}, "required": set()},
    "tab_select": {"fields": {"tab_id"}, "required": {"tab_id"}},
    "tab_close": {"fields": {"tab_id"}, "required": {"tab_id"}},
    "tab_borrow": {"fields": {"confirm", "tab_id"}, "required": {"tab_id"}},
    "tab_return": {"fields": {"tab_id"}, "required": {"tab_id"}},
    "get_html": {"fields": {"max_bytes", "ref", "tab_id"}, "required": set()},
    "evaluate": {
        "fields": {"await_promise", "expression", "return_by_value", "tab_id", "timeout_ms"},
        "required": {"expression"},
    },
    "console": {
        "fields": {"include_stack", "limit", "max_text_chars", "since", "tab_id"},
        "required": set(),
    },
    "network": {"fields": {"limit", "max_text_chars", "since", "tab_id"}, "required": set()},
    "wait_for_navigation": {"fields": {"tab_id", "timeout_ms", "wait_until"}, "required": set()},
    "wait_ms": {"fields": {"duration_ms"}, "required": {"duration_ms"}},
    "window_resize": {"fields": {"height", "width"}, "required": {"height", "width"}},
    "emulate": {"fields": {"off", "overrides", "tab_id"}, "required": set()},
    "request_help": {
        "fields": {"prompt", "tab_id", "targets", "timeout_ms", "title"},
        "required": {"prompt"},
    },
}


def _validate_browser_value(name: str, value: Any, schema: dict[str, Any]) -> None:
    kind = schema.get("type")
    valid = True
    if kind == "boolean":
        valid = isinstance(value, bool)
    elif kind == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif kind == "string":
        valid = isinstance(value, str)
    elif kind == "array":
        valid = isinstance(value, list)
    elif kind == "object":
        valid = isinstance(value, dict)
    if not valid:
        raise ValueError(f"{name} must be {kind}")
    if kind == "string":
        if schema.get("minLength") and len(value) < int(schema["minLength"]):
            raise ValueError(f"{name} must not be empty")
        enum = schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise ValueError(f"{name} must be one of {', '.join(str(item) for item in enum)}")
    elif kind in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{name} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{name} must be at most {schema['maximum']}")
    elif kind == "array":
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for item in value:
                _validate_browser_value(name, item, item_schema)
    elif kind == "object":
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for key, item in value.items():
                item_schema = properties.get(key)
                if isinstance(item_schema, dict):
                    _validate_browser_value(f"{name}.{key}", item, item_schema)
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    raise ValueError(f"{name} requires {key}")


def validate_browser_arguments(arguments: dict[str, Any]) -> None:
    if not isinstance(arguments, dict):
        raise ValueError("browser arguments must be an object")
    if "params" in arguments:
        raise ValueError(
            "params is not supported; place arguments alongside method, "
            'e.g. {"method":"navigate","url":"https://example.com"}'
        )
    method = arguments.get("method")
    if not isinstance(method, str) or not method:
        raise ValueError("method is required and must be a string")
    rule = BROWSER_ARGUMENT_RULES.get(method)
    if rule is None:
        raise ValueError(f"unsupported browser method {method!r}")
    allowed = rule["fields"]
    for name in arguments:
        if name not in {"method", "keep_open"} and name not in allowed:
            fields = ", ".join(sorted(allowed))
            raise ValueError(
                f"{method} does not accept argument {name!r}; "
                f"allowed fields: {fields} (plus keep_open)"
            )
    for name in rule["required"]:
        if name not in arguments or arguments[name] is None:
            raise ValueError(f"{method} requires {name} at the top level")
    _validate_browser_value("keep_open", arguments.get("keep_open", False), BROWSER_TOOL_PROPERTIES["keep_open"])
    for name in allowed:
        if name in arguments:
            _validate_browser_value(name, arguments[name], BROWSER_TOOL_PROPERTIES[name])
    has_ref = "ref" in arguments
    has_selector = "selector" in arguments
    if method in {"click", "fill", "hover", "scroll_to", "focus", "select"}:
        if has_ref == has_selector:
            raise ValueError(f"{method} requires exactly one of ref or selector")
    elif method in {"press", "wheel", "blur"} and has_ref and has_selector:
        raise ValueError(f"{method} accepts only one of ref or selector")
    if method == "emulate":
        off = arguments.get("off") is True
        has_overrides = "overrides" in arguments
        if off == has_overrides:
            raise ValueError("emulate requires either off:true or overrides")


def browser_call_params(method: str, arguments: dict[str, Any]) -> dict[str, Any]:
    params = {
        name: value
        for name, value in arguments.items()
        if name not in {"method", "keep_open"} and value is not None
    }
    if method in {"observe", "snapshot"} and "max_text_chars" in params:
        chars = int(params.pop("max_text_chars"))
        params["max_tokens"] = max(1, (chars + 3) // 4)
    return params


class BrowserSkillError(RuntimeError):
    def __init__(self, message: str, *, code: str = "", data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def bounded_error_data(data: Any) -> Any:
    """Match the Go RPCError.BoundDetails recovery-detail contract."""
    if data is None:
        return None
    try:
        size = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        size = 0
    if size <= 8192:
        return data
    bounded: dict[str, Any] = {"truncated": True}
    if isinstance(data, dict):
        for key in ("reason", "effect_state"):
            value = data.get(key)
            if isinstance(value, str) and len(value) <= 128:
                bounded[key] = value
    return bounded


class InternalRequestError(BrowserSkillError):
    def __init__(self, message: str, *, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


class LiveConnection:
    def __init__(self, scope: str, device_id: str, lease_key: str, upstream: Any) -> None:
        self.scope = scope
        self.device_id = device_id
        self.lease_key = lease_key
        self.upstream = upstream
        self.write_lock = asyncio.Lock()
        self.ui_replies: dict[str, asyncio.Future[str]] = {}

    async def send(self, raw: str) -> None:
        async with self.write_lock:
            await self.upstream.send(raw)


_daemon_lock = threading.Lock()
_daemon_process: subprocess.Popen[bytes] | None = None
_daemon_home = ""
_daemon_port = 0

_tasks_lock = threading.Lock()
_tasks: dict[str, dict[str, dict[str, Any]]] = {}
_session_locks: dict[tuple[str, str], threading.Lock] = {}
_live: dict[str, LiveConnection] = {}
_connection_lock = threading.Lock()
_connection_reservations = 0
_extension_versions: dict[tuple[str, str], str] = {}
_internal_lock = threading.Lock()
_seen_internal: dict[str, float] = {}


def enabled() -> bool:
    return bool(str(os.getenv("BROWSERSKILL_BINARY") or "").strip())


def valid_extension_origin(origin: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.scheme != "chrome-extension"
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    extension_id = parsed.netloc
    return len(extension_id) == 32 and all("a" <= char <= "p" for char in extension_id)


def _status_page_url(raw: Any) -> str:
    value = str(raw or "")
    if len(value) > 8192:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def _navigation_incomplete(method: str, result: dict[str, Any]) -> bool:
    return method in {
        "navigate",
        "navigate_back",
        "navigate_forward",
        "reload",
        "wait_for_navigation",
    } and result.get("reached") == "timeout"


def _action_elapsed_ms(task: dict[str, Any]) -> int:
    started = task.get("action_started")
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return 0
    finished = task.get("action_finished")
    if isinstance(finished, bool) or not isinstance(finished, (int, float)):
        finished = time.monotonic()
    return max(0, int((finished - started) * 1000))


def _finish_action_progress(
    task: dict[str, Any],
    method: str,
    result: dict[str, Any] | None,
    error_text: str,
) -> None:
    task["action_finished"] = time.monotonic()
    page = result if isinstance(result, dict) else {}
    page_url = str(page.get("final_url") or page.get("url") or "")
    if page_url:
        task["page_url"] = _status_page_url(page_url)
    message = str(error_text or "")
    if not message and _navigation_incomplete(method, page):
        message = str(page.get("error_text") or "")
        if not message:
            message = (
                "Navigation did not reach the requested loading phase. "
                "Observe the current page before continuing."
            )
    if not message and method == "evaluate" and page.get("ok") is False:
        failure = page.get("error")
        message = str(failure.get("text") or "") if isinstance(failure, dict) else ""
    task["last_error"] = message[:1200]
    if method in HUMAN_STEP_METHODS:
        task["help_calls"] = max(0, int(task.get("help_calls") or 0) - 1)
        task["help_prompt"] = ""


def _interrupted_command(exc: BrowserSkillError) -> bool:
    if exc.code in {"timeout", "cancelled", "user_aborted"}:
        return True
    message = str(exc)
    return any(
        needle in message
        for needle in ("user_aborted", "interrupted or timed out", "unfinished command")
    )


def scope_key(tenant_id: int, user_id: str) -> str:
    import hashlib

    return hashlib.sha256(f"{tenant_id}:{user_id}".encode()).hexdigest()[:32]


def token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


def _new_lease_key() -> str:
    return secrets.token_hex(16)


def _now() -> str:
    return db.utcnow()


def _timestamp(offset_seconds: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


def _internal_secret() -> str:
    return str(os.getenv("BROWSERSKILL_CLUSTER_SECRET") or "").strip()


def _internal_url() -> str:
    return str(os.getenv("BROWSERSKILL_INTERNAL_URL") or "").strip()


def internal_available() -> bool:
    return len(_internal_secret()) >= 32 and _valid_internal_url(_internal_url())


def _valid_internal_url(raw: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )


def sign_internal(timestamp: str, nonce: str, body: bytes) -> str:
    return hmac.new(
        _internal_secret().encode(),
        timestamp.encode() + b"\n" + nonce.encode() + body,
        hashlib.sha256,
    ).hexdigest()


def verify_internal(timestamp: str, nonce: str, signature: str, body: bytes) -> None:
    secret = _internal_secret()
    if len(secret) < 32:
        raise InternalRequestError("unavailable", status=404)
    try:
        issued = int(timestamp)
    except ValueError as exc:
        raise InternalRequestError("expired request", status=401) from exc
    if abs(time.time() - issued) > 60:
        raise InternalRequestError("expired request", status=401)
    try:
        supplied = bytes.fromhex(signature)
    except ValueError as exc:
        raise InternalRequestError("invalid signature", status=401) from exc
    expected = bytes.fromhex(sign_internal(timestamp, nonce, body))
    if not hmac.compare_digest(supplied, expected):
        raise InternalRequestError("invalid signature", status=401)
    if len(nonce) != 32:
        raise InternalRequestError("invalid nonce", status=401)
    now = time.time()
    with _internal_lock:
        for key, expiry in list(_seen_internal.items()):
            if expiry <= now:
                _seen_internal.pop(key, None)
        if nonce in _seen_internal or len(_seen_internal) >= 8192:
            raise InternalRequestError("replayed or throttled request", status=409)
        _seen_internal[nonce] = now + 120


def _post_internal(url: str, headers: dict[str, str], body: bytes, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_FRAME + 1)
    except urllib.error.HTTPError as exc:
        raise BrowserSkillError("browser owner rejected the request; refresh connection status") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BrowserSkillError("browser owner unavailable; do not replay interrupted operations") from exc
    if len(raw) > MAX_FRAME:
        raise BrowserSkillError("invalid browser owner response")
    try:
        result = json.loads(raw)
    except ValueError as exc:
        raise BrowserSkillError("invalid browser owner response") from exc
    if not isinstance(result, dict):
        raise BrowserSkillError("invalid browser owner response")
    return result


def _route(
    scope: str,
    session: str,
    operation: str,
    method: str = "",
    params: dict[str, Any] | None = None,
) -> tuple[Any, bool]:
    row = _device(scope)
    if not row:
        return None, False
    owner = str(row.get("owner") or "")
    if not owner or owner == NODE_ID:
        return None, False
    now = _now()
    active = bool(
        not row.get("revoked_at")
        and str(row.get("expires_at") or "") > now
        and str(row.get("lease_until") or "") > now
    )
    requires_peer = operation in {"call", "preview", "focus"} or (
        operation == "control" and method in {"start", "resume"}
    )
    if not active:
        if requires_peer:
            raise BrowserSkillError("local browser is offline; keep Chrome and the extension open")
        return None, False
    owner_url = str(row.get("owner_url") or "")
    secret = _internal_secret()
    if len(secret) < 32 or not _valid_internal_url(owner_url):
        raise BrowserSkillError("browser is connected to another node; configure BrowserSkill cluster routing")
    payload: dict[str, Any] = {
        "node": owner,
        "scope": {"tenant": int(row.get("tenant") or 0), "user": str(row.get("user") or "")},
        "session": session,
        "operation": operation,
        "method": method,
        "params": params or {},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    headers = {
        "Content-Type": "application/json",
        "X-Browser-Timestamp": timestamp,
        "X-Browser-Nonce": nonce,
        "X-Browser-Signature": sign_internal(timestamp, nonce, body),
    }
    timeout = (
        HUMAN_STEP_TIMEOUT
        if operation == "call" and method in HUMAN_STEP_METHODS
        else CLUSTER_RPC_TIMEOUT if operation == "call" else LOCAL_RPC_TIMEOUT
    )
    result = _post_internal(owner_url.rstrip("/") + INTERNAL_PATH, headers, body, timeout)
    rpc_error = result.get("rpc_error")
    if isinstance(rpc_error, dict) and rpc_error:
        raise BrowserSkillError(
            str(rpc_error.get("message") or rpc_error.get("code") or "BrowserSkill command failed"),
            code=str(rpc_error.get("code") or ""),
            data=rpc_error.get("data"),
        )
    error = result.get("error")
    if error:
        raise BrowserSkillError(str(error))
    return result.get("data"), True


def authenticate(token: str) -> dict[str, Any] | None:
    import base64
    import binascii

    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, binascii.Error):
        return None
    if len(raw) != 32:
        return None
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != token:
        return None
    digest = token_hash(token)
    row = db.fetch_one(
        "SELECT * FROM browser_devices WHERE revoked_at IS NULL AND expires_at>? "
        "AND (token_hash=? OR (previous_hash=? AND previous_until>?))",
        (_now(), digest, digest, _now()),
    )
    if not row:
        return None
    active = db.fetch_one(
        "SELECT 1 FROM users WHERE id=? AND is_active=1 AND deleted_at IS NULL", (row["user"],)
    )
    if not active:
        return None
    return row


def claim(device_id: str, origin: str) -> str:
    lease_key = _new_lease_key()
    now = _now()
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE browser_devices SET owner=?, owner_url=?, lease_key=?, lease_until=?, last_seen_at=? "
            "WHERE id=? AND revoked_at IS NULL AND expires_at>? AND (owner='' OR lease_until<=?)",
            (NODE_ID, _internal_url(), lease_key, _timestamp(LEASE_SECONDS), now, device_id, now, now),
        )
        if int(cursor.rowcount or 0) != 1:
            raise BrowserSkillError("browser connection is owned by another node; reconnect shortly")
    with _tasks_lock:
        for key in [key for key in _extension_versions if key[0] == device_id and key[1] != lease_key]:
            _extension_versions.pop(key, None)
    return lease_key


def heartbeat(device_id: str, lease_key: str) -> bool:
    now = _now()
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE browser_devices SET lease_until=?, last_seen_at=? "
            "WHERE id=? AND lease_key=? AND revoked_at IS NULL AND expires_at>?",
            (_timestamp(LEASE_SECONDS), now, device_id, lease_key, now),
        )
        return int(cursor.rowcount or 0) == 1


def set_extension_version(device_id: str, lease_key: str, version: str) -> None:
    with _tasks_lock:
        _extension_versions[(device_id, lease_key)] = version[:64]


def release(device_id: str, lease_key: str) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE browser_devices SET owner='', owner_url='', lease_until='' "
            "WHERE id=? AND lease_key=?",
            (device_id, lease_key),
        )
        released = int(cursor.rowcount or 0) == 1
    if released:
        with _tasks_lock:
            _extension_versions.pop((device_id, lease_key), None)
    return released


def clear_tasks(scope: str) -> None:
    with _tasks_lock:
        _tasks.pop(scope, None)
        for key in [key for key in _session_locks if key[0] == scope]:
            _session_locks.pop(key, None)


def _fresh_turn() -> dict[str, Any]:
    return {"used": False, "keep_open": False, "failed": False}


def _paused_error() -> BrowserSkillError:
    return BrowserSkillError(
        "Browser is connected, but this task is paused for user intervention. Ask the user to complete "
        "any manual step, click Continue operation in the conversation browser preview, then continue "
        "in this same conversation. Do not reconnect or pair again. Observe before acting.",
        code="task_paused",
    )


def _human_timeout_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return HUMAN_STEP_WAIT_MS
    if not 1 <= value <= HUMAN_STEP_WAIT_MS:
        return HUMAN_STEP_WAIT_MS
    return int(value)


def begin_turn(scope: str, session: str) -> None:
    """Reset one agent turn's retention state without dropping the task."""
    with _tasks_lock:
        task = _tasks.setdefault(scope, {}).setdefault(session, {})
        task["turn"] = _fresh_turn()


def _turn_state(task: dict[str, Any]) -> dict[str, Any]:
    turn = task.get("turn")
    if not isinstance(turn, dict):
        turn = _fresh_turn()
        task["turn"] = turn
    return turn


def mark_turn_failed(scope: str, session: str) -> None:
    with _tasks_lock:
        task = (_tasks.get(scope) or {}).get(session)
        if task is not None:
            _turn_state(task)["failed"] = True


def _connection_limit() -> int:
    try:
        limit = int(os.getenv("BROWSERSKILL_MAX_CONNECTIONS", "").strip() or 0)
    except ValueError:
        limit = 0
    return limit if limit > 0 else DEFAULT_MAX_CONNECTIONS


def reserve_connection() -> None:
    global _connection_reservations
    with _connection_lock:
        if len(_live) + _connection_reservations >= _connection_limit():
            raise BrowserSkillError("local browser connection capacity reached", code="capacity")
        _connection_reservations += 1


def release_connection_reservation() -> None:
    global _connection_reservations
    with _connection_lock:
        _connection_reservations = max(0, _connection_reservations - 1)


def register_live(connection: LiveConnection, *, reserved: bool = False) -> None:
    global _connection_reservations
    with _connection_lock:
        if reserved:
            _connection_reservations = max(0, _connection_reservations - 1)
        _live[connection.scope] = connection


def unregister_live(connection: LiveConnection) -> None:
    with _connection_lock:
        if _live.get(connection.scope) is connection:
            _live.pop(connection.scope, None)
    for future in connection.ui_replies.values():
        if not future.done():
            future.cancel()
    connection.ui_replies.clear()


def _session_lock(scope: str, session: str) -> threading.Lock:
    with _tasks_lock:
        return _session_locks.setdefault((scope, session), threading.Lock())


def _device(scope: str) -> dict[str, Any] | None:
    return db.fetch_one("SELECT * FROM browser_devices WHERE scope_key=?", (scope,))


def connected(scope: str) -> bool:
    row = _device(scope)
    return bool(
        row
        and not row.get("revoked_at")
        and str(row.get("expires_at") or "") > _now()
        and str(row.get("lease_until") or "") > _now()
        and row.get("lease_key")
    )


def owns(scope: str) -> bool:
    row = _device(scope)
    return bool(row and str(row.get("owner") or "") == NODE_ID)


def extension_version(scope: str) -> str:
    row = _device(scope)
    if not row or not connected(scope):
        return ""
    owner = str(row.get("owner") or "")
    if owner == NODE_ID:
        with _tasks_lock:
            return _extension_versions.get((str(row.get("id") or ""), str(row.get("lease_key") or "")), "")
    data, remote = _route(scope, "", "status")
    if remote and isinstance(data, dict):
        return str(data.get("extension_version") or "")
    return ""


def status(scope: str, session: str = "") -> dict[str, Any]:
    data, remote = _route(scope, session, "status")
    if remote:
        if not isinstance(data, dict):
            raise BrowserSkillError("invalid browser owner status")
        return data
    row = _device(scope)
    connected_now = connected(scope)
    selected = bool(
        session
        and db.fetch_one(
            "SELECT 1 FROM browser_task_interruptions WHERE scope_key=? AND session=?",
            (scope, session),
        )
    )
    with _tasks_lock:
        task = dict((_tasks.get(scope) or {}).get(session) or {})
    task_id = str(task.get("task_id") or "")
    selected = selected or bool(task)
    paused = bool(selected and (not connected_now or task.get("paused")))
    needs_help = selected and int(task.get("help_calls") or 0) > 0 and not bool(task.get("paused"))
    version = ""
    if connected_now and row and str(row.get("owner") or "") == NODE_ID:
        with _tasks_lock:
            version = _extension_versions.get(
                (str(row.get("id") or ""), str(row.get("lease_key") or "")), ""
            )
    return {
        "enabled": enabled(),
        "connected": connected_now,
        "selected": selected,
        "paused": paused,
        "idle": not selected,
        "needs_help": needs_help,
        "stopping": bool(task.get("stopping")),
        "extension_version": version,
        "task_id": task_id or (session if selected else ""),
        "action": str(task.get("action") or ""),
        "action_elapsed_ms": _action_elapsed_ms(task),
        "page_url": str(task.get("page_url") or ""),
        "last_error": str(task.get("last_error") or ""),
        "help_prompt": str(task.get("help_prompt") or "") if needs_help else "",
    }


def task_id(scope: str, session: str) -> str:
    with _tasks_lock:
        task = (_tasks.get(scope) or {}).get(session) or {}
    return str(task.get("task_id") or "")


def ensure_daemon() -> tuple[str, int]:
    global _daemon_process, _daemon_home, _daemon_port
    with _daemon_lock:
        if _daemon_process is not None and _daemon_process.poll() is None and _daemon_port:
            return _daemon_home, _daemon_port
        _stop_daemon_locked()
        binary = str(os.getenv("BROWSERSKILL_BINARY") or "").strip()
        if not binary:
            raise BrowserSkillError("local browser is unavailable")
        home = tempfile.mkdtemp(prefix="wkb-", dir="/tmp")
        command = [
            binary,
            "daemon",
            "start",
            "--foreground",
            "--port",
            "0",
            "--daemon-idle",
            "24h",
            "--session-idle",
            "30m",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ, "BSK_HOME": home},
            )
        except OSError as exc:
            raise BrowserSkillError("could not start BrowserSkill daemon") from exc
        info_path = Path(home) / "daemon.json"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            port = int(info.get("ws_port") or 0)
            if port > 0:
                _daemon_process, _daemon_home, _daemon_port = process, home, port
                return home, port
            time.sleep(0.05)
        process.kill()
        process.wait(timeout=5)
        import shutil

        shutil.rmtree(home, ignore_errors=True)
        raise BrowserSkillError("BrowserSkill daemon startup timed out")


def _stop_daemon_locked() -> None:
    global _daemon_process, _daemon_home, _daemon_port
    if _daemon_process is not None and _daemon_process.poll() is None:
        _daemon_process.kill()
        try:
            _daemon_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if _daemon_home:
        import shutil

        shutil.rmtree(_daemon_home, ignore_errors=True)
    _daemon_process, _daemon_home, _daemon_port = None, "", 0


def shutdown() -> None:
    global _connection_reservations
    try:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE browser_devices SET owner='', owner_url='', lease_until='' WHERE owner=?",
                (NODE_ID,),
            )
    except Exception:  # noqa: BLE001 - shutdown must continue if the store is unavailable
        pass
    with _daemon_lock:
        _stop_daemon_locked()
    with _tasks_lock:
        _tasks.clear()
        _session_locks.clear()
        _extension_versions.clear()
    with _connection_lock:
        _live.clear()
        _connection_reservations = 0


def _track_rpc(task_key: tuple[str, str] | None, path: Path, request_id: str) -> None:
    if task_key is None:
        return
    with _tasks_lock:
        task = (_tasks.get(task_key[0]) or {}).get(task_key[1])
        if task is not None:
            task.setdefault("rpc_calls", set()).add((str(path), request_id))


def _untrack_rpc(task_key: tuple[str, str] | None, path: Path, request_id: str) -> None:
    if task_key is None:
        return
    with _tasks_lock:
        task = (_tasks.get(task_key[0]) or {}).get(task_key[1])
        if task is not None:
            calls = task.get("rpc_calls")
            if isinstance(calls, set):
                calls.discard((str(path), request_id))


def _cancel_rpc_calls(calls: list[tuple[str, str]]) -> None:
    for raw_path, request_id in calls:
        _cancel_rpc(Path(raw_path), request_id)


def _task_rpc_calls(scope: str, session: str) -> list[tuple[str, str]]:
    with _tasks_lock:
        task = (_tasks.get(scope) or {}).get(session) or {}
        return list(task.get("rpc_calls") or ())


def _numeric_tab_id(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _agent_window_tabs(
    scope: str,
    session: str,
    task_id: str,
    *,
    task_key: tuple[str, str] | None,
) -> list[int | float]:
    result = _rpc(
        "tool.tab_list",
        {"session_id": task_id, "scope": "agent"},
        task_key=task_key,
    )
    tabs = result.get("tabs")
    if tabs is None:
        return []
    if not isinstance(tabs, list):
        raise BrowserSkillError("invalid BrowserSkill tab list")
    ids: list[int | float] = []
    for tab in tabs:
        tab_id = _numeric_tab_id(tab.get("tab_id")) if isinstance(tab, dict) else None
        if tab_id is None:
            raise BrowserSkillError("invalid BrowserSkill tab list")
        ids.append(tab_id)
    return ids


def _preserve_agent_window(
    scope: str,
    session: str,
    task_id: str,
    tab_id: Any,
) -> int | float | None:
    target = _numeric_tab_id(tab_id)
    if target is None:
        return None
    tabs = _agent_window_tabs(scope, session, task_id, task_key=(scope, session))
    if len(tabs) != 1 or tabs[0] != target:
        return None
    created = _rpc(
        "tool.tab_create",
        {"session_id": task_id, "url": "about:blank", "active": False},
        task_key=(scope, session),
    )
    placeholder = _numeric_tab_id(created.get("tab_id"))
    if placeholder is None or placeholder == 0:
        raise BrowserSkillError("invalid BrowserSkill tab_create result")
    return placeholder


def _release_placeholder(scope: str, session: str, task_id: str, tab_id: Any, placeholder: Any) -> None:
    target = _numeric_tab_id(tab_id)
    placeholder_id = _numeric_tab_id(placeholder)
    if target is None or placeholder_id is None:
        return
    try:
        tabs = _agent_window_tabs(scope, session, task_id, task_key=None)
        if target not in tabs:
            return
        _rpc(
            "tool.tab_close",
            {"session_id": task_id, "tab_id": placeholder_id},
            timeout=5,
        )
    except BrowserSkillError:
        pass


def _rpc(
    method: str,
    params: dict[str, Any],
    timeout: float = 35,
    *,
    task_key: tuple[str, str] | None = None,
) -> dict[str, Any]:
    home, _ = ensure_daemon()
    path = Path(home) / "run" / "daemon.sock"
    request_id = secrets.token_hex(12)
    _track_rpc(task_key, path, request_id)
    payload = json.dumps(
        {"id": request_id, "method": method, "params": params},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode() + b"\n"
    try:
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(path))
                connection.sendall(payload)
                sent = True
                data = b""
                while b"\n" not in data and len(data) <= MAX_FRAME:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
        except TimeoutError as exc:
            if sent:
                _cancel_rpc(path, request_id)
            raise BrowserSkillError(
                "browser command interrupted or timed out; do not replay actions automatically",
                code="timeout",
            ) from exc
        except OSError as exc:
            if sent:
                _cancel_rpc(path, request_id)
            raise BrowserSkillError("BrowserSkill daemon unavailable") from exc
        line = data.split(b"\n", 1)[0]
        if len(line) > MAX_FRAME:
            raise BrowserSkillError("BrowserSkill response is too large")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise BrowserSkillError("invalid BrowserSkill response") from exc
        if not isinstance(reply, dict):
            raise BrowserSkillError("invalid BrowserSkill response")
        if reply.get("id") != request_id:
            error = reply.get("error")
            if (
                reply.get("id") == "0"
                and isinstance(error, dict)
                and error.get("code") == "protocol_error"
            ):
                raise BrowserSkillError(
                    "BrowserSkill daemon rejected the request protocol; rebuild bsk and the extension "
                    "from the same pinned source baseline",
                    code="daemon_incompatible",
                )
            raise BrowserSkillError("invalid BrowserSkill response ID")
        error = reply.get("error")
        if isinstance(error, dict) and error:
            message = str(error.get("message") or error.get("code") or "BrowserSkill command failed")
            raise BrowserSkillError(
                message,
                code=str(error.get("code") or ""),
                data=error.get("data"),
            )
        result = reply.get("result")
        return result if isinstance(result, dict) else {"result": result}
    finally:
        _untrack_rpc(task_key, path, request_id)


def _cancel_rpc(path: Path, request_id: str) -> None:
    payload = json.dumps(
        {"id": secrets.token_hex(12), "method": "cancel", "params": {"rpc_id": request_id}},
        separators=(",", ":"),
    ).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(payload)
    except OSError:
        pass


def start_task(scope: str, session: str) -> str:
    row = _device(scope)
    if not row or not connected(scope):
        raise BrowserSkillError("local browser is offline; keep Chrome and the extension open")
    browser_id = str(row.get("lease_key") or "")
    with _tasks_lock:
        state = _tasks.setdefault(scope, {}).setdefault(session, {})
        state.setdefault("turn", _fresh_turn())
    result = _rpc(
        "session.start",
        {"focused": False, "browser_instance_id": browser_id},
        timeout=60,
        task_key=(scope, session),
    )
    task = str(result.get("session_id") or "")
    if not task or str(result.get("browser_instance_id") or "") != browser_id:
        raise BrowserSkillError("invalid BrowserSkill task response")
    with _tasks_lock:
        state = _tasks.setdefault(scope, {}).setdefault(session, {})
        state.update({"task_id": task, "paused": False, "stopping": False})
    return task


def ensure_task(scope: str, session: str) -> str:
    with _tasks_lock:
        task = dict((_tasks.get(scope) or {}).get(session) or {})
    task_id = str(task.get("task_id") or "")
    if not task_id:
        return start_task(scope, session)
    if task.get("paused"):
        _rpc(
            "tool.snapshot",
            {"session_id": task_id},
            timeout=35,
            task_key=(scope, session),
        )
        with _tasks_lock:
            current = (_tasks.get(scope) or {}).get(session)
            if current is not None:
                current["paused"] = False
    return task_id


def pause_task(scope: str, session: str) -> None:
    _, remote = _route(scope, session, "control", "pause")
    if remote:
        return
    with _tasks_lock:
        task = (_tasks.get(scope) or {}).get(session)
        if task is not None:
            task["paused"] = True
    _cancel_rpc_calls(_task_rpc_calls(scope, session))


def _stop_task_local(scope: str, session: str) -> None:
    with _tasks_lock:
        task = (_tasks.get(scope) or {}).get(session)
        if task is not None:
            task["paused"] = True
            task["stopping"] = True
    _cancel_rpc_calls(_task_rpc_calls(scope, session))
    lock = _session_lock(scope, session)
    if not lock.acquire(timeout=5):
        with _tasks_lock:
            task = (_tasks.get(scope) or {}).get(session)
            if task is not None:
                task["stopping"] = False
        return
    try:
        with _tasks_lock:
            task = (_tasks.get(scope) or {}).pop(session, None)
        task_id = str((task or {}).get("task_id") or "")
        if not task_id:
            return
        try:
            _rpc("session.stop", {"session_id": task_id, "all": False}, timeout=15)
        except BrowserSkillError:
            pass
    finally:
        lock.release()
        with _tasks_lock:
            if _session_locks.get((scope, session)) is lock:
                _session_locks.pop((scope, session), None)


def stop_task(scope: str, session: str) -> None:
    _, remote = _route(scope, session, "control", "stop")
    if remote:
        return
    _stop_task_local(scope, session)


def control(scope: str, session: str, action: str) -> None:
    if action not in {"start", "resume", "pause", "stop", "select"}:
        raise BrowserSkillError("invalid browser control")
    _, remote = _route(scope, session, "control", action)
    if remote:
        return
    if action == "pause":
        pause_task(scope, session)
    elif action == "stop":
        _stop_task_local(scope, session)
    elif action in {"start", "resume"}:
        ensure_task(scope, session)


def finish_turn(scope: str, session: str, *, keep_open: bool = False) -> None:
    """Apply BrowserSkill's per-turn retention rule after an agent turn.

    Successful, explicit ``keep_open=false`` calls release the task. Failed or
    cancelled turns retain it by default so the next turn can resume.
    """
    _, remote = _route(scope, session, "finish_turn", params={"keep_open": keep_open})
    if remote:
        return
    with _tasks_lock:
        task = (_tasks.get(scope) or {}).get(session)
        if task is None:
            return
        turn = _turn_state(task)
        if not bool(turn.get("used")):
            task["turn"] = _fresh_turn()
            return
        retain = bool(turn.get("keep_open")) or bool(turn.get("failed")) or keep_open
        task["turn"] = _fresh_turn()
    if not retain:
        stop_task(scope, session)


def forget(scope: str, sessions: list[str]) -> None:
    """Stop and discard BrowserSkill tasks for deleted conversations."""
    for session in sessions:
        if not session:
            continue
        _, remote = _route(scope, session, "forget")
        if remote:
            with db.transaction() as conn:
                conn.execute(
                    "DELETE FROM browser_task_interruptions WHERE scope_key=? AND session=?",
                    (scope, session),
                )
            continue
        stop_task(scope, session)
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM browser_task_interruptions WHERE scope_key=? AND session=?",
                (scope, session),
            )


def forget_all(scope: str) -> None:
    with _tasks_lock:
        sessions = set((_tasks.get(scope) or {}).keys())
    sessions.update(
        str(row["session"])
        for row in db.fetch_all(
            "SELECT session FROM browser_task_interruptions WHERE scope_key=?", (scope,)
        )
    )
    forget(scope, sorted(sessions))
    with db.transaction() as conn:
        conn.execute("DELETE FROM browser_task_interruptions WHERE scope_key=?", (scope,))


def call_tool(
    scope: str,
    session: str,
    method: str,
    params: dict[str, Any],
    *,
    keep_open: bool | None = None,
) -> dict[str, Any]:
    if method not in BROWSER_METHODS:
        raise BrowserSkillError("unsupported BrowserSkill tool")
    remote_result, remote = _route(
        scope,
        session,
        "call",
        method,
        {"tool_params": params, "keep_open": keep_open},
    )
    if remote:
        return remote_result if isinstance(remote_result, dict) else {"result": remote_result}
    if not connected(scope):
        raise BrowserSkillError("local browser is offline; keep Chrome and the extension open")
    lock = _session_lock(scope, session)
    with lock:
        with _tasks_lock:
            task = dict((_tasks.get(scope) or {}).get(session) or {})
        task_id = str(task.get("task_id") or "")
        if task.get("stopping"):
            raise BrowserSkillError("browser task is ending")
        if task.get("paused"):
            raise _paused_error()
        if not task_id:
            task_id = ensure_task(scope, session)
        with _tasks_lock:
            current = _tasks.setdefault(scope, {}).setdefault(session, {})
            current["action"] = method
            current["action_started"] = time.monotonic()
            current["action_finished"] = None
            current["last_error"] = ""
            if method == "navigate":
                current["page_url"] = _status_page_url(params.get("url"))
            if method in {"tab_select", "tab_close", "tab_return"}:
                current["page_url"] = ""
            if method in HUMAN_STEP_METHODS:
                current["help_calls"] = int(current.get("help_calls") or 0) + 1
                current["help_prompt"] = str(params.get("prompt") or "")
        clean = {
            key: value
            for key, value in params.items()
            if key not in {"session_id", "browser_instance_id"} and value is not None
        }
        if method in {"navigate", "navigate_back", "navigate_forward", "reload"}:
            clean.setdefault("wait_until", "domcontentloaded")
        if method == "request_help":
            clean.pop("completion_criteria", None)
            clean["timeout_ms"] = _human_timeout_ms(clean.get("timeout_ms"))
        elif method == "tab_borrow":
            clean["confirmation_timeout_ms"] = _human_timeout_ms(clean.get("confirmation_timeout_ms"))
            clean.pop("timeout_ms", None)
        clean["session_id"] = task_id
        placeholder = None
        try:
            if method == "tab_close":
                placeholder = _preserve_agent_window(scope, session, task_id, clean.get("tab_id"))
            result = _rpc(
                "tool." + method,
                clean,
                timeout=HUMAN_STEP_TIMEOUT if method in HUMAN_STEP_METHODS else LOCAL_RPC_TIMEOUT,
                task_key=(scope, session),
            )
        except BrowserSkillError as exc:
            if method == "tab_close" and placeholder is not None:
                _release_placeholder(scope, session, task_id, clean.get("tab_id"), placeholder)
            with _tasks_lock:
                current = (_tasks.get(scope) or {}).get(session)
                if current is not None:
                    _finish_action_progress(current, method, None, str(exc))
                    if "session not registered" in str(exc):
                        current["task_id"] = ""
                        current["paused"] = True
                    elif _interrupted_command(exc):
                        current["paused"] = True
                    _turn_state(current)["failed"] = True
            raise
        with _tasks_lock:
            current = (_tasks.get(scope) or {}).get(session)
            if current is not None:
                _finish_action_progress(current, method, result, "")
                turn = _turn_state(current)
                turn["used"] = True
                if keep_open is not None:
                    turn["keep_open"] = bool(keep_open)
                if method == "request_help":
                    turn["keep_open"] = True
                    if result.get("outcome") not in {"continued", "completed"}:
                        current["paused"] = True
        return result


def prepare_handshake(raw: str, lease_key: str) -> tuple[dict[str, Any], str]:
    try:
        frame = json.loads(raw)
    except ValueError as exc:
        raise BrowserSkillError("invalid browser handshake") from exc
    if not isinstance(frame, dict):
        raise BrowserSkillError("invalid browser handshake")
    params = frame.get("params")
    if (
        frame.get("method") != "system.handshake"
        or not isinstance(frame.get("id"), str)
        or not frame["id"]
        or not isinstance(params, dict)
    ):
        raise BrowserSkillError("expected browser handshake")
    params["instance_id"] = lease_key
    version = str(params.get("version") or "").strip()[:64]
    return frame, version


def validate_handshake_reply(raw: str, request_id: str) -> None:
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        raise BrowserSkillError("invalid BrowserSkill handshake reply") from exc
    if not isinstance(reply, dict) or reply.get("id") != request_id:
        raise BrowserSkillError("BrowserSkill handshake failed")
    error = reply.get("error")
    if isinstance(error, dict) and error:
        raise BrowserSkillError("BrowserSkill handshake failed")
    result = reply.get("result")
    if not isinstance(result, dict) or not str(result.get("protocol_version") or ""):
        raise BrowserSkillError("BrowserSkill handshake failed")


async def connect_upstream(port: int, origin: str):
    import websockets

    return await websockets.connect(
        f"ws://127.0.0.1:{port}",
        origin=origin,
        max_size=MAX_FRAME,
        open_timeout=5,
    )


async def heartbeat_until_lost(device_id: str, lease_key: str, interval: float = 10) -> None:
    import asyncio
    import anyio.to_thread

    while True:
        await asyncio.sleep(interval)
        if not await anyio.to_thread.run_sync(heartbeat, device_id, lease_key):
            return


def observe_event(scope: str, raw: str) -> bool:
    try:
        event = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(event, dict):
        return False
    name = str(event.get("event") or "")
    if name not in {"session.user_interrupt", "session.window_closed"}:
        return False
    payload = event.get("payload")
    session = str((payload if isinstance(payload, dict) else {}).get("session_id") or "")
    if not session:
        return False
    sessions: list[str] = []
    with _tasks_lock:
        for key, task in (_tasks.get(scope) or {}).items():
            if str(task.get("task_id") or "") == session:
                if name == "session.window_closed" and task.get("stopping"):
                    continue
                task["paused"] = True
                sessions.append(key)
                if name == "session.window_closed":
                    task["task_id"] = ""
                    task["turn"] = _fresh_turn()
    for key in sessions:
        _cancel_rpc_calls(_task_rpc_calls(scope, key))
    return name == "session.user_interrupt"


async def ui_call(scope: str, session: str, method: str, timeout: float = 5) -> dict[str, Any]:
    import anyio.to_thread

    operation = "preview" if method == "ui.task_preview" else "focus" if method == "ui.task_focus" else ""
    if operation:
        data, remote = await anyio.to_thread.run_sync(
            lambda: _route(scope, session, operation)
        )
        if remote:
            return data if isinstance(data, dict) else {"result": data}
    connection = _live.get(scope)
    task = task_id(scope, session)
    if connection is None:
        raise BrowserSkillError("browser disconnected")
    if not task:
        raise BrowserSkillError("browser task unavailable")
    request_id = "wk-ui-" + secrets.token_hex(8)
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    connection.ui_replies[request_id] = future
    try:
        await connection.send(
            json.dumps(
                {"id": request_id, "method": method, "params": {"session_id": task}},
                separators=(",", ":"),
            )
        )
        raw = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise BrowserSkillError("browser preview timed out; check the BrowserSkill extension") from exc
    finally:
        connection.ui_replies.pop(request_id, None)
    try:
        reply = json.loads(raw)
    except ValueError as exc:
        raise BrowserSkillError("invalid browser UI response") from exc
    error = reply.get("error") if isinstance(reply, dict) else None
    if isinstance(error, dict) and error:
        if str(error.get("code") or "") == "unknown_method":
            raise BrowserSkillError(
                "BrowserSkill extension lacks the ui.task_preview/ui.task_focus channel"
            )
        raise BrowserSkillError(str(error.get("message") or "browser task tab unavailable"))
    result = reply.get("result") if isinstance(reply, dict) else None
    return result if isinstance(result, dict) else {"result": result}


def receive_ui(scope: str, raw: str) -> bool:
    connection = _live.get(scope)
    if connection is None:
        return False
    try:
        frame = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(frame, dict):
        return False
    request_id = str(frame.get("id") or "")
    future = connection.ui_replies.get(request_id)
    if future is None:
        return request_id.startswith("wk-ui-")
    if not future.done():
        future.set_result(raw)
    return True


def local_browser_tool() -> dict[str, Any]:
    method_names = sorted(BROWSER_METHODS)
    return {
        "type": "function",
        "function": {
            "name": "local_browser",
            "description": (
                "Control the user's connected Chrome through BrowserSkill. Pass exactly one "
                "method plus only that method's fields. Use observe before ref interactions, "
                "navigate with a URL-encoded search when no entry URL is known, and request_help "
                "for logins or CAPTCHAs."
            ),
            "parameters": {
                "type": "object",
                "properties": {"method": {"type": "string", "enum": method_names}, **BROWSER_TOOL_PROPERTIES},
                "required": ["method"],
                "additionalProperties": False,
            },
        },
        "service_id": "",
        "tool_name": "local_browser",
        "require_approval": False,
    }

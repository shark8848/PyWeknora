"""Minimal Cube/E2B runtime client used by the sandbox connectivity check.

The two providers share the envd data-plane protocol shape but use different
control-plane credentials and Connect payload encodings: Cube speaks JSON and
E2B speaks protobuf.  This module keeps that difference behind one small
interface so the settings check can create a throwaway sandbox, run commands
inside it, and destroy it without depending on either Go SDK.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import socket
import ssl
import struct
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .sandbox_terminal import SandboxTerminal, SandboxTerminalClosed


DEFAULT_TIMEOUT = 30
ENVD_PORT = 49983
DESKTOP_PORT = 6080
TRAFFIC_TOKEN_HEADER = "e2b-traffic-access-token"
STANDARD_TEMPLATE_NAME = "weknora"
DESKTOP_TEMPLATE_NAME = "weknora-desktop"
STANDARD_IMAGE_REPOSITORY = "wechatopenai/weknora-sandbox"
DEFAULT_E2B_IMAGE = "wechatopenai/weknora-sandbox:main"
DEFAULT_E2B_DESKTOP_IMAGE = "wechatopenai/weknora-sandbox:main-desktop"
DEFAULT_CUBE_IMAGE = "wechatopenai/weknora-sandbox:main-cube"
DEFAULT_CUBE_DESKTOP_IMAGE = "wechatopenai/weknora-sandbox:main-desktop-cube"
DEFAULT_E2B_TEMPLATE_TAG = "default"
CUBE_ENVD_HEALTH_PATH = "/health"
FAILED_TEMPLATE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "untagged"}
E2B_PTY_PROMPT_OVERRIDE = (
    ". /etc/weknora/pty-prompt.sh 2>/dev/null; "
    "for f in /root/.bashrc /home/user/.bashrc /etc/profile.d/zz-weknora-prompt.sh; do "
    "grep -q /etc/weknora/pty-prompt.sh \"$f\" 2>/dev/null || "
    "echo '. /etc/weknora/pty-prompt.sh' >> \"$f\"; "
    "done"
)


def _template_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("templates", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _normalize_image_repository(image: str) -> str:
    reference = image.strip()
    if not reference:
        return ""
    reference = reference.split("@", 1)[0]
    colon = reference.rfind(":")
    if colon > reference.rfind("/"):
        reference = reference[:colon]
    parts = [part for part in reference.strip("/").split("/") if part]
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        parts = parts[1:]
    if len(parts) > 1 and parts[0].lower() == "library":
        parts = parts[1:]
    return "/".join(parts).lower()


def _image_tag(image: str) -> str:
    reference = image.split("@", 1)[0]
    colon = reference.rfind(":")
    slash = reference.rfind("/")
    return reference[colon + 1 :].lower() if colon > slash else ""


def _classify_template(name: str, image: str = "") -> tuple[bool, bool]:
    normalized = name.strip().strip("/")
    leaf = normalized.rsplit("/", 1)[-1].lower()
    if leaf == DESKTOP_TEMPLATE_NAME:
        return False, True
    if leaf == STANDARD_TEMPLATE_NAME:
        return True, False
    repository = _normalize_image_repository(image)
    if repository and repository == _normalize_image_repository(STANDARD_IMAGE_REPOSITORY):
        tag = _image_tag(image)
        desktop = tag == "main-desktop" or tag.endswith("-desktop") or tag.endswith("-desktop-cube")
        return not desktop, desktop
    return False, False


def _normalize_template_status(provider: str, status: str) -> str:
    value = status.strip().lower()
    if value in {"ready", "available", "complete", "completed", "success", "succeeded"}:
        return "ready"
    if value in {"building", "processing", "in_progress", "running"}:
        return "building"
    if provider == "e2b" and value in {"waiting", "queued", "pending", "uploaded"}:
        return "waiting"
    if provider == "cube" and value in {"waiting", "queued", "pending"}:
        return "building"
    if value in {"failed", "failure", "error", "cancelled", "canceled"}:
        return "error" if provider == "e2b" else "failed"
    return value


def _blank_e2b_uuid(value: str) -> bool:
    trimmed = value.strip()
    return not trimmed or all(char in {"0", "-"} for char in trimmed)


def pick_template(templates: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    def rank(item: dict[str, Any]) -> int:
        status = str(item.get("status") or "").lower()
        if status in {"ready", "available", "complete", "completed", "success", "succeeded"}:
            return 3
        if status in {"building", "waiting", "pending", "queued", "processing", "running"}:
            return 2
        if status in FAILED_TEMPLATE_STATUSES:
            return 1
        return 0

    candidates = [
        item for item in templates
        if item.get(kind) and str(item.get("status") or "").lower() not in FAILED_TEMPLATE_STATUSES
    ]
    return max(candidates, key=rank) if candidates else None


class RemoteSandboxError(RuntimeError):
    """A provider-neutral sandbox failure with a UI-safe reason."""

    def __init__(
        self,
        provider: str,
        operation: str,
        message: str,
        *,
        kind: str = "internal",
        status: int = 0,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.operation = operation
        self.message = message
        self.kind = kind
        self.status = status

    def reason(self) -> str:
        if self.kind == "authentication":
            return "认证失败：API Key 无效或无权限"
        if self.kind == "not_found":
            return "资源不存在：请检查模板 ID"
        if self.kind == "timeout":
            return "请求超时：端点不可达或响应过慢"
        if self.kind == "unavailable":
            return "服务不可用：端点拒绝连接"
        if self.kind == "capacity":
            return "配额不足或触发限流"
        if self.kind == "invalid_request":
            return "参数无效：" + self.message
        return self.message


@dataclass(frozen=True)
class RemoteSandbox:
    provider: str
    sandbox_id: str
    traffic_token: str = ""
    envd_token: str = ""
    domain: str = ""


@dataclass(frozen=True)
class RemoteExecResult:
    stdout: str
    stderr: str
    exit_code: int
    latency_ms: int


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: bytes


HTTPTransport = Callable[[str, str, Mapping[str, str], bytes, float], HTTPResponse]


def _default_transport(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
) -> HTTPResponse:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RemoteSandboxError("", "request", "sandbox endpoint must be an absolute http(s) URL")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RemoteSandboxError("", "request", "sandbox endpoint has an invalid port") from exc

    if parsed.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    host_header = headers.get("Host") or parsed.netloc
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", host_header)
        for name, value in headers.items():
            if name.lower() != "host":
                connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body or None)
        response = connection.getresponse()
        payload = response.read()
        return HTTPResponse(response.status, payload)
    except socket.timeout as exc:
        raise RemoteSandboxError("", "request", "sandbox request timed out", kind="timeout") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise RemoteSandboxError("", "request", str(exc), kind="unavailable") from exc
    finally:
        connection.close()


def _error_message(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text[:500]
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return text[:500]


def _classify_status(provider: str, operation: str, status: int, raw: bytes) -> RemoteSandboxError:
    message = _error_message(raw) or f"HTTP {status}"
    if status in {401, 403}:
        kind = "authentication"
    elif status == 404:
        kind = "not_found"
    elif status in {408, 504}:
        kind = "timeout"
    elif status == 429:
        kind = "capacity"
    elif status == 409:
        kind = "invalid_request"
    elif 400 <= status < 500:
        kind = "invalid_request"
    else:
        kind = "unavailable" if status >= 500 else "internal"
    return RemoteSandboxError(provider, operation, message, kind=kind, status=status)


def _varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_varint(number: int, value: int) -> bytes:
    return _varint((number << 3) | 0) + _varint(value)


def _field_bytes(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _field_string(number: int, value: str) -> bytes:
    return _field_bytes(number, value.encode("utf-8"))


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 63)


def _unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift >= 70:
            break
    raise ValueError("malformed protobuf varint")


def _fields(data: bytes):
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, offset = _read_varint(data, offset)
            yield number, wire, value
        elif wire == 2:
            size, offset = _read_varint(data, offset)
            end = offset + size
            if end > len(data):
                raise ValueError("truncated protobuf field")
            yield number, wire, data[offset:end]
            offset = end
        elif wire == 1:
            offset += 8
        elif wire == 5:
            offset += 4
        else:
            raise ValueError("unsupported protobuf wire type")
    if offset != len(data):
        raise ValueError("trailing protobuf bytes")


def _connect_envelope(payload: bytes) -> bytes:
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def _connect_frames(body: bytes):
    offset = 0
    while offset < len(body):
        if offset + 5 > len(body):
            raise ValueError("truncated Connect frame")
        flags = body[offset]
        size = struct.unpack(">I", body[offset + 1 : offset + 5])[0]
        offset += 5
        end = offset + size
        if end > len(body):
            raise ValueError("truncated Connect payload")
        yield flags, body[offset:end]
        offset = end


def _connect_end(payload: bytes, provider: str, operation: str) -> None:
    if not payload:
        return
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return
    error = decoded.get("error") if isinstance(decoded, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or "Connect stream error")
        raise RemoteSandboxError(provider, operation, message, kind="internal")


def _encode_process_config(command: str, env: Mapping[str, str]) -> bytes:
    payload = bytearray()
    payload += _field_string(1, "/bin/bash")
    for arg in ("-l", "-c", command):
        payload += _field_string(2, arg)
    for name, value in env.items():
        entry = _field_string(1, str(name)) + _field_string(2, str(value))
        payload += _field_bytes(3, entry)
    return bytes(payload)


def _e2b_start_request(command: str, env: Mapping[str, str]) -> bytes:
    process = _encode_process_config(command, env)
    # StartRequest.process=1, stdin=4 (explicit false, as the Go SDK sends).
    return _field_bytes(1, process) + _field_varint(4, 0)


def _encode_terminal_process_config(cwd: str, env: Mapping[str, str]) -> bytes:
    payload = bytearray(_field_string(1, "/bin/bash"))
    payload += _field_string(2, "-l")
    for name, value in env.items():
        entry = _field_string(1, str(name)) + _field_string(2, str(value))
        payload += _field_bytes(3, entry)
    if cwd:
        payload += _field_string(4, cwd)
    return bytes(payload)


def _encode_pty(cols: int, rows: int) -> bytes:
    size = _field_varint(1, max(1, int(cols))) + _field_varint(2, max(1, int(rows)))
    return _field_bytes(1, size)


def _encode_process_selector(pid: int) -> bytes:
    return _field_varint(1, max(1, int(pid)))


def _e2b_terminal_start_request(cols: int, rows: int, cwd: str, env: Mapping[str, str]) -> bytes:
    return (
        _field_bytes(1, _encode_terminal_process_config(cwd, env))
        + _field_bytes(2, _encode_pty(cols, rows))
        + _field_varint(4, 1)
    )


def _e2b_terminal_connect_request(pid: int) -> bytes:
    return _field_bytes(1, _encode_process_selector(pid))


def _e2b_terminal_update_request(pid: int, cols: int, rows: int) -> bytes:
    return _field_bytes(1, _encode_process_selector(pid)) + _field_bytes(2, _encode_pty(cols, rows))


def _e2b_terminal_input_request(pid: int, data: bytes) -> bytes:
    process_input = _field_bytes(2, data)
    return _field_bytes(1, _encode_process_selector(pid)) + _field_bytes(2, process_input)


def _decode_e2b_process_event(payload: bytes) -> tuple[bytes, bytes, int | None, str]:
    stdout = bytearray()
    stderr = bytearray()
    exit_code: int | None = None
    status = ""
    for number, wire, value in _fields(payload):
        if number != 1 or wire != 2:
            continue
        for event_number, event_wire, event_value in _fields(value):
            if event_wire != 2:
                continue
            if event_number == 2:
                for out_number, out_wire, out_value in _fields(event_value):
                    if out_wire != 2:
                        continue
                    if out_number == 1:
                        stdout += out_value
                    elif out_number == 2:
                        stderr += out_value
            elif event_number == 3:
                for end_number, end_wire, end_value in _fields(event_value):
                    if end_number == 1 and end_wire == 0:
                        exit_code = _unzigzag(int(end_value))
                    elif end_number == 3 and end_wire == 2:
                        status = end_value.decode("utf-8", errors="replace")
    return bytes(stdout), bytes(stderr), exit_code, status


def _decode_e2b_terminal_event(payload: bytes) -> tuple[str, bytes, int, int]:
    """Decode one ProcessEvent into (kind, data, pid, exit_code)."""
    kind = "unknown"
    data = b""
    pid = 0
    exit_code = -1
    for number, wire, value in _fields(payload):
        if number != 1 or wire != 2:
            continue
        for event_number, event_wire, event_value in _fields(value):
            if event_number == 1 and event_wire == 2:
                kind = "start"
                for number2, wire2, value2 in _fields(event_value):
                    if number2 == 1 and wire2 == 0:
                        pid = int(value2)
            elif event_number == 2 and event_wire == 2:
                kind = "data"
                for number2, wire2, value2 in _fields(event_value):
                    if number2 == 3 and wire2 == 2:
                        data = value2
                    elif number2 in {1, 2} and wire2 == 2 and not data:
                        data = value2
            elif event_number == 3 and event_wire == 2:
                kind = "end"
                for number2, wire2, value2 in _fields(event_value):
                    if number2 == 1 and wire2 == 0:
                        exit_code = _unzigzag(int(value2))
                    elif number2 == 2 and wire2 == 0:
                        exit_code = exit_code if exit_code != -1 else 0
            elif event_number == 4:
                kind = "keepalive"
    return kind, data, pid, exit_code


def _deny_all_ipv4(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for item in values:
        target = str(item).strip()
        if target == "0/0":
            target = "0.0.0.0/0"
        try:
            network = ipaddress.ip_network(target, strict=False)
        except ValueError:
            continue
        if network.version == 4 and network.prefixlen == 0:
            return True
    return False


def denies_egress_by_default(config_data: Mapping[str, Any]) -> bool:
    network = config_data.get("network")
    if not isinstance(network, dict):
        return False
    return bool(network.get("deny_egress_by_default")) or _deny_all_ipv4(network.get("deny_out"))


def _network_payload(provider: str, config_data: Mapping[str, Any]) -> dict[str, Any]:
    network = config_data.get("network")
    policy = network if isinstance(network, dict) else {}
    allow_internet = not bool(policy.get("deny_egress_by_default"))
    allow_out = [str(item) for item in policy.get("allow_out") or [] if str(item).strip()]
    deny_out = [str(item) for item in policy.get("deny_out") or [] if str(item).strip()]
    if allow_internet is False and allow_out and not deny_out:
        deny_out = ["0.0.0.0/0"]

    payload: dict[str, Any] = {"allowPublicTraffic": False}
    if allow_out:
        payload["allowOut"] = allow_out
    if deny_out:
        payload["denyOut"] = deny_out

    if provider == "cube":
        rules = policy.get("cube_rules")
        if isinstance(rules, list) and rules:
            converted = []
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                match = {
                    key: rule.get(key)
                    for key in ("host", "sni", "path", "methods", "scheme")
                    if rule.get(key)
                }
                if match.get("methods"):
                    match["method"] = match.pop("methods")
                action: dict[str, Any] = {"allow": not bool(rule.get("deny"))}
                if rule.get("audit"):
                    action["audit"] = str(rule["audit"])
                inject = rule.get("inject")
                if isinstance(inject, list) and inject:
                    action["inject"] = inject
                converted.append({
                    "name": str(rule.get("name") or ""),
                    "match": match,
                    "action": action,
                })
            if converted:
                payload["rules"] = converted
    else:
        host_rules = policy.get("e2b_host_rules")
        if isinstance(host_rules, list):
            rules: dict[str, Any] = {}
            for rule in host_rules:
                if not isinstance(rule, dict):
                    continue
                host = str(rule.get("host") or "").strip()
                headers = rule.get("headers")
                if host and isinstance(headers, dict) and headers:
                    rules[host] = [{"transform": {"headers": headers}}]
            if rules:
                payload["rules"] = rules
    return payload


class EnvdTerminalSession(SandboxTerminal):
    """Streaming Process.Start/Connect handle for Cube and E2B."""

    def __init__(
        self,
        client: "RemoteSandboxClient",
        sandbox: RemoteSandbox,
        *,
        scheme: str,
        dial_host: str,
        authority: str,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        self.client = client
        self.sandbox = sandbox
        self.backend = client.provider
        self.scheme = scheme
        self.dial_host = dial_host
        self.authority = authority
        self.connection = connection
        self.response = response
        self.pending: list[bytes] = []
        self._closed = False
        self._ended = False
        self._connection_closed = False

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.response.read(size - len(chunks))
            if not chunk:
                raise EOFError("terminal stream ended")
            chunks.extend(chunk)
        return bytes(chunks)

    def _raw_event(self) -> tuple[str, bytes, int]:
        header = self.response.read(5)
        if not header:
            raise EOFError("terminal stream ended")
        if len(header) < 5:
            header += self._read_exact(5 - len(header))
        flags = header[0]
        size = struct.unpack(">I", header[1:])[0]
        if size > 64 * 1024 * 1024:
            raise RemoteSandboxError(self.backend, "Terminal", "terminal event is too large", kind="internal")
        payload = self._read_exact(size) if size else b""
        if flags & 0x01:
            raise RemoteSandboxError(self.backend, "Terminal", "compressed terminal event", kind="internal")
        if flags & 0x02:
            _connect_end(payload, self.backend, "Terminal")
            return "end", b"", -1
        if self.backend == "cube":
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise RemoteSandboxError(
                    self.backend, "Terminal", "terminal returned malformed JSON", kind="internal"
                ) from exc
            event = decoded.get("event") if isinstance(decoded, dict) else None
            if not isinstance(event, dict):
                return "unknown", b"", -1
            start = event.get("start")
            if isinstance(start, dict):
                try:
                    return "start", b"", int(start.get("pid") or 0)
                except (TypeError, ValueError):
                    return "start", b"", 0
            data = event.get("data")
            if isinstance(data, dict):
                value = data.get("pty")
                if isinstance(value, str) and value:
                    try:
                        return "data", base64.b64decode(value), 0
                    except ValueError as exc:
                        raise RemoteSandboxError(
                            self.backend, "Terminal", "terminal returned invalid base64", kind="internal"
                        ) from exc
                return "unknown", b"", -1
            end = event.get("end")
            if isinstance(end, dict):
                code = end.get("exitCode", end.get("exit_code"))
                if isinstance(code, int):
                    return "end", b"", code
                status = str(end.get("status") or "")
                if status.startswith("exit status "):
                    try:
                        return "end", b"", int(status.removeprefix("exit status ").strip())
                    except ValueError:
                        return "end", b"", 0
                return "end", b"", 0 if bool(end.get("exited")) else -1
            return "keepalive", b"", -1
        kind, data, pid, exit_code = _decode_e2b_terminal_event(payload)
        return kind, data, pid if pid else exit_code

    def begin(self) -> None:
        while not self.pid:
            kind, data, value = self._raw_event()
            if kind == "start":
                self.pid = value
            elif kind == "data":
                self.pending.append(data)
            elif kind == "end":
                self.close()
                raise RemoteSandboxError(
                    self.backend, "Terminal", "terminal exited before it started", kind="internal"
                )

    def read(self, size: int = 32768) -> bytes:
        if self.pending:
            return self.pending.pop(0)[:size]
        while True:
            if self._closed:
                raise SandboxTerminalClosed(-1)
            try:
                kind, data, value = self._raw_event()
            except (OSError, EOFError) as exc:
                self._closed = True
                raise SandboxTerminalClosed(-1) from exc
            if kind == "data" and data:
                return data[:size]
            if kind == "end":
                self._ended = True
                self._closed = True
                raise SandboxTerminalClosed(value)

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self.pid <= 0:
            raise RemoteSandboxError(self.backend, "terminal input", "terminal has no PID", kind="internal")
        if self.backend == "cube":
            payload = json.dumps({
                "process": {"pid": self.pid},
                "input": {"pty": base64.b64encode(data).decode("ascii")},
            }).encode("utf-8")
        else:
            payload = _e2b_terminal_input_request(self.pid, data)
        self.client._terminal_rpc(self.sandbox, "SendInput", payload)

    def resize(self, cols: int, rows: int) -> None:
        if cols <= 0 or rows <= 0 or self.pid <= 0:
            return
        if self.backend == "cube":
            payload = json.dumps({
                "process": {"pid": self.pid},
                "pty": {"size": {"cols": int(cols), "rows": int(rows)}},
            }).encode("utf-8")
        else:
            payload = _e2b_terminal_update_request(self.pid, cols, rows)
        self.client._terminal_rpc(self.sandbox, "Update", payload)

    def close(self) -> None:
        self._closed = True
        if self._connection_closed:
            return
        self._connection_closed = True
        try:
            self.response.close()
        finally:
            self.connection.close()


class RemoteSandboxClient:
    def __init__(
        self,
        provider: str,
        backend: Mapping[str, Any],
        config_data: Mapping[str, Any] | None = None,
        *,
        transport: HTTPTransport | None = None,
    ) -> None:
        provider = provider.strip().lower()
        if provider not in {"cube", "e2b"}:
            raise ValueError(f"unsupported remote sandbox provider: {provider}")
        api_url = str(backend.get("api_url") or "").strip()
        if not api_url:
            raise ValueError(f"{provider}.api_url is required")
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{provider}.api_url must be an absolute http(s) URL")
        self.provider = provider
        self.backend = dict(backend)
        self.config_data = dict(config_data or {})
        self.api_url = api_url.rstrip("/")
        self.api_key = str(backend.get("api_key") or "")
        self.proxy_url = str(backend.get("proxy_url") or "").strip()
        self.template_id = str(backend.get("template_id") or "").strip()
        self.sandbox_domain = str(backend.get("sandbox_domain") or "").strip()
        timeout = int(backend.get("http_timeout_sec") or 0)
        self.timeout = timeout if timeout > 0 else DEFAULT_TIMEOUT
        self.transport = transport or _default_transport

    def _request(
        self,
        operation: str,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes = b"",
        *,
        timeout: float | None = None,
    ) -> HTTPResponse:
        try:
            return self.transport(method, url, headers, body, float(timeout or self.timeout))
        except RemoteSandboxError as exc:
            if not exc.provider:
                exc.provider = self.provider
            if exc.operation == "request":
                exc.operation = operation
            raise
        except (OSError, ValueError) as exc:
            raise RemoteSandboxError(self.provider, operation, str(exc), kind="unavailable") from exc

    def _control_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            if self.provider == "cube":
                headers["Authorization"] = "Bearer " + self.api_key
            else:
                headers["X-API-Key"] = self.api_key
        return headers

    def _json_request(
        self,
        operation: str,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        response = self._request(
            operation,
            method,
            self.api_url + path,
            self._control_headers(),
            body,
        )
        if not 200 <= response.status < 300:
            raise _classify_status(self.provider, operation, response.status, response.body)
        if not response.body:
            return None
        try:
            return json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteSandboxError(
                self.provider, operation, "sandbox control plane returned invalid JSON", kind="internal"
            ) from exc

    def health(self) -> None:
        if self.provider == "cube":
            self._json_request("Health", "GET", "/health")
            return
        try:
            self._json_request("Health", "GET", "/v2/sandboxes?limit=1")
        except RemoteSandboxError as exc:
            if exc.status != 404:
                raise
            self._json_request("Health", "GET", "/sandboxes?limit=1")

    def list_templates(self) -> list[dict[str, Any]]:
        payload = self._json_request("ListTemplates", "GET", "/templates")
        templates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in _template_items(payload):
            if not isinstance(item, dict):
                continue
            template_id = str(item.get("templateID") or item.get("template_id") or item.get("id") or "").strip()
            if not template_id or template_id in seen:
                continue
            if self.provider == "cube" and template_id.lower().startswith("snap-"):
                continue
            seen.add(template_id)
            names = [str(value).strip() for value in item.get("names") or [] if str(value).strip()]
            aliases = [str(value).strip() for value in item.get("aliases") or [] if str(value).strip()]
            name = str(item.get("name") or "").strip()
            if not name:
                name = next(iter(names + aliases), template_id)
            image = str(item.get("imageInfo") or item.get("image") or "").strip()
            standard, desktop = _classify_template(name, image)
            for candidate in aliases + names:
                candidate_standard, candidate_desktop = _classify_template(candidate, image)
                if candidate_desktop:
                    standard, desktop = False, True
                    break
                if candidate_standard:
                    standard = True
            if standard:
                name = STANDARD_TEMPLATE_NAME if name == template_id else name
            elif desktop:
                name = DESKTOP_TEMPLATE_NAME if name == template_id else name
            status = _normalize_template_status(
                self.provider,
                str(item.get("buildStatus") or item.get("status") or ""),
            )
            if (
                self.provider == "e2b"
                and (standard or desktop)
                and status in {"building", "waiting"}
            ):
                reconciled = self._reconcile_e2b_template_status(
                    template_id,
                    str(item.get("buildID") or item.get("build_id") or ""),
                    int(item.get("buildCount") or item.get("build_count") or 0),
                )
                if reconciled:
                    status = reconciled
            template: dict[str, Any] = {
                "id": template_id,
                "name": name,
                "standard": standard,
            }
            if desktop:
                template["desktop"] = True
            if status:
                template["status"] = status
            for source, target in (
                ("buildID", "version"),
                ("createdAt", "created_at"),
                ("updatedAt", "updated_at"),
                ("instanceType", "instance_type"),
                ("networkType", "network_type"),
                ("lastError", "error"),
            ):
                value = str(item.get(source) or "").strip()
                if value:
                    template[target] = value
            if image:
                template["image"] = image
            if isinstance(item.get("allowInternetAccess"), bool):
                template["allow_internet_access"] = item["allowInternetAccess"]
            templates.append(template)
        return templates

    def _reconcile_e2b_template_status(
        self, template_id: str, build_id: str, build_count: int
    ) -> str:
        """Resolve a stale pending list status from E2B's current build."""
        if _blank_e2b_uuid(build_id):
            if build_count > 0 and self._e2b_has_successful_build(template_id):
                return "untagged"
            return ""
        path = (
            "/templates/"
            + urllib.parse.quote(template_id, safe="")
            + "/builds/"
            + urllib.parse.quote(build_id.strip(), safe="")
            + "/status"
        )
        try:
            payload = self._json_request("GetBuildStatus", "GET", path)
        except RemoteSandboxError:
            return ""
        if not isinstance(payload, dict):
            return ""
        build = payload.get("build")
        if isinstance(build, dict):
            payload = build
        status = _normalize_template_status(
            "e2b", str(payload.get("status") or payload.get("buildStatus") or "")
        )
        if status in {"building", "waiting"}:
            return ""
        return status

    def _e2b_has_successful_build(self, template_id: str) -> bool:
        try:
            payload = self._json_request(
                "GetTemplate",
                "GET",
                "/templates/" + urllib.parse.quote(template_id, safe=""),
            )
        except RemoteSandboxError:
            return False
        if not isinstance(payload, dict):
            return False
        template = payload.get("template")
        if isinstance(template, dict):
            payload = template
        builds = payload.get("builds")
        if not isinstance(builds, list):
            return False
        return any(
            _normalize_template_status("e2b", str(build.get("status") or "")) == "ready"
            for build in builds
            if isinstance(build, dict)
        )

    def _cube_template_spec(self, kind: str) -> dict[str, Any]:
        desktop = kind == "desktop"
        spec: dict[str, Any] = {
            "image": DEFAULT_CUBE_DESKTOP_IMAGE if desktop else DEFAULT_CUBE_IMAGE,
            "name": DESKTOP_TEMPLATE_NAME if desktop else STANDARD_TEMPLATE_NAME,
            "writableLayerSize": "8G" if desktop else "1G",
            "exposedPorts": [ENVD_PORT],
            "probePort": ENVD_PORT,
            "probePath": CUBE_ENVD_HEALTH_PATH,
            "allowInternetAccess": True,
        }
        if desktop:
            spec["command"] = ["/usr/bin/envd"]
            spec["args"] = ["-port", str(ENVD_PORT), "-isnotfc"]
        raw_dns = self.backend.get("dns_servers")
        if isinstance(raw_dns, list):
            dns: list[str] = []
            seen: set[str] = set()
            for value in raw_dns:
                candidate = str(value).strip()
                if not candidate:
                    continue
                try:
                    canonical = str(ipaddress.ip_address(candidate))
                except ValueError as exc:
                    raise ValueError(f"invalid cube DNS server {candidate!r} (need an IP address)") from exc
                if canonical not in seen:
                    seen.add(canonical)
                    dns.append(canonical)
            if dns:
                spec["dns"] = dns
        return spec

    def _cube_template_from_job(
        self, kind: str, job: Any, current: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        desktop = kind == "desktop"
        item = dict(current or {})
        payload = job if isinstance(job, dict) else {}
        template_id = str(
            payload.get("templateID") or payload.get("template_id") or item.get("id") or ""
        ).strip()
        item.update({
            "id": template_id,
            "name": DESKTOP_TEMPLATE_NAME if desktop else STANDARD_TEMPLATE_NAME,
            "image": DEFAULT_CUBE_DESKTOP_IMAGE if desktop else DEFAULT_CUBE_IMAGE,
            "standard": not desktop,
        })
        if desktop:
            item["desktop"] = True
        else:
            item.pop("desktop", None)
        status = _normalize_template_status(
            "cube", str(payload.get("status") or payload.get("buildStatus") or "")
        )
        item["status"] = status or "building"
        error = str(payload.get("errorMessage") or payload.get("lastError") or "").strip()
        if error:
            item["error"] = error
        else:
            item.pop("error", None)
        return item

    def _cube_build_template(self, kind: str) -> dict[str, Any]:
        desktop = kind == "desktop"
        image = DEFAULT_CUBE_DESKTOP_IMAGE if desktop else DEFAULT_CUBE_IMAGE
        try:
            job = self._json_request(
                "BuildTemplate", "POST", "/templates", self._cube_template_spec(kind)
            )
        except RemoteSandboxError as exc:
            failed: dict[str, Any] = {
                "id": "",
                "name": DESKTOP_TEMPLATE_NAME if desktop else STANDARD_TEMPLATE_NAME,
                "status": "failed",
                "image": image,
                "standard": not desktop,
                "error": exc.reason(),
            }
            if desktop:
                failed["desktop"] = True
            return failed
        return self._cube_template_from_job(kind, job)

    def _cube_rebuild_template(self, kind: str, current: dict[str, Any]) -> dict[str, Any]:
        template_id = str(current.get("id") or "").strip()
        if not template_id:
            raise RemoteSandboxError(
                self.provider, "RebuildTemplate", "template ID is required", kind="invalid_request"
            )
        job = self._json_request(
            "RebuildTemplate",
            "POST",
            "/templates/" + urllib.parse.quote(template_id, safe=""),
            self._cube_template_spec(kind),
        )
        return self._cube_template_from_job(kind, job, current)

    def _e2b_build_template(self, kind: str) -> dict[str, Any]:
        desktop = kind == "desktop"
        image = DEFAULT_E2B_DESKTOP_IMAGE if desktop else DEFAULT_E2B_IMAGE
        name = DESKTOP_TEMPLATE_NAME if desktop else STANDARD_TEMPLATE_NAME
        created = self._json_request(
            "BuildTemplate",
            "POST",
            "/v3/templates",
            {
                "alias": name,
                "tags": [DEFAULT_E2B_TEMPLATE_TAG],
                "cpuCount": 4 if desktop else 2,
                "memoryMB": 4096 if desktop else 2048,
            },
        )
        payload = created if isinstance(created, dict) else {}
        template_id = str(payload.get("templateID") or payload.get("template_id") or "").strip()
        build_id = str(payload.get("buildID") or payload.get("build_id") or "").strip()
        if not template_id or not build_id:
            raise RemoteSandboxError(
                self.provider,
                "BuildTemplate",
                "sandbox control plane returned an incomplete template build",
                kind="internal",
            )
        self._json_request(
            "StartTemplateBuild",
            "POST",
            "/v2/templates/"
            + urllib.parse.quote(template_id, safe="")
            + "/builds/"
            + urllib.parse.quote(build_id, safe=""),
            {
                "fromImage": image,
                "steps": [{"type": "run", "args": [E2B_PTY_PROMPT_OVERRIDE]}],
            },
        )
        item: dict[str, Any] = {
            "id": template_id,
            "name": name,
            "status": "building",
            "version": build_id,
            "image": image,
            "standard": not desktop,
        }
        if desktop:
            item["desktop"] = True
        return item

    def ensure_template(self, kind: str, *, replace: bool = False) -> dict[str, Any]:
        """Return the first-party template, building or rebuilding it when needed."""
        kind = kind.strip().lower()
        if kind not in {"standard", "desktop"}:
            raise ValueError("template kind must be standard or desktop")
        templates = self.list_templates()
        usable = pick_template(templates, kind)
        failed = next(
            (
                item
                for item in templates
                if item.get(kind)
                and str(item.get("status") or "").lower() in FAILED_TEMPLATE_STATUSES
            ),
            None,
        )
        if usable is not None and not replace:
            return usable
        if self.provider == "e2b":
            return self._e2b_build_template(kind)
        if replace:
            current = usable or failed
            if current is not None:
                try:
                    return self._cube_rebuild_template(kind, current)
                except RemoteSandboxError:
                    pass
            return self._cube_build_template(kind)
        if usable is None and failed is not None:
            try:
                return self._cube_rebuild_template(kind, failed)
            except RemoteSandboxError:
                return failed
        return self._cube_build_template(kind)

    def delete_superseded_templates(self, kind: str, keep_id: str) -> None:
        keep = keep_id.strip()
        if not keep:
            return
        try:
            templates = self.list_templates()
        except (RemoteSandboxError, ValueError):
            return
        for item in templates:
            template_id = str(item.get("id") or "").strip()
            if not item.get(kind) or not template_id or template_id == keep:
                continue
            try:
                self._json_request(
                    "DeleteTemplate",
                    "DELETE",
                    "/templates/" + urllib.parse.quote(template_id, safe=""),
                )
            except RemoteSandboxError as exc:
                if exc.status != 404:
                    continue

    def create(self, *, timeout_seconds: int = 120) -> RemoteSandbox:
        template = self.template_id
        if not template:
            raise RemoteSandboxError(
                self.provider, "Create", "template ID is required", kind="invalid_request"
            )
        network = _network_payload(self.provider, self.config_data)
        env_vars = self.config_data.get("env_vars")
        if not isinstance(env_vars, dict):
            env_vars = {}
        if self.provider == "cube":
            payload: dict[str, Any] = {
                "templateID": template,
                "timeout": timeout_seconds,
                "allowInternetAccess": not denies_egress_by_default(self.config_data),
                "network": network,
                "lifecycle": {"onTimeout": "kill", "autoResume": False},
            }
        else:
            payload = {
                "templateID": template,
                "timeout": timeout_seconds,
                "secure": True,
                "allow_internet_access": not denies_egress_by_default(self.config_data),
                "network": network,
                "autoPause": False,
            }
        if env_vars:
            payload["envVars"] = {str(key): str(value) for key, value in env_vars.items()}
        created = self._json_request("Create", "POST", "/sandboxes", payload)
        if not isinstance(created, dict):
            raise RemoteSandboxError(
                self.provider, "Create", "sandbox control plane returned an empty handle", kind="internal"
            )
        sandbox_id = str(created.get("sandboxID") or created.get("sandbox_id") or "").strip()
        if not sandbox_id:
            raise RemoteSandboxError(
                self.provider, "Create", "sandbox control plane returned an empty sandbox ID", kind="internal"
            )
        return RemoteSandbox(
            provider=self.provider,
            sandbox_id=sandbox_id,
            traffic_token=str(created.get("trafficAccessToken") or ""),
            envd_token=str(created.get("envdAccessToken") or created.get("envd_access_token") or ""),
            domain=str(created.get("domain") or ""),
        )

    def delete(self, sandbox: RemoteSandbox) -> None:
        self._json_request(
            "Delete",
            "DELETE",
            "/sandboxes/" + urllib.parse.quote(sandbox.sandbox_id, safe=""),
        )

    def _data_target(self, sandbox: RemoteSandbox, port: int = ENVD_PORT) -> tuple[str, str, str]:
        domain = sandbox.domain or self.sandbox_domain
        if not domain:
            domain = "cube.app" if self.provider == "cube" else "e2b.app"
        authority = f"{port}-{sandbox.sandbox_id}.{domain}"
        if self.proxy_url:
            proxy = urllib.parse.urlsplit(self.proxy_url)
            if proxy.scheme not in {"http", "https"} or not proxy.hostname:
                raise RemoteSandboxError(
                    self.provider, "Request", "proxy_url must be an absolute http(s) URL", kind="invalid_request"
                )
            port = proxy.port or (443 if proxy.scheme == "https" else 80)
            return proxy.scheme, f"{proxy.hostname}:{port}", authority
        return "https", authority, authority

    def desktop_target(self, sandbox: RemoteSandbox, secret: str) -> tuple[str, dict[str, str]]:
        scheme, dial_host, authority = self._data_target(sandbox, DESKTOP_PORT)
        headers = {
            "Host": authority,
            "Authorization": "Basic " + base64.b64encode(b"weknora:" + secret.encode()).decode("ascii"),
        }
        if sandbox.traffic_token:
            headers[TRAFFIC_TOKEN_HEADER] = sandbox.traffic_token
            headers["cube-traffic-access-token"] = sandbox.traffic_token
        if sandbox.envd_token:
            headers["X-Access-Token"] = sandbox.envd_token
        websocket_scheme = "wss" if scheme == "https" else "ws"
        return f"{websocket_scheme}://{dial_host}/websockify", headers

    def _terminal_headers(self, sandbox: RemoteSandbox, *, content_type: str) -> dict[str, str]:
        _, _, authority = self._data_target(sandbox)
        headers = {
            "Host": authority,
            "Authorization": "Basic " + base64.b64encode(b"root:").decode("ascii"),
            "Content-Type": content_type,
            "Connect-Protocol-Version": "1",
        }
        if sandbox.traffic_token:
            headers[TRAFFIC_TOKEN_HEADER] = sandbox.traffic_token
            headers["cube-traffic-access-token"] = sandbox.traffic_token
        if sandbox.envd_token:
            headers["X-Access-Token"] = sandbox.envd_token
        return headers

    def _terminal_rpc(self, sandbox: RemoteSandbox, operation: str, payload: bytes) -> None:
        scheme, dial_host, _ = self._data_target(sandbox)
        content_type = "application/json" if self.provider == "cube" else "application/proto"
        headers = self._terminal_headers(sandbox, content_type=content_type)
        response = self._request(
            "Terminal " + operation,
            "POST",
            f"{scheme}://{dial_host}/process.Process/{operation}",
            headers,
            payload,
            timeout=self.timeout,
        )
        if not 200 <= response.status < 300:
            raise _classify_status(self.provider, "Terminal " + operation, response.status, response.body)

    def _open_terminal_stream(
        self,
        sandbox: RemoteSandbox,
        operation: str,
        payload: bytes,
    ) -> EnvdTerminalSession:
        scheme, dial_host, authority = self._data_target(sandbox)
        parsed = urllib.parse.urlsplit("//" + dial_host)
        if not parsed.hostname:
            raise RemoteSandboxError(self.provider, "Terminal", "invalid envd endpoint", kind="internal")
        port = parsed.port or (443 if scheme == "https" else 80)
        content_type = "application/connect+json" if self.provider == "cube" else "application/connect+proto"
        headers = self._terminal_headers(sandbox, content_type=content_type)
        headers.update({
            "Connect-Content-Encoding": "identity",
            "Connect-Timeout-Ms": str(24 * 60 * 60 * 1000),
            "Keepalive-Ping-Interval": "50",
        })
        if scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname,
                port,
                timeout=self.timeout,
                context=ssl.create_default_context(),
            )
        else:
            connection = http.client.HTTPConnection(parsed.hostname, port, timeout=self.timeout)
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self.timeout)
            connection.putrequest(
                "POST",
                f"/process.Process/{operation}",
                skip_host=True,
                skip_accept_encoding=True,
            )
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            if response.status < 200 or response.status >= 300:
                detail = response.read().decode("utf-8", errors="replace")
                response.close()
                raise _classify_status(self.provider, "Terminal", response.status, detail.encode("utf-8"))
            if connection.sock is not None:
                connection.sock.settimeout(None)
            session = EnvdTerminalSession(
                self,
                sandbox,
                scheme=scheme,
                dial_host=dial_host,
                authority=authority,
                connection=connection,
                response=response,
            )
            session.begin()
            return session
        except RemoteSandboxError:
            connection.close()
            raise
        except (OSError, EOFError, http.client.HTTPException) as exc:
            connection.close()
            raise RemoteSandboxError(
                self.provider, "Terminal", str(exc) or "terminal stream failed", kind="unavailable"
            ) from exc

    def open_terminal(
        self,
        sandbox: RemoteSandbox,
        *,
        cols: int = 80,
        rows: int = 24,
        cwd: str = "/workspace",
        attach_pid: int = 0,
        env: Mapping[str, str] | None = None,
    ) -> EnvdTerminalSession:
        values = {"TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        values.update({str(key): str(value) for key, value in (env or {}).items()})
        if attach_pid > 0:
            if self.provider == "cube":
                connect_payload = json.dumps({"process": {"pid": int(attach_pid)}}).encode("utf-8")
            else:
                connect_payload = _e2b_terminal_connect_request(int(attach_pid))
            try:
                return self._open_terminal_stream(
                    sandbox, "Connect", _connect_envelope(connect_payload)
                )
            except RemoteSandboxError as exc:
                if exc.kind == "authentication":
                    raise
        if self.provider == "cube":
            payload = json.dumps({
                "process": {
                    "cmd": "/bin/bash",
                    "args": ["-l"],
                    "envs": values,
                    "cwd": cwd or "/workspace",
                },
                "pty": {"size": {"cols": max(1, int(cols)), "rows": max(1, int(rows))}},
                "stdin": True,
            }).encode("utf-8")
        else:
            payload = _e2b_terminal_start_request(cols, rows, cwd or "/workspace", values)
        return self._open_terminal_stream(sandbox, "Start", _connect_envelope(payload))

    def exec(
        self,
        sandbox: RemoteSandbox,
        command: str,
        *,
        timeout: int = 30,
        env: Mapping[str, str] | None = None,
    ) -> RemoteExecResult:
        scheme, dial_host, authority = self._data_target(sandbox)
        url = f"{scheme}://{dial_host}/process.Process/Start"
        headers = {
            "Host": authority,
            "Authorization": "Basic " + base64.b64encode(b"root:").decode("ascii"),
            "Connect-Protocol-Version": "1",
            "Connect-Content-Encoding": "identity",
            "Connect-Timeout-Ms": str(max(1, timeout) * 1000),
            "Keepalive-Ping-Interval": "50",
        }
        if sandbox.traffic_token:
            headers[TRAFFIC_TOKEN_HEADER] = sandbox.traffic_token
            headers["cube-traffic-access-token"] = sandbox.traffic_token
        if sandbox.envd_token:
            headers["X-Access-Token"] = sandbox.envd_token

        values = {str(key): str(value) for key, value in (env or {}).items()}
        if self.provider == "cube":
            headers["Content-Type"] = "application/connect+json"
            payload = json.dumps({
                "process": {
                    "cmd": "/bin/bash",
                    "args": ["-l", "-c", command],
                    "envs": values,
                },
                "stdin": False,
            }).encode("utf-8")
        else:
            headers["Content-Type"] = "application/connect+proto"
            payload = _e2b_start_request(command, values)

        started = time.perf_counter()
        response = self._request(
            "Exec",
            "POST",
            url,
            headers,
            _connect_envelope(payload),
            timeout=max(self.timeout, timeout + 5),
        )
        if not 200 <= response.status < 300:
            raise _classify_status(self.provider, "Exec", response.status, response.body)
        latency = int((time.perf_counter() - started) * 1000)
        try:
            stdout, stderr, exit_code = self._decode_exec_stream(response.body)
        except ValueError as exc:
            raise RemoteSandboxError(
                self.provider, "Exec", "sandbox returned a malformed command stream", kind="internal"
            ) from exc
        return RemoteExecResult(stdout, stderr, exit_code, latency)

    def _decode_exec_stream(self, body: bytes) -> tuple[str, str, int]:
        stdout = bytearray()
        stderr = bytearray()
        exit_code: int | None = None
        for flags, payload in _connect_frames(body):
            if flags & 0x01:
                raise ValueError("compressed Connect message")
            if flags & 0x02:
                _connect_end(payload, self.provider, "Exec")
                continue
            if self.provider == "cube":
                decoded = json.loads(payload.decode("utf-8"))
                event = decoded.get("event") if isinstance(decoded, dict) else None
                if not isinstance(event, dict):
                    continue
                data = event.get("data")
                if isinstance(data, dict):
                    for key, target in (("stdout", stdout), ("stderr", stderr)):
                        value = data.get(key)
                        if isinstance(value, str) and value:
                            target += base64.b64decode(value)
                end = event.get("end")
                if isinstance(end, dict):
                    code = end.get("exitCode", end.get("exit_code"))
                    if isinstance(code, int):
                        exit_code = code
                    elif bool(end.get("exited")):
                        status = str(end.get("status") or "")
                        if status.startswith("exit status "):
                            try:
                                exit_code = int(status.removeprefix("exit status ").strip())
                            except ValueError:
                                exit_code = 0
                        else:
                            exit_code = 0
            else:
                out, err, code, _status = _decode_e2b_process_event(payload)
                stdout += out
                stderr += err
                if code is not None:
                    exit_code = code
        if exit_code is None:
            raise ValueError("process stream ended without an exit event")
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            exit_code,
        )


def effective_template_id(provider: str, backend: Mapping[str, Any], config_data: Mapping[str, Any]) -> str:
    template = str(backend.get("template_id") or "").strip()
    if template:
        return template
    skill_image = config_data.get("skill_image")
    if isinstance(skill_image, dict):
        candidate = skill_image.get("image") if provider == "cube" else skill_image.get("template_id")
        if candidate:
            return str(candidate).strip()
    return ""

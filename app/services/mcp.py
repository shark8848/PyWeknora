"""MCP client used by service discovery, tests and agent tool execution."""

from __future__ import annotations

import base64
import hashlib
import json
import queue
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import config, db
from ..utils import loads


MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION = LEGACY_PROTOCOL_VERSION
STREAMABLE_TRANSPORTS = {"http-streamable", "streamable-http", "streamable_http", "http"}
SSE_TRANSPORTS = {"sse"}
STDIO_TRANSPORTS = {"stdio"}
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_MCP_IMAGES = 5
MAX_MCP_IMAGE_BYTES = 10 * 1024 * 1024
MCP_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
HEADER_PARAM_PREFIX = "Mcp-Param-"
_BASE64_HEADER_PREFIX = "=?base64?"
_BASE64_HEADER_SUFFIX = "?="
_MAX_SAFE_INTEGER = 1 << 53 - 1


class MCPError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        oauth_required: bool = False,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.oauth_required = oauth_required
        self.transient = transient


def _mapping(value: Any) -> dict[str, Any]:
    result = loads(value, {})
    return result if isinstance(result, dict) else {}


def normalize_transport(value: Any) -> str:
    transport = str(value or "http-streamable").strip().lower()
    if transport in STREAMABLE_TRANSPORTS:
        return "http-streamable"
    if transport in SSE_TRANSPORTS:
        return "sse"
    if transport in STDIO_TRANSPORTS:
        return "stdio"
    return transport


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MCPError("MCP endpoint must be an absolute http(s) URL")
    if (parsed.hostname or "").lower() in {"169.254.169.254", "metadata.google.internal"}:
        raise MCPError("MCP endpoint points at a blocked metadata host")
    return url


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _header(headers: dict[str, str], name: str) -> str:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def _param_header_value(value: Any) -> str | None:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            return None
        text = str(value)
    elif isinstance(value, float):
        if not value.is_integer() or abs(value) > _MAX_SAFE_INTEGER:
            return None
        text = str(int(value))
    elif isinstance(value, str):
        text = value
    else:
        return None
    needs_base64 = bool(text) and (
        text[0] in " \t"
        or text[-1] in " \t"
        or any(ord(char) < 0x20 or ord(char) > 0x7E for char in text)
        or (
            text.startswith(_BASE64_HEADER_PREFIX)
            and text.endswith(_BASE64_HEADER_SUFFIX)
        )
    )
    if not needs_base64:
        return text
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"{_BASE64_HEADER_PREFIX}{encoded}{_BASE64_HEADER_SUFFIX}"


def _param_header_bindings(schema: Any) -> list[tuple[tuple[str, ...], str]]:
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return []
    bindings: list[tuple[tuple[str, ...], str]] = []

    def collect(items: dict[str, Any], prefix: tuple[str, ...]) -> None:
        for name, prop in items.items():
            if not isinstance(prop, dict):
                continue
            path = (*prefix, str(name))
            header = prop.get("x-mcp-header")
            if isinstance(header, str) and header:
                bindings.append((path, header))
            nested = prop.get("properties")
            if isinstance(nested, dict):
                collect(nested, path)

    collect(properties, ())
    return bindings


def _param_headers(schema: Any, arguments: dict[str, Any] | None) -> dict[str, str]:
    bindings = _param_header_bindings(schema)
    if not bindings or not isinstance(arguments, dict):
        return {}
    headers: dict[str, str] = {}
    for path, header in bindings:
        current: Any = arguments
        for part in path:
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current[part]
        if current is None:
            continue
        value = _param_header_value(current)
        if value is not None:
            headers[HEADER_PARAM_PREFIX + header] = value
    return headers


def _auth_headers(service: dict[str, Any], access_token: str = "") -> dict[str, str]:
    headers = {
        str(key): str(value)
        for key, value in _mapping(service.get("headers")).items()
        if value is not None
    }
    auth = _mapping(service.get("auth_config"))
    auth_type = str(auth.get("auth_type") or "").strip().lower()
    if auth_type == "api_key":
        api_key = str(auth.get("api_key") or "")
        if api_key:
            headers[str(auth.get("api_key_header") or "X-API-Key")] = api_key
    elif auth_type == "bearer":
        token = str(auth.get("token") or "")
        if token:
            headers["Authorization"] = "Bearer " + token
    elif auth_type == "oauth":
        if access_token:
            headers["Authorization"] = "Bearer " + access_token
    else:
        if auth.get("api_key"):
            headers[str(auth.get("api_key_header") or "X-API-Key")] = str(auth["api_key"])
        if auth.get("token"):
            headers["Authorization"] = "Bearer " + str(auth["token"])
    headers.update(
        {
            str(key): str(value)
            for key, value in _mapping(auth.get("custom_headers")).items()
            if value is not None
        }
    )
    return headers


def oauth_access_token(tenant_id: int, user_id: str, service_id: str) -> str:
    from . import mcp_oauth

    service = db.fetch_one(
        "SELECT * FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (service_id, tenant_id),
    )
    if not service:
        return ""
    try:
        return mcp_oauth.refresh_access_token(service, tenant_id, user_id)
    except mcp_oauth.OAuthError:
        return ""


def configuration_fingerprint(service: dict[str, Any]) -> str:
    identity = {
        "transport": normalize_transport(service.get("transport_type")),
        "url": service.get("url") or "",
        "headers": _mapping(service.get("headers")),
        "auth": _mapping(service.get("auth_config")),
        "stdio": _mapping(service.get("stdio_config")),
        "env": _mapping(service.get("env_vars")),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request(
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, str], bytes, str]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            response_headers = {str(key): str(value) for key, value in response.headers.items()}
            return response.status, response_headers, body, response.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        response_headers = {str(key): str(value) for key, value in exc.headers.items()}
        return exc.code, response_headers, body, exc.headers.get_content_type() if exc.headers else ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MCPError(f"MCP request failed: {exc}", transient=True) from exc


def _read_sse_event(stream: Any) -> tuple[str, str] | None:
    event = ""
    data: list[str] = []
    while True:
        raw = stream.readline()
        if not raw:
            if event or data:
                return event or "message", "\n".join(data)
            return None
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if event or data:
                return event or "message", "\n".join(data)
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)


class _SseChannel:
    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.response: Any = None
        self.endpoint = ""
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.error: Exception | None = None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    def connect(self) -> None:
        headers = {**self.headers, "Accept": "text/event-stream", "Cache-Control": "no-cache"}
        request = urllib.request.Request(self.url, headers=headers, method="GET")
        try:
            self.response = urllib.request.urlopen(request, timeout=self.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MCPError(f"MCP SSE connection failed: {exc}") from exc
        deadline = 0
        while not self.endpoint and deadline < 40:
            event = _read_sse_event(self.response)
            if event is None:
                raise MCPError("MCP SSE stream closed before sending its endpoint")
            name, data = event
            deadline += 1
            if name == "endpoint" and data:
                self.endpoint = urllib.parse.urljoin(self.url, data.strip())
        if not self.endpoint:
            raise MCPError("MCP SSE endpoint was not announced")
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self) -> None:
        try:
            while not self.stop.is_set():
                event = _read_sse_event(self.response)
                if event is None:
                    break
                _name, data = event
                if not data:
                    continue
                try:
                    message = json.loads(data)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self.responses.put(message)
        except Exception as exc:  # noqa: BLE001
            self.error = exc
        finally:
            self.responses.put({"__closed__": True})

    def post(self, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        headers = {
            **self.headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        status, _headers, body, _content_type = _request(self.endpoint, payload, headers, self.timeout)
        if status == 401:
            raise MCPError("MCP server rejected the request with HTTP 401")
        if status >= 400:
            raise MCPError(f"MCP server returned HTTP {status}: {body.decode('utf-8', errors='replace')[:300]}")
        if body:
            try:
                message = json.loads(body)
            except ValueError:
                message = None
            if isinstance(message, dict):
                self.responses.put(message)

    def wait(self, request_id: int, timeout: float) -> dict[str, Any]:
        deadline = threading.Event()
        try:
            message = self.responses.get(timeout=timeout)
        except queue.Empty:
            raise MCPError("Timed out waiting for MCP SSE response") from None
        finally:
            deadline.set()
        while True:
            if isinstance(message, dict) and message.get("__closed__"):
                if self.error:
                    raise MCPError(f"MCP SSE stream failed: {self.error}")
                raise MCPError("MCP SSE stream closed before responding")
            if isinstance(message, dict) and message.get("id") == request_id:
                return message
            try:
                message = self.responses.get(timeout=timeout)
            except queue.Empty:
                raise MCPError("Timed out waiting for MCP SSE response") from None

    def close(self) -> None:
        self.stop.set()
        try:
            if self.response is not None:
                self.response.close()
        except Exception:  # noqa: BLE001
            pass


class MCPClient:
    def __init__(
        self,
        service: dict[str, Any],
        *,
        tenant_id: int = 0,
        user_id: str = "",
    ) -> None:
        self.service = dict(service)
        self.transport = normalize_transport(service.get("transport_type"))
        advanced = _mapping(service.get("advanced_config"))
        self.timeout = min(120.0, max(1.0, float(advanced.get("timeout") or 30)))
        access_token = ""
        if str(_mapping(service.get("auth_config")).get("auth_type") or "") == "oauth":
            access_token = oauth_access_token(tenant_id, user_id, str(service.get("id") or ""))
        self.headers = _auth_headers(service, access_token)
        self.session_id = ""
        self.protocol_version = ""
        self._modern_protocol = False
        self.initialize_result: dict[str, Any] = {}
        self.instructions = ""
        self._next_id = 1
        self._sse: _SseChannel | None = None
        self._tool_schemas: dict[str, dict[str, Any]] = {}

    @property
    def connected(self) -> bool:
        if self.transport == "sse":
            return bool(self._sse and self._sse.endpoint)
        return self.transport == "http-streamable"

    @property
    def server_info(self) -> dict[str, Any]:
        value = self.initialize_result.get("serverInfo")
        return value if isinstance(value, dict) else {}

    @property
    def capabilities(self) -> dict[str, Any]:
        value = self.initialize_result.get("capabilities")
        return value if isinstance(value, dict) else {}

    def connect(self) -> None:
        if self.transport == "stdio":
            raise MCPError("stdio transport is disabled for security reasons; use SSE or HTTP Streamable")
        if self.transport == "sse":
            if self._sse is None:
                url = _validate_url(str(self.service.get("url") or ""))
                self._sse = _SseChannel(url, self.headers, self.timeout)
                self._sse.connect()
            return
        if self.transport != "http-streamable":
            raise MCPError(f"Unsupported MCP transport: {self.transport}")
        _validate_url(str(self.service.get("url") or ""))

    def _request_headers(
        self,
        method: str,
        params: dict[str, Any] | None,
        modern: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {**self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if modern:
            headers["MCP-Protocol-Version"] = MODERN_PROTOCOL_VERSION
            headers["Mcp-Method"] = method
            if method == "tools/call" and isinstance(params, dict):
                name = str(params.get("name") or "").strip()
                if name:
                    headers["Mcp-Name"] = name
        headers.update(extra_headers or {})
        return headers

    def _rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        notification: bool = False,
        modern: bool | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        self.connect()
        use_modern = self._modern_protocol if modern is None else modern
        if use_modern and not notification:
            params = dict(params or {})
            meta = params.get("_meta")
            if not isinstance(meta, dict):
                meta = {}
            meta.setdefault(
                "io.modelcontextprotocol/protocolVersion", MODERN_PROTOCOL_VERSION
            )
            meta.setdefault("io.modelcontextprotocol/clientCapabilities", {})
            params["_meta"] = meta
        request_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            payload["id"] = request_id
        request_headers = self._request_headers(method, params, use_modern, extra_headers)
        if self._sse is not None:
            self._sse.post(payload, request_headers)
            if notification:
                return None
            message = self._sse.wait(request_id, self.timeout)
        else:
            url = str(self.service.get("url") or "")
            status, headers, body, content_type = _request(
                url,
                payload,
                request_headers,
                self.timeout,
            )
            session = _header(headers, "Mcp-Session-Id")
            if session:
                self.session_id = session
            if status == 401:
                challenge = _header(headers, "WWW-Authenticate")
                raise MCPError(
                    "MCP server requires OAuth authorization",
                    oauth_required="resource_metadata" in challenge.lower(),
                    transient=True,
                )
            if status >= 400:
                raise MCPError(
                    f"MCP server returned HTTP {status}: {body.decode('utf-8', errors='replace')[:300]}",
                    transient=status >= 500,
                )
            if notification:
                if body and content_type == "application/json":
                    try:
                        message = json.loads(body)
                    except ValueError:
                        return None
                    if isinstance(message, dict) and message.get("error"):
                        raise MCPError(str(message["error"].get("message") or message["error"]))
                return None
            message = None
            if "text/event-stream" in content_type:
                for event in body.decode("utf-8", errors="replace").split("\n\n"):
                    data = "\n".join(
                        line[5:].lstrip()
                        for line in event.splitlines()
                        if line.startswith("data:")
                    )
                    if not data:
                        continue
                    try:
                        candidate = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(candidate, dict) and candidate.get("id") == request_id:
                        message = candidate
                        break
            else:
                try:
                    message = json.loads(body) if body else None
                except ValueError as exc:
                    raise MCPError("MCP server returned invalid JSON") from exc
            if not isinstance(message, dict):
                raise MCPError("MCP server returned no JSON-RPC response")
        error = message.get("error")
        if error:
            text = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPError(str(text or "MCP JSON-RPC error"))
        return message.get("result")

    def initialize(self) -> dict[str, Any]:
        if self.transport == "http-streamable":
            discovered = None
            for attempt in range(2):
                try:
                    discovered = self._rpc("server/discover", {}, modern=True)
                    break
                except MCPError as exc:
                    if not exc.transient or attempt == 1:
                        break
            if isinstance(discovered, dict):
                versions = discovered.get("supportedVersions") or discovered.get("protocolVersions")
                if isinstance(versions, list) and MODERN_PROTOCOL_VERSION in versions:
                    self._modern_protocol = True
                    self.protocol_version = MODERN_PROTOCOL_VERSION
                    self.initialize_result = discovered
                    self.instructions = str(discovered.get("instructions") or "")
                    return self.initialize_result
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "PyWeKnora", "version": config.APP_VERSION},
            },
        )
        self.initialize_result = result if isinstance(result, dict) else {}
        self.protocol_version = str(self.initialize_result.get("protocolVersion") or "")
        if self.protocol_version == MODERN_PROTOCOL_VERSION:
            self._modern_protocol = True
        self.instructions = str(self.initialize_result.get("instructions") or "")
        self._rpc("notifications/initialized", {}, notification=True)
        return self.initialize_result

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        schemas: dict[str, dict[str, Any]] = {}
        cursor = ""
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("tools/list", params) or {}
            for tool in result.get("tools") or []:
                if isinstance(tool, dict) and tool.get("name"):
                    name = str(tool.get("name"))
                    schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"}
                    tools.append(
                        {
                            "name": name,
                            "description": str(tool.get("description") or ""),
                            "inputSchema": schema,
                        }
                    )
                    schemas[name] = schema if isinstance(schema, dict) else {"type": "object"}
            cursor = str(result.get("nextCursor") or "")
            if not cursor:
                self._tool_schemas = schemas
                return tools

    def list_resources(self) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._rpc("resources/list", params) or {}
            for resource in result.get("resources") or []:
                if isinstance(resource, dict) and resource.get("uri"):
                    resources.append(
                        {
                            "uri": str(resource.get("uri")),
                            "name": str(resource.get("name") or ""),
                            "description": str(resource.get("description") or ""),
                            "mimeType": str(resource.get("mimeType") or ""),
                        }
                    )
            cursor = str(result.get("nextCursor") or "")
            if not cursor:
                return resources

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if not name:
            raise MCPError("Tool name is required")
        call_arguments = arguments or {}
        extra_headers: dict[str, str] = {}
        if self._modern_protocol:
            schema = self._tool_schemas.get(name)
            if schema is None:
                self.list_tools()
                schema = self._tool_schemas.get(name)
                if schema is None:
                    raise MCPError(f'tool "{name}" not found in MCP directory')
            extra_headers = _param_headers(schema, call_arguments)
        result = self._rpc(
            "tools/call",
            {"name": name, "arguments": call_arguments},
            extra_headers=extra_headers,
        ) or {}
        content = result.get("content") if isinstance(result, dict) else []
        if not isinstance(content, list):
            content = []
        return {
            "content": content,
            "structuredContent": result.get("structuredContent") if isinstance(result, dict) else None,
            "isError": bool(result.get("isError")) if isinstance(result, dict) else False,
        }

    def read_resource(self, uri: str) -> dict[str, Any]:
        if not uri:
            raise MCPError("Resource URI is required")
        result = self._rpc("resources/read", {"uri": uri}) or {}
        contents = result.get("contents") if isinstance(result, dict) else []
        return {"contents": contents if isinstance(contents, list) else []}

    def close(self) -> None:
        if self._sse is not None:
            self._sse.close()
            self._sse = None

    def __enter__(self) -> "MCPClient":
        self.connect()
        self.initialize()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _resource_text(item: dict[str, Any]) -> str:
    value = item
    if item.get("type") == "resource" and isinstance(item.get("resource"), dict):
        value = item["resource"]
    label = "Resource link" if item.get("type") == "resource_link" else "Resource"
    uri = str(value.get("uri") or item.get("uri") or "")
    mime_type = str(value.get("mimeType") or item.get("mimeType") or "")
    reference = f"{uri} ({mime_type})" if uri and mime_type else uri or mime_type
    text = str(value.get("text") or "") if item.get("type") == "resource" else ""
    rendered = f"[{label}: {reference}]"
    return rendered + (f"\n{text}" if text else "")


def tool_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    content = result.get("content") or []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
        elif item.get("type") in {"resource", "resource_link"}:
            parts.append(_resource_text(item))
        elif item.get("type") == "image":
            parts.append(f"[Image: {str(item.get('mimeType') or 'image/png')}]")
    text = "\n".join(parts)
    structured = result.get("structuredContent")
    if structured is None:
        return text
    payload = json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            fallback = json.loads(str(item.get("text") or ""))
        except (TypeError, ValueError):
            continue
        canonical = json.dumps(fallback, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if canonical == payload:
            return text
    prefix = text + "\n\n" if text else ""
    return prefix + "Structured content:\n" + payload


def tool_images(result: dict[str, Any]) -> list[str]:
    images: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        mime_type = str(item.get("mimeType") or "image/png")
        data = str(item.get("data") or "")
        if (
            data
            and mime_type in MCP_IMAGE_MIMES
            and len(data) * 3 // 4 <= MAX_MCP_IMAGE_BYTES
            and len(images) < MAX_MCP_IMAGES
        ):
            images.append(f"data:{mime_type};base64,{data}")
    return images


def metadata_payload(client: MCPClient) -> dict[str, Any]:
    tools = client.list_tools()
    names = [str(tool.get("name") or "") for tool in tools]
    if any(not name for name in names) or len(set(names)) != len(names):
        raise MCPError("MCP directory contains empty or duplicate tool names")
    if len(json.dumps(tools, ensure_ascii=False).encode("utf-8")) > MAX_METADATA_BYTES:
        raise MCPError("MCP metadata exceeds the 8 MiB storage limit")
    info = client.server_info
    return {
        "tools": tools,
        "instructions": client.instructions,
        "server_name": str(info.get("name") or info.get("title") or ""),
        "server_version": str(info.get("version") or ""),
        "server_description": str(info.get("description") or ""),
    }

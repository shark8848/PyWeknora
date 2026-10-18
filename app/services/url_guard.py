"""Fetch remote resources without exposing the server network to SSRF."""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import socket
import ssl
import urllib.parse
from typing import Any, Iterator


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_OUTBOUND_DIAL_TIMEOUT = 5.0


class URLGuardError(ValueError):
    """A remote URL is malformed, resolves internally, or cannot be fetched."""


def _duration_seconds(value: str, default: float) -> float:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(ns|us|µs|ms|s|m|h)?\s*", value or "")
    if not match:
        return default
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {
        "ns": 1e-9,
        "us": 1e-6,
        "µs": 1e-6,
        "ms": 1e-3,
        "s": 1,
        "m": 60,
        "h": 3600,
    }[unit]
    seconds = amount * multiplier
    return seconds if seconds > 0 else default


def outbound_dial_timeout() -> float:
    raw = str(os.environ.get("WEKNORA_OUTBOUND_DIAL_TIMEOUT") or "")
    return _duration_seconds(raw, DEFAULT_OUTBOUND_DIAL_TIMEOUT)


def _address_reason(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise URLGuardError("remote host resolved to an invalid address") from exc
    if isinstance(address, ipaddress.IPv6Address):
        embedded = address.ipv4_mapped or address.sixtofour
        if embedded is not None:
            reason = _address_reason(str(embedded))
            if reason:
                return reason
        if address.teredo is not None:
            for item in address.teredo:
                reason = _address_reason(str(item))
                if reason:
                    return reason
        if address in ipaddress.ip_network("64:ff9b::/96"):
            reason = _address_reason(str(ipaddress.IPv4Address(address.packed[-4:])))
            if reason:
                return reason
    if address.is_unspecified:
        return "unspecified address"
    if address.is_loopback:
        return "loopback address"
    if address.is_link_local:
        return "link-local address"
    if address.is_multicast:
        return "multicast address"
    if address.is_private:
        return "private address"
    if address.is_reserved:
        return "reserved address"
    if not address.is_global:
        return "non-public address"
    return ""


def _ascii_host(host: str) -> str:
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLGuardError("remote host is not a valid DNS name") from exc


def _split_whitelist(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _database_whitelist() -> list[str] | None:
    try:
        from .. import db

        row = db.fetch_one("SELECT value FROM system_settings WHERE key='ssrf.whitelist'")
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    raw = str(row.get("value") or "")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, list):
        return None
    return [str(item).strip() for item in value if str(item).strip()]


def validate_whitelist_entries(entries: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in entries:
        item = str(raw).strip()
        if not item:
            continue
        if "/" in item:
            try:
                ipaddress.ip_network(item, strict=True)
            except ValueError as exc:
                raise URLGuardError(f"invalid CIDR {item!r}") from exc
        elif item.startswith("*."):
            if len(item) <= 2:
                raise URLGuardError(f"wildcard entry {item!r} is missing a domain")
        elif "*" in item:
            raise URLGuardError(f"unsupported wildcard pattern {item!r}")
        normalized.append(item)
    return normalized


def _whitelist_config() -> tuple[set[str], list[str], list[ipaddress.IPv4Network | ipaddress.IPv6Network]]:
    exact: set[str] = set()
    suffixes: list[str] = []
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    entries = _database_whitelist()
    if entries is None:
        entries = _split_whitelist(os.environ.get("SSRF_WHITELIST", ""))
    entries.extend(_split_whitelist(os.environ.get("SSRF_WHITELIST_EXTRA", "")))
    for entry in entries:
        item = entry.strip()
        if not item:
            continue
        if "/" in item:
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                continue
        elif item.startswith("*."):
            if len(item) > 2:
                suffixes.append(item[1:].lower())
        elif "*" not in item:
            exact.add(item.lower())
    return exact, suffixes, networks


def is_whitelisted(hostname: str) -> bool:
    host = str(hostname or "").strip().lower()
    if not host:
        return False
    exact, suffixes, networks = _whitelist_config()
    if host in exact:
        return True
    if any(host.endswith(suffix) or host == suffix[1:] for suffix in suffixes):
        return True
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return any(literal in network for network in networks if network.version == literal.version)
    if not networks:
        return False
    try:
        records = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for record in records:
        try:
            address = ipaddress.ip_address(str(record[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if any(address in network for network in networks if network.version == address.version):
            return True
    return False


def _resolve_addresses(host: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [str(literal)]
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLGuardError("remote host could not be resolved") from exc
    addresses: list[str] = []
    for record in records:
        value = str(record[4][0]).split("%", 1)[0]
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise URLGuardError("remote host did not resolve to an address")
    return addresses


def _resolve_public_addresses(host: str, port: int) -> list[str]:
    addresses = _resolve_addresses(host, port)
    for value in addresses:
        reason = _address_reason(value)
        if reason:
            raise URLGuardError(f"remote host resolves to a {reason}")
    return addresses


def _without_credentials(parsed: urllib.parse.SplitResult) -> str:
    host = _ascii_host(parsed.hostname or "")
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment))


def validate_url_for_ssrf(url: str, *, allow_credentials: bool = False) -> urllib.parse.SplitResult:
    """Validate an outbound URL, allowing private hosts only via SSRF_WHITELIST."""
    if not isinstance(url, str) or not url or url != url.strip():
        raise URLGuardError("remote URL is invalid")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise URLGuardError("remote URL must be an absolute http(s) URL")
    if not allow_credentials and (parsed.username is not None or parsed.password is not None):
        raise URLGuardError("remote URL must not contain credentials")
    if is_whitelisted(parsed.hostname):
        return parsed
    return validate_public_url(_without_credentials(parsed) if allow_credentials else url)[0]


def validate_proxy_url(proxy_url: str) -> urllib.parse.SplitResult | None:
    value = str(proxy_url or "").strip()
    if not value:
        return None
    return validate_url_for_ssrf(value, allow_credentials=True)


def request_url(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30,
    max_bytes: int = 10 * 1024 * 1024,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> tuple[int, dict[str, str], bytes, str]:
    """Perform an outbound request against validated, pinned addresses."""
    current = url
    current_method = method.upper()
    current_body = body
    for hop in range(max_redirects + 1):
        parsed = urllib.parse.urlsplit(current)
        if is_whitelisted(parsed.hostname or ""):
            validate_url_for_ssrf(current)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = _resolve_addresses(_ascii_host(parsed.hostname or ""), port)
        else:
            parsed, addresses = validate_public_url(current)
        status, response_headers, response_body = _fetch_once(
            parsed,
            addresses,
            headers or {},
            timeout,
            max_bytes,
            method=current_method,
            body=current_body,
        )
        if status not in REDIRECT_STATUSES:
            return status, response_headers, response_body, current
        location = str(response_headers.get("location") or "").strip()
        if not location:
            raise URLGuardError("remote redirect is missing a Location header")
        if hop >= max_redirects:
            raise URLGuardError("remote URL redirected too many times")
        current = urllib.parse.urljoin(current, location)
        if status in {301, 302, 303} and current_method != "HEAD":
            current_method = "GET"
            current_body = None
    raise URLGuardError("remote URL redirected too many times")


def validate_public_url(url: str) -> tuple[urllib.parse.SplitResult, list[str]]:
    """Validate one hop and resolve every address before a connection is made."""
    if not isinstance(url, str) or not url or url != url.strip():
        raise URLGuardError("remote URL is invalid")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise URLGuardError("remote URL must be an absolute http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise URLGuardError("remote URL must not contain credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise URLGuardError("remote URL has an invalid port") from exc
    if not 1 <= port <= 65535:
        raise URLGuardError("remote URL has an invalid port")
    host = _ascii_host(parsed.hostname)
    return parsed, _resolve_public_addresses(host, port)


def _host_header(parsed: urllib.parse.SplitResult) -> str:
    host = _ascii_host(parsed.hostname or "")
    default_port = 443 if parsed.scheme == "https" else 80
    if ":" in host:
        host = f"[{host}]"
    if parsed.port and parsed.port != default_port:
        host = f"{host}:{parsed.port}"
    return host


def _request_target(parsed: urllib.parse.SplitResult) -> str:
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port),
            min(float(self.timeout or DEFAULT_OUTBOUND_DIAL_TIMEOUT), outbound_dial_timeout()),
            self.source_address,
        )
        self.sock.settimeout(self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._pinned_address, self.port),
            min(float(self.timeout or DEFAULT_OUTBOUND_DIAL_TIMEOUT), outbound_dial_timeout()),
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        self.sock.settimeout(self.timeout)


def _fetch_once(
    parsed: urllib.parse.SplitResult,
    addresses: list[str],
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
    *,
    method: str = "GET",
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    host = _ascii_host(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    request_headers = {
        "Host": _host_header(parsed),
        "Connection": "close",
        **headers,
    }
    last_error: Exception | None = None
    for address in addresses:
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHTTPSConnection(
                host, port, address, timeout, ssl.create_default_context()
            )
        else:
            connection = _PinnedHTTPConnection(host, port, address, timeout)
        try:
            connection.request(method, _request_target(parsed), body=body, headers=request_headers)
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise URLGuardError("remote response exceeds the download limit")
            body = response.read(max_bytes + 1)
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            status = response.status
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            last_error = exc
            continue
        finally:
            connection.close()
        if len(body) > max_bytes:
            raise URLGuardError("remote response exceeds the download limit")
        return status, response_headers, body
    raise URLGuardError(f"failed to connect to remote host: {last_error}") from last_error


class StreamedURL:
    """An open, SSRF-validated HTTP response whose body can be consumed lazily."""

    def __init__(
        self,
        status: int,
        headers: dict[str, str],
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        max_bytes: int,
    ) -> None:
        self.status = status
        self.headers = headers
        self._response = response
        self._connection = connection
        self._max_bytes = max_bytes
        self._read = 0
        self._closed = False

    def iter_bytes(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        try:
            while True:
                chunk = self._response.read(chunk_size)
                if not chunk:
                    return
                self._read += len(chunk)
                if self._max_bytes and self._read > self._max_bytes:
                    raise URLGuardError("remote response exceeds the download limit")
                yield chunk
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self) -> "StreamedURL":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _open_once(
    parsed: urllib.parse.SplitResult,
    addresses: list[str],
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
    *,
    method: str,
    body: bytes | None,
) -> StreamedURL:
    host = _ascii_host(parsed.hostname or "")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    request_headers = {
        "Host": _host_header(parsed),
        "Connection": "close",
        **headers,
    }
    last_error: Exception | None = None
    for address in addresses:
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHTTPSConnection(
                host, port, address, timeout, ssl.create_default_context()
            )
        else:
            connection = _PinnedHTTPConnection(host, port, address, timeout)
        try:
            connection.request(method, _request_target(parsed), body=body, headers=request_headers)
            response = connection.getresponse()
        except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
            connection.close()
            last_error = exc
            continue
        length = response.getheader("Content-Length")
        if max_bytes and length and length.isdigit() and int(length) > max_bytes:
            response.close()
            connection.close()
            raise URLGuardError("remote response exceeds the download limit")
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        return StreamedURL(response.status, response_headers, response, connection, max_bytes)
    raise URLGuardError(f"failed to connect to remote host: {last_error}")


_CREDENTIAL_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-amz-security-token",
    "x-tos-security-token",
}


def _same_origin(left: str, right: str) -> bool:
    first = urllib.parse.urlsplit(left)
    second = urllib.parse.urlsplit(right)
    return (
        first.scheme.lower() == second.scheme.lower()
        and (first.hostname or "").lower() == (second.hostname or "").lower()
        and first.port == second.port
    )


def open_url(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30,
    max_bytes: int = 0,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> StreamedURL:
    """Open a remote response after validating and pinning every redirect hop."""
    current = url
    current_method = method.upper()
    current_body = body
    current_headers = dict(headers or {})
    for hop in range(max_redirects + 1):
        parsed = urllib.parse.urlsplit(current)
        if is_whitelisted(parsed.hostname or ""):
            validate_url_for_ssrf(current)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = _resolve_addresses(_ascii_host(parsed.hostname or ""), port)
        else:
            parsed, addresses = validate_public_url(current)
        response = _open_once(
            parsed,
            addresses,
            current_headers,
            timeout,
            max_bytes,
            method=current_method,
            body=current_body,
        )
        if response.status not in REDIRECT_STATUSES:
            return response
        location = str(response.headers.get("location") or "").strip()
        response.close()
        if not location:
            raise URLGuardError("remote redirect is missing a Location header")
        if hop >= max_redirects:
            raise URLGuardError("remote URL redirected too many times")
        next_url = urllib.parse.urljoin(current, location)
        if not _same_origin(current, next_url):
            current_headers = {
                key: value
                for key, value in current_headers.items()
                if key.lower() not in _CREDENTIAL_HEADERS
                and not key.lower().startswith(("x-amz-", "x-tos-"))
            }
        current = next_url
        if response.status in {301, 302, 303} and current_method != "HEAD":
            current_method = "GET"
            current_body = None
    raise URLGuardError("remote URL redirected too many times")


def fetch_public_url(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    max_bytes: int = 10 * 1024 * 1024,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> tuple[bytes, dict[str, str], str]:
    """Download a public http(s) resource, validating and pinning every hop."""
    current = url
    for hop in range(max_redirects + 1):
        parsed, addresses = validate_public_url(current)
        status, response_headers, body = _fetch_once(
            parsed, addresses, headers or {}, timeout, max_bytes
        )
        if status not in REDIRECT_STATUSES:
            if not 200 <= status < 300:
                preview = body[:512].decode("utf-8", errors="replace").strip()
                raise URLGuardError(f"remote server returned HTTP {status}: {preview}")
            return body, response_headers, current
        location = str(response_headers.get("location") or "").strip()
        if not location:
            raise URLGuardError("remote redirect is missing a Location header")
        if hop >= max_redirects:
            raise URLGuardError("remote URL redirected too many times")
        current = urllib.parse.urljoin(current, location)
    raise URLGuardError("remote URL redirected too many times")

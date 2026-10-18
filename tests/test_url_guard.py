from __future__ import annotations

import io
import socket

import pytest

from app.services import url_guard


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.1", "private"),
        ("100.64.0.1", "non-public"),
        ("169.254.169.254", "link-local"),
        ("fe80::1", "link-local"),
        ("::ffff:127.0.0.1", "loopback"),
        ("2002:c0a8:0101::1", "private"),
        ("64:ff9b::7f00:1", "loopback"),
    ],
)
def test_internal_addresses_are_rejected(address: str, reason: str) -> None:
    assert reason in url_guard._address_reason(address)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/archive.zip",
        "http://[::1]/archive.zip",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "https://user:pass@example.com/archive.zip",
    ],
)
def test_unsafe_urls_are_rejected(url: str) -> None:
    with pytest.raises(url_guard.URLGuardError):
        url_guard.validate_public_url(url)


def test_every_resolved_address_must_be_public(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
    ]
    monkeypatch.setattr(url_guard.socket, "getaddrinfo", lambda *args, **kwargs: records)
    with pytest.raises(url_guard.URLGuardError, match="private address"):
        url_guard.validate_public_url("https://example.test/archive.zip")


def test_fetch_validates_redirect_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        url_guard,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )
    calls: list[tuple[str, list[str]]] = []

    def fake_fetch(parsed, addresses, headers, timeout, max_bytes):
        calls.append((parsed.hostname or "", addresses))
        if parsed.hostname == "public.test":
            return 302, {"location": "https://cdn.test/skill.zip"}, b""
        return 200, {"content-type": "application/zip"}, b"skill bytes"

    monkeypatch.setattr(url_guard, "_fetch_once", fake_fetch)
    body, headers, final_url = url_guard.fetch_public_url("https://public.test/skill")
    assert body == b"skill bytes"
    assert headers["content-type"] == "application/zip"
    assert final_url == "https://cdn.test/skill.zip"
    assert calls == [
        ("public.test", ["93.184.216.34"]),
        ("cdn.test", ["93.184.216.34"]),
    ]


def test_fetch_rejects_redirect_to_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve_public = url_guard._resolve_public_addresses
    monkeypatch.setattr(
        url_guard,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"] if host == "public.test" else resolve_public(host, port),
    )
    monkeypatch.setattr(
        url_guard,
        "_fetch_once",
        lambda *args: (302, {"location": "http://127.0.0.1/internal"}, b""),
    )
    with pytest.raises(url_guard.URLGuardError, match="loopback address"):
        url_guard.fetch_public_url("https://public.test/skill")


def test_redirect_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        url_guard,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )
    monkeypatch.setattr(
        url_guard,
        "_fetch_once",
        lambda *args: (302, {"location": "https://public.test/again"}, b""),
    )
    with pytest.raises(url_guard.URLGuardError, match="too many times"):
        url_guard.fetch_public_url("https://public.test/skill", max_redirects=2)


def test_ssrf_whitelist_supports_exact_wildcard_and_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SSRF_WHITELIST",
        "SearXNG.local,*.internal.test,10.0.0.0/8,2001:db8::/32",
    )
    monkeypatch.setenv("SSRF_WHITELIST_EXTRA", "extra.test")
    assert url_guard.is_whitelisted("searxng.local")
    assert url_guard.is_whitelisted("svc.internal.test")
    assert url_guard.is_whitelisted("internal.test")
    assert url_guard.is_whitelisted("10.24.1.8")
    assert url_guard.is_whitelisted("2001:db8::42")
    assert url_guard.is_whitelisted("extra.test")
    assert not url_guard.is_whitelisted("public.test")


def test_validate_url_for_ssrf_honors_whitelist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SSRF_WHITELIST", raising=False)
    monkeypatch.delenv("SSRF_WHITELIST_EXTRA", raising=False)
    with pytest.raises(url_guard.URLGuardError, match="loopback"):
        url_guard.validate_url_for_ssrf("http://127.0.0.1:8080/search")

    monkeypatch.setenv("SSRF_WHITELIST", "127.0.0.1")
    parsed = url_guard.validate_url_for_ssrf("http://127.0.0.1:8080/search")
    assert parsed.hostname == "127.0.0.1"


def test_proxy_url_validation_requires_http_and_whitelisted_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SSRF_WHITELIST", raising=False)
    monkeypatch.delenv("SSRF_WHITELIST_EXTRA", raising=False)
    assert url_guard.validate_proxy_url("") is None
    with pytest.raises(url_guard.URLGuardError):
        url_guard.validate_proxy_url("ftp://proxy.example.test:21")
    with pytest.raises(url_guard.URLGuardError, match="loopback"):
        url_guard.validate_proxy_url("http://127.0.0.1:3128")

    monkeypatch.setenv("SSRF_WHITELIST", "127.0.0.1")
    parsed = url_guard.validate_proxy_url("http://user:pass@127.0.0.1:3128")
    assert parsed is not None
    assert parsed.hostname == "127.0.0.1"


def test_outbound_dial_timeout_is_configurable_and_caps_connect_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEKNORA_OUTBOUND_DIAL_TIMEOUT", raising=False)
    assert url_guard.outbound_dial_timeout() == 5.0
    monkeypatch.setenv("WEKNORA_OUTBOUND_DIAL_TIMEOUT", "250ms")
    assert url_guard.outbound_dial_timeout() == 0.25
    monkeypatch.setenv("WEKNORA_OUTBOUND_DIAL_TIMEOUT", "invalid")
    assert url_guard.outbound_dial_timeout() == 5.0

    calls: list[tuple] = []

    class Sock:
        def settimeout(self, value: float) -> None:
            calls.append(("read", value))

    def connect(address, timeout, source_address):
        calls.append(("connect", address, timeout, source_address))
        return Sock()

    monkeypatch.setenv("WEKNORA_OUTBOUND_DIAL_TIMEOUT", "1s")
    monkeypatch.setattr(url_guard.socket, "create_connection", connect)
    connection = url_guard._PinnedHTTPConnection(
        "example.test", 80, "93.184.216.34", 30
    )
    connection.connect()
    assert calls == [
        ("connect", ("93.184.216.34", 80), 1.0, None),
        ("read", 30),
    ]


def test_request_url_uses_validated_pinned_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        url_guard,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )
    calls: list[tuple[str, list[str], bytes | None]] = []

    def fake_fetch(parsed, addresses, headers, timeout, max_bytes, *, method="GET", body=None):
        calls.append((method, addresses, body))
        return 201, {"content-type": "application/json"}, b'{"ok": true}'

    monkeypatch.setattr(url_guard, "_fetch_once", fake_fetch)
    status, headers, body, final_url = url_guard.request_url(
        "POST",
        "https://public.test/search",
        headers={"Content-Type": "application/json"},
        body=b'{"query": "release"}',
        timeout=7,
    )
    assert status == 201
    assert headers["content-type"] == "application/json"
    assert body == b'{"ok": true}'
    assert final_url == "https://public.test/search"
    assert calls == [("POST", ["93.184.216.34"], b'{"query": "release"}')]


def test_open_url_streams_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        url_guard,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )
    closed: list[str] = []

    class Response:
        def __init__(self) -> None:
            self.buffer = io.BytesIO(b"streamed bytes")

        def read(self, size: int) -> bytes:
            return self.buffer.read(size)

        def close(self) -> None:
            closed.append("response")

    class Connection:
        def close(self) -> None:
            closed.append("connection")

    monkeypatch.setattr(
        url_guard,
        "_open_once",
        lambda *args, **kwargs: url_guard.StreamedURL(
            200, {"content-length": "14"}, Response(), Connection(), 0
        ),
    )
    response = url_guard.open_url("GET", "https://public.test/file")
    assert b"".join(response.iter_bytes(3)) == b"streamed bytes"
    assert closed == ["response", "connection"]


def test_open_url_drops_credentials_on_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        url_guard,
        "_resolve_public_addresses",
        lambda host, port: ["93.184.216.34"],
    )
    calls: list[tuple[str, dict[str, str]]] = []

    class EmptyResponse:
        def read(self, _size: int) -> bytes:
            return b""

        def close(self) -> None:
            return None

    class Connection:
        def close(self) -> None:
            return None

    def fake_open(parsed, addresses, headers, timeout, max_bytes, *, method, body):
        calls.append((parsed.hostname or "", dict(headers)))
        if parsed.hostname == "public.test":
            return url_guard.StreamedURL(
                302,
                {"location": "https://cdn.test/file"},
                EmptyResponse(),
                Connection(),
                0,
            )
        return url_guard.StreamedURL(200, {}, EmptyResponse(), Connection(), 0)

    monkeypatch.setattr(url_guard, "_open_once", fake_open)
    response = url_guard.open_url(
        "GET",
        "https://public.test/file",
        headers={
            "Authorization": "secret",
            "x-amz-date": "date",
            "Accept": "application/octet-stream",
        },
    )
    assert response.status == 200
    assert calls[0][1]["Authorization"] == "secret"
    assert "Authorization" not in calls[1][1]
    assert "x-amz-date" not in calls[1][1]
    assert calls[1][1]["Accept"] == "application/octet-stream"

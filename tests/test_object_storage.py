from __future__ import annotations

import urllib.parse

import pytest

from app.errors import AppError


@pytest.fixture
def object_storage():
    from app.services import object_storage as module

    return module


def test_object_storage_url_layouts(object_storage) -> None:
    base = {
        "endpoint": "https://objects.example.test",
        "region": "us-east-1",
        "bucket_name": "docs",
        "access_key_id": "ACCESS",
        "secret_access_key": "SECRET",
    }
    s3_url, path_style = object_storage._object_url(
        "s3", base, "docs", "7/exports/a b.png"
    )
    assert path_style is True
    assert s3_url == "https://objects.example.test/docs/7/exports/a%20b.png"

    cos_url, path_style = object_storage._object_url(
        "cos",
        {**base, "endpoint": "https://docs.cos.us-east-1.myqcloud.com"},
        "docs",
        "7/exports/a.png",
    )
    assert path_style is False
    assert cos_url == "https://docs.cos.us-east-1.myqcloud.com/7/exports/a.png"

    obs_url, path_style = object_storage._object_url(
        "obs", {**base, "endpoint": "https://10.0.0.8:8443"}, "docs", "7/a.png"
    )
    assert path_style is True
    assert obs_url == "https://10.0.0.8:8443/docs/7/a.png"


@pytest.mark.parametrize(
    ("endpoint", "force_path_style", "expected"),
    [
        (
            "https://cos.ap-shanghai.myqcloud.com",
            False,
            "https://examplebucket-1250000000.cos.ap-shanghai.myqcloud.com/a/b.txt",
        ),
        (
            "https://cos.ap-shanghai.myqcloud.com",
            True,
            "https://cos.ap-shanghai.myqcloud.com/examplebucket-1250000000/a/b.txt",
        ),
        (
            "https://cos.myqcloud.com.example.org",
            False,
            "https://cos.myqcloud.com.example.org/examplebucket-1250000000/a/b.txt",
        ),
        (
            "https://objects.example.test",
            False,
            "https://objects.example.test/examplebucket-1250000000/a/b.txt",
        ),
    ],
)
def test_s3_endpoint_addressing_matches_storage_connectivity(
    object_storage, endpoint: str, force_path_style: bool, expected: str
) -> None:
    from app.services import connectivity

    config = {
        "endpoint": endpoint,
        "region": "us-east-1",
        "bucket_name": "examplebucket-1250000000",
        "access_key_id": "ACCESS",
        "secret_access_key": "SECRET",
        "force_path_style": force_path_style,
    }
    object_url, _path_style = object_storage._object_url(
        "s3", config, config["bucket_name"], "a/b.txt"
    )
    connectivity_url, _region, _path_style = connectivity._boto_url("s3", config)
    assert object_url == expected
    assert connectivity_url == expected.removesuffix("/a/b.txt")


@pytest.mark.parametrize(
    ("provider", "header"),
    [
        ("s3", "Authorization"),
        ("minio", "Authorization"),
        ("obs", "Authorization"),
        ("cos", "Authorization"),
        ("tos", "x-tos-signature"),
        ("oss", "Authorization"),
        ("ks3", "Authorization"),
    ],
)
def test_object_storage_signs_read_requests(object_storage, provider: str, header: str) -> None:
    config = {
        "endpoint": "https://objects.example.test",
        "region": "us-east-1",
        "bucket_name": "docs",
        "access_key_id": "ACCESS",
        "secret_access_key": "SECRET",
    }
    parsed = urllib.parse.urlsplit("https://objects.example.test/docs/7/exports/a.png")
    headers = object_storage._signed_headers(
        provider, "GET", parsed, config, "docs", True
    )
    assert headers[header]
    if provider == "s3":
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ACCESS/")
    if provider == "cos":
        assert headers["Authorization"].startswith("q-sign-algorithm=sha1")
    if provider == "oss":
        assert headers["Authorization"].startswith("OSS ACCESS:")
    if provider == "ks3":
        assert headers["Authorization"].startswith("AWS ACCESS:")


def test_object_storage_rejects_cross_tenant_reference(object_storage) -> None:
    with pytest.raises(AppError) as exc:
        object_storage.resolve_config(
            "storage://backend/s3://docs/7/exports/a.png", 8
        )
    assert exc.value.status == 403


def test_object_storage_rejects_unknown_scheme(object_storage) -> None:
    with pytest.raises(AppError) as exc:
        object_storage.resolve_config("ftp://docs/7/a.png", 7)
    assert exc.value.status == 400


def test_object_storage_put_uses_payload_hash_and_scoped_path(
    object_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "endpoint": "https://objects.example.test",
        "region": "us-east-1",
        "bucket_name": "docs",
        "access_key_id": "ACCESS",
        "secret_access_key": "SECRET",
    }
    monkeypatch.setattr(
        object_storage,
        "resolve_write_target",
        lambda tenant_id, backend_id, provider: (
            "backend-1", "s3", config, "docs", "us-east-1"
        ),
    )
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    class Response:
        status = 200

        def close(self) -> None:
            return None

    def fake_open(method, url, **kwargs):
        calls.append((method, url, dict(kwargs.get("headers") or {}), kwargs.get("body")))
        return Response()

    monkeypatch.setattr(object_storage.url_guard, "open_url", fake_open)
    reference = object_storage.save_bytes(
        b"payload",
        7,
        "Report.PDF",
        backend_id="backend-1",
        provider="s3",
        knowledge_id="knowledge-1",
        content_type="application/pdf",
    )
    assert reference.startswith(
        "storage://backend-1/s3://docs/7/knowledge-1/"
    )
    assert reference.endswith(".pdf")
    method, url, headers, body = calls[0]
    assert method == "PUT"
    assert url.startswith("https://objects.example.test/docs/7/knowledge-1/")
    assert body == b"payload"
    assert headers["Content-Type"] == "application/pdf"
    assert headers["x-amz-content-sha256"] == object_storage._sha256(b"payload")
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=ACCESS/")


def test_object_storage_delete_is_idempotent(
    object_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "endpoint": "https://objects.example.test",
        "region": "us-east-1",
        "bucket_name": "docs",
        "access_key_id": "ACCESS",
        "secret_access_key": "SECRET",
    }
    monkeypatch.setattr(
        object_storage,
        "resolve_config",
        lambda reference, tenant_id: (
            "s3", config, "docs", "7/knowledge-1/a.pdf", ""
        ),
    )
    calls: list[tuple[str, str]] = []

    class Response:
        status = 404

        def close(self) -> None:
            return None

    def fake_open(method, url, **kwargs):
        calls.append((method, url))
        return Response()

    monkeypatch.setattr(object_storage.url_guard, "open_url", fake_open)
    object_storage.delete_object(
        "storage://backend-1/s3://docs/7/knowledge-1/a.pdf", 7
    )
    assert calls == [("DELETE", "https://objects.example.test/docs/7/knowledge-1/a.pdf")]


@pytest.mark.parametrize(
    ("provider", "endpoint", "copy_header", "copy_source"),
    [
        ("s3", "https://objects.example.test", "x-amz-copy-source", "/docs/7/knowledge-1/source%20a.pdf"),
        ("minio", "http://objects.example.test", "x-amz-copy-source", "/docs/7/knowledge-1/source%20a.pdf"),
        ("obs", "https://objects.example.test", "x-amz-copy-source", "/docs/7/knowledge-1/source%20a.pdf"),
        ("tos", "https://objects.example.test", "x-tos-copy-source", "/docs/7/knowledge-1/source%20a.pdf"),
        (
            "cos",
            "https://docs.cos.us-east-1.myqcloud.com",
            "x-cos-copy-source",
            "docs.cos.us-east-1.myqcloud.com/7/knowledge-1/source%20a.pdf",
        ),
        ("oss", "https://objects.example.test", "x-oss-copy-source", "/docs/7/knowledge-1/source%20a.pdf"),
        ("ks3", "https://objects.example.test", "x-ks3-copy-source", "/docs/7/knowledge-1/source%20a.pdf"),
    ],
)
def test_object_storage_copy_uses_server_side_request(
    object_storage,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    endpoint: str,
    copy_header: str,
    copy_source: str,
) -> None:
    config = {
        "endpoint": endpoint,
        "region": "us-east-1",
        "bucket_name": "docs",
        "access_key_id": "ACCESS",
        "secret_access_key": "SECRET",
    }
    monkeypatch.setattr(
        object_storage,
        "resolve_config",
        lambda reference, tenant_id: (provider, config, "docs", "7/knowledge-1/source a.pdf", "us-east-1"),
    )
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    class Response:
        status = 200

        def close(self) -> None:
            return None

    def fake_open(method, url, **kwargs):
        calls.append((method, url, dict(kwargs.get("headers") or {}), kwargs.get("body")))
        return Response()

    monkeypatch.setattr(object_storage.url_guard, "open_url", fake_open)
    reference = object_storage.copy_object(
        f"{provider}://docs/7/knowledge-1/source a.pdf",
        7,
        "knowledge-2",
        filename="Clone.PDF",
    )
    if provider == "cos":
        assert reference.startswith("cos://docs/us-east-1/7/knowledge-2/")
    else:
        assert reference.startswith(f"{provider}://docs/7/knowledge-2/")
    assert reference.endswith(".pdf")
    assert len(calls) == 1
    method, url, headers, body = calls[0]
    assert method == "PUT"
    if provider in {"s3", "minio"}:
        assert url.startswith(endpoint + "/docs/7/knowledge-2/")
    elif provider == "cos":
        assert url.startswith(endpoint + "/7/knowledge-2/")
    else:
        assert url.startswith("https://docs.objects.example.test/7/knowledge-2/")
    assert body is None
    assert headers[copy_header] == copy_source
    if provider in {"s3", "minio", "obs"}:
        assert "x-amz-copy-source" in headers["Authorization"]
    if provider == "tos":
        assert "x-tos-copy-source" in headers["x-tos-signedheaders"]

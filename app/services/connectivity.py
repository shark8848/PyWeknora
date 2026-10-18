"""Connectivity checks for storage and vector-store integrations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config


class ConnectivityError(RuntimeError):
    pass


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 8,
    insecure: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConnectivityError("endpoint must be an http or https URL")
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    context = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise ConnectivityError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ConnectivityError(f"connection failed: {exc.reason}") from exc


def _local_storage(config_data: dict[str, Any]) -> dict[str, Any]:
    raw_path = str(config_data.get("path") or config.FILES_DIR / "_healthcheck")
    root = Path(raw_path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    probe = root / f".pyweknora-{uuid.uuid4().hex}.tmp"
    value = b"pyweknora-storage-check"
    probe.write_bytes(value)
    try:
        if probe.read_bytes() != value:
            raise ConnectivityError("storage read-back did not match")
    finally:
        probe.unlink(missing_ok=True)
    return {"ok": True, "provider": "local", "path": str(root)}


def _boto_url(provider: str, config_data: dict[str, Any]) -> tuple[str, str, bool]:
    bucket = str(config_data.get("bucket_name") or config_data.get("bucket") or "").strip()
    if not bucket:
        raise ConnectivityError("bucket name is required")
    endpoint = str(config_data.get("endpoint") or "").strip().rstrip("/")
    region = str(config_data.get("region") or "us-east-1").strip()
    path_style = bool(config_data.get("force_path_style"))
    use_ssl = config_data.get("use_ssl")
    if not endpoint:
        endpoint = f"https://s3.{region}.amazonaws.com" if provider == "s3" else ""
    if not endpoint:
        raise ConnectivityError("endpoint is required")
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        base = endpoint
    else:
        scheme = "https" if use_ssl is not False else "http"
        base = f"{scheme}://{endpoint}"
    parsed = urllib.parse.urlparse(base)
    host = parsed.netloc
    path = parsed.path.rstrip("/")
    hostname = (parsed.hostname or "").lower()
    if provider == "s3" and not path_style:
        path_style = not (
            hostname.endswith("amazonaws.com")
            or hostname.endswith(".myqcloud.com")
        )
    if path_style or provider in {"minio", "ks3"}:
        target = f"{base}/{bucket}"
    else:
        if not hostname.startswith(bucket.lower() + "."):
            host = f"{bucket}.{host}"
        target = f"{parsed.scheme}://{host}{path or ''}"
    return target, region, path_style


def _sign_s3(
    method: str,
    url: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str = "",
) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_uri = parsed.path or "/"
    canonical_querystring = parsed.query
    headers_to_sign = {
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers_to_sign["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(headers_to_sign))
    canonical_headers = "".join(f"{key}:{headers_to_sign[key]}\n" for key in sorted(headers_to_sign))
    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_querystring,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    key = ("AWS4" + secret_key).encode()
    for part in (date_stamp, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        **headers_to_sign,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


def _oss_sign(method: str, url: str, access_key: str, secret_key: str, bucket: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    resource = f"/{bucket}{parsed.path.split('/' + bucket, 1)[-1] if bucket in parsed.path else parsed.path}"
    if parsed.query:
        resource += "?" + parsed.query
    string_to_sign = f"{method}\n\n\n{date}\n{resource}"
    signature = base64.b64encode(hmac.new(secret_key.encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()
    return {"Date": date, "Authorization": f"OSS {access_key}:{signature}"}


def test_storage(provider: str, config_data: dict[str, Any]) -> dict[str, Any]:
    provider = (provider or "local").lower()
    if provider == "local":
        return _local_storage(config_data)
    url, region, _ = _boto_url(provider, config_data)
    access_key = str(config_data.get("access_key_id") or config_data.get("access_key") or "")
    secret_key = str(config_data.get("secret_access_key") or config_data.get("secret_key") or "")
    headers: dict[str, str] = {}
    if access_key and secret_key:
        if provider == "oss":
            bucket = str(config_data.get("bucket_name") or "")
            headers = _oss_sign("HEAD", url, access_key, secret_key, bucket)
        else:
            headers = _sign_s3("HEAD", url, region, access_key, secret_key, str(config_data.get("session_token") or ""))
    status, response_headers, _ = _http("HEAD", url, headers=headers)
    version = (
        response_headers.get("x-amz-bucket-region")
        or response_headers.get("Server")
        or response_headers.get("server")
        or str(status)
    )
    return {"ok": True, "provider": provider, "version": version}


def test_vector_store(engine_type: str, connection: dict[str, Any]) -> dict[str, Any]:
    engine = (engine_type or "sqlite").lower()
    if engine in {"sqlite", "postgres", "postgresql"}:
        return {"ok": True, "version": "sqlite-local", "engine_type": engine}
    config_data = connection if isinstance(connection, dict) else {}
    headers: dict[str, str] = {}
    api_key = str(config_data.get("api_key") or "")
    username = str(config_data.get("username") or "")
    password = str(config_data.get("password") or "")
    if username or password:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers["Authorization"] = "Basic " + token
    if engine == "qdrant":
        scheme = "https" if config_data.get("use_tls") else "http"
        host = str(config_data.get("host") or "localhost")
        port = int(config_data.get("port") or (6333 if scheme == "http" else 6333))
        url = f"{scheme}://{host}:{port}/collections"
        if api_key:
            headers["api-key"] = api_key
    elif engine in {"elasticsearch", "opensearch"}:
        url = str(config_data.get("addr") or config_data.get("endpoint") or "").rstrip("/")
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
    elif engine == "weaviate":
        scheme = str(config_data.get("scheme") or "http")
        host = str(config_data.get("host") or "localhost:8080")
        url = f"{scheme}://{host}/v1/meta"
    elif engine == "milvus":
        addr = str(config_data.get("addr") or config_data.get("endpoint") or "")
        if not addr.startswith("http"):
            addr = "http://" + addr
        url = addr.rstrip("/") + "/healthz"
    else:
        url = str(config_data.get("addr") or config_data.get("endpoint") or config_data.get("url") or "").rstrip("/")
    if not url:
        raise ConnectivityError("endpoint is required")
    status, response_headers, body = _http(
        "GET",
        url,
        headers=headers,
        insecure=bool(config_data.get("insecure_skip_verify")),
    )
    version = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict):
            raw_version = payload.get("version")
            if isinstance(raw_version, dict):
                raw_version = raw_version.get("number")
            version = str(raw_version or "")
    except (ValueError, UnicodeDecodeError):
        pass
    return {"ok": True, "version": version or str(status), "engine_type": engine}

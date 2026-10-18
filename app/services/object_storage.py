"""Tenant-scoped object storage access for protected files and uploads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import mimetypes
import os
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import db
from ..errors import forbidden, not_found, validation
from ..utils import loads
from . import url_guard


OBJECT_SCHEMES = {"minio", "cos", "tos", "s3", "oss", "ks3", "obs"}
_ACTIVE_STATUSES = {"active", "ready", ""}


class ObjectStorageError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def is_object_reference(reference: str) -> bool:
    _backend_id, inner = _unwrap_backend(str(reference or "").strip())
    scheme, separator, _rest = inner.partition("://")
    return bool(separator and scheme.lower() in OBJECT_SCHEMES)


def _unwrap_backend(reference: str) -> tuple[str, str]:
    value = reference.strip()
    if not value.startswith("storage://"):
        return "", value
    rest = value[len("storage://"):]
    backend_id, separator, inner = rest.partition("/")
    if not separator or not backend_id or not inner:
        raise validation("invalid storage backend reference")
    return backend_id, inner


def _split_reference(reference: str) -> tuple[str, str, str, str, str]:
    backend_id, inner = _unwrap_backend(reference)
    scheme, separator, rest = inner.partition("://")
    provider = scheme.lower()
    if not separator or provider not in OBJECT_SCHEMES or not rest:
        raise validation("unsupported object storage reference")
    if ".." in rest:
        raise validation("invalid file path")
    if provider == "cos":
        parts = rest.split("/", 2)
        if len(parts) != 3 or not all(parts):
            raise validation("invalid COS file path")
        bucket, region, key = parts
    else:
        parts = rest.split("/", 1)
        if len(parts) != 2 or not all(parts):
            raise validation(f"invalid {provider.upper()} file path")
        bucket, key = parts
        region = ""
    return backend_id, provider, bucket, region, key


def _tenant_segment(reference: str) -> int:
    _backend_id, inner = _unwrap_backend(reference)
    _scheme, separator, rest = inner.partition("://")
    if not separator:
        return 0
    for segment in rest.split("/"):
        if segment.isdigit():
            return int(segment)
    return 0


def _backend_row(tenant_id: int, backend_id: str, provider: str) -> dict[str, Any] | None:
    if backend_id:
        row = db.fetch_one(
            "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (backend_id, tenant_id),
        )
        if not row:
            raise not_found("Storage backend not found")
        if str(row.get("provider") or "").lower() != provider:
            raise validation("storage backend provider mismatch")
        if str(row.get("status") or "").lower() not in _ACTIVE_STATUSES:
            raise validation("storage backend is not active")
        return row
    row = db.fetch_one(
        "SELECT * FROM storage_backends WHERE tenant_id=? AND provider=? "
        "AND legacy_alias=1 AND deleted_at IS NULL",
        (tenant_id, provider),
    )
    if row:
        if str(row.get("status") or "").lower() not in _ACTIVE_STATUSES:
            raise validation("storage backend is not active")
        return row
    tenant = db.fetch_one(
        "SELECT default_storage_backend_id FROM tenants WHERE id=? AND deleted_at IS NULL",
        (tenant_id,),
    )
    default_id = str((tenant or {}).get("default_storage_backend_id") or "").strip()
    if not default_id:
        return None
    row = db.fetch_one(
        "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (default_id, tenant_id),
    )
    if not row:
        raise not_found("Storage backend not found")
    if str(row.get("provider") or "").lower() != provider:
        raise validation("storage backend provider mismatch")
    if str(row.get("status") or "").lower() not in _ACTIVE_STATUSES:
        raise validation("storage backend is not active")
    return row


def _env_value(*keys: str) -> str:
    for key in keys:
        value = str(os.getenv(key) or "").strip()
        if value:
            return value
    return ""


def _provider_env(provider: str) -> dict[str, Any]:
    if provider == "minio":
        return {
            "endpoint": _env_value("MINIO_ENDPOINT"),
            "access_key_id": _env_value("MINIO_ACCESS_KEY_ID"),
            "secret_access_key": _env_value("MINIO_SECRET_ACCESS_KEY"),
            "bucket_name": _env_value("MINIO_BUCKET_NAME"),
        }
    if provider == "cos":
        return {
            "endpoint": _env_value("COS_ENDPOINT"),
            "region": _env_value("COS_REGION"),
            "access_key_id": _env_value("COS_SECRET_ID"),
            "secret_access_key": _env_value("COS_SECRET_KEY"),
            "bucket_name": _env_value("COS_BUCKET_NAME"),
            "app_id": _env_value("COS_APP_ID"),
        }
    prefix = provider.upper()
    return {
        "endpoint": _env_value(f"{prefix}_ENDPOINT"),
        "region": _env_value(f"{prefix}_REGION"),
        "access_key_id": _env_value(f"{prefix}_ACCESS_KEY", f"{prefix}_ACCESS_KEY_ID"),
        "secret_access_key": _env_value(f"{prefix}_SECRET_KEY", f"{prefix}_SECRET_ACCESS_KEY"),
        "bucket_name": _env_value(f"{prefix}_BUCKET_NAME"),
    }


def _legacy_config(tenant_id: int, provider: str) -> dict[str, Any]:
    tenant = db.fetch_one(
        "SELECT storage_engine_config FROM tenants WHERE id=? AND deleted_at IS NULL",
        (tenant_id,),
    )
    engine = loads((tenant or {}).get("storage_engine_config"), {})
    config: dict[str, Any] = {}
    if isinstance(engine, dict):
        nested = engine.get(provider)
        if isinstance(nested, dict):
            config = dict(nested)
        elif str(engine.get("default_provider") or "").strip().lower() == provider:
            config = dict(engine)
    for key, value in _provider_env(provider).items():
        if value and not config.get(key):
            config[key] = value
    return config


def _normalize_config(provider: str, config_data: Any) -> dict[str, Any]:
    config = dict(config_data) if isinstance(config_data, dict) else {}
    for key, value in _provider_env(provider).items():
        if value and not config.get(key):
            config[key] = value
    config["bucket_name"] = str(
        config.get("bucket_name") or config.get("bucket") or ""
    ).strip()
    config["access_key_id"] = str(
        config.get("access_key_id")
        or config.get("access_key")
        or config.get("secret_id")
        or ""
    ).strip()
    config["secret_access_key"] = str(
        config.get("secret_access_key") or config.get("secret_key") or ""
    ).strip()
    config["endpoint"] = str(config.get("endpoint") or "").strip()
    config["region"] = str(config.get("region") or "").strip()
    if provider == "s3":
        if not config["access_key_id"]:
            config["access_key_id"] = _env_value("AWS_ACCESS_KEY_ID")
        if not config["secret_access_key"]:
            config["secret_access_key"] = _env_value("AWS_SECRET_ACCESS_KEY")
        if not config.get("session_token"):
            config["session_token"] = _env_value("AWS_SESSION_TOKEN")
        if not config["region"]:
            config["region"] = _env_value("AWS_REGION", "AWS_DEFAULT_REGION") or "us-east-1"
    if provider == "cos" and not config["endpoint"]:
        region = config["region"]
        bucket = config["bucket_name"]
        if bucket and region:
            app_id = str(config.get("app_id") or "").strip()
            host_bucket = bucket if (not app_id or bucket.endswith("-" + app_id)) else f"{bucket}-{app_id}"
            config["endpoint"] = f"https://{host_bucket}.cos.{region}.myqcloud.com"
    return config


def resolve_config(
    reference: str, expected_tenant_id: int
) -> tuple[str, dict[str, Any], str, str, str]:
    backend_id, provider, bucket, region, key = _split_reference(reference)
    path_tenant = _tenant_segment(reference)
    if path_tenant == 0:
        raise forbidden("file path not accessible")
    if path_tenant != expected_tenant_id:
        raise forbidden("file path not accessible")
    row = _backend_row(expected_tenant_id, backend_id, provider)
    if row is not None:
        config = _normalize_config(provider, loads(row.get("config"), {}))
    else:
        config = _normalize_config(provider, _legacy_config(expected_tenant_id, provider))
    if not config.get("bucket_name"):
        raise validation("storage backend bucket_name is required")
    if provider == "cos" and region and config.get("region") and region != config["region"]:
        raise validation("storage backend region mismatch")
    allowed_buckets = {
        str(config.get("bucket_name") or ""),
        str(config.get("temp_bucket_name") or ""),
    }
    if bucket not in allowed_buckets:
        raise validation("storage backend bucket mismatch")
    return provider, config, bucket, key, region


def resolve_write_target(
    tenant_id: int, backend_id: str = "", provider: str = ""
) -> tuple[str, str, dict[str, Any], str, str]:
    """Resolve the concrete storage instance used for a new object.

    An explicit KB binding wins. Otherwise a provider alias is honoured before
    the workspace default, matching the Go storage resolver.
    """
    backend_id = str(backend_id or "").strip()
    provider = str(provider or "").strip().lower()
    if provider == "__pending_env__":
        provider = ""
    if backend_id:
        row = db.fetch_one(
            "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (backend_id, tenant_id),
        )
        if not row:
            raise not_found("Storage backend not found")
        if str(row.get("status") or "").lower() not in _ACTIVE_STATUSES:
            raise validation("storage backend is not active")
        provider = str(row.get("provider") or "").lower()
        config = _normalize_config(provider, loads(row.get("config"), {}))
    elif provider in OBJECT_SCHEMES:
        row = db.fetch_one(
            "SELECT * FROM storage_backends WHERE tenant_id=? AND provider=? "
            "AND legacy_alias=1 AND deleted_at IS NULL",
            (tenant_id, provider),
        )
        if not row:
            tenant = db.fetch_one(
                "SELECT default_storage_backend_id FROM tenants WHERE id=? AND deleted_at IS NULL",
                (tenant_id,),
            )
            default_id = str((tenant or {}).get("default_storage_backend_id") or "").strip()
            if default_id:
                row = db.fetch_one(
                    "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                    (default_id, tenant_id),
                )
                if not row:
                    raise not_found("Storage backend not found")
                backend_id = str(row.get("id") or "")
                provider = str(row.get("provider") or "").lower()
        if row:
            if str(row.get("status") or "").lower() not in _ACTIVE_STATUSES:
                raise validation("storage backend is not active")
            backend_id = backend_id or str(row.get("id") or "")
        config = _normalize_config(
            provider,
            loads(row.get("config"), {}) if row else _legacy_config(tenant_id, provider),
        )
    else:
        tenant = db.fetch_one(
            "SELECT default_storage_backend_id, storage_engine_config FROM tenants "
            "WHERE id=? AND deleted_at IS NULL",
            (tenant_id,),
        )
        backend_id = str((tenant or {}).get("default_storage_backend_id") or "").strip()
        if backend_id:
            row = db.fetch_one(
                "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (backend_id, tenant_id),
            )
            if not row:
                raise not_found("Storage backend not found")
            if str(row.get("status") or "").lower() not in _ACTIVE_STATUSES:
                raise validation("storage backend is not active")
            provider = str(row.get("provider") or "").lower()
            config = _normalize_config(provider, loads(row.get("config"), {}))
        else:
            engine = loads((tenant or {}).get("storage_engine_config"), {})
            if not provider and isinstance(engine, dict):
                provider = str(engine.get("default_provider") or "").strip().lower()
            if provider in OBJECT_SCHEMES:
                config = _normalize_config(provider, _legacy_config(tenant_id, provider))
            else:
                return "", provider or "local", {}, "", ""
    if provider in OBJECT_SCHEMES:
        if not config.get("bucket_name"):
            raise validation("storage backend bucket_name is required")
        return (
            backend_id,
            provider,
            config,
            str(config.get("bucket_name") or ""),
            str(config.get("region") or ""),
        )
    return backend_id, provider or "local", config, "", ""


def _reference_for(
    backend_id: str, provider: str, bucket: str, region: str, key: str
) -> str:
    if provider == "cos":
        inner = f"cos://{bucket}/{region}/{key}"
    else:
        inner = f"{provider}://{bucket}/{key}"
    return f"storage://{backend_id}/{inner}" if backend_id else inner


def save_bytes(
    data: bytes,
    tenant_id: int,
    filename: str,
    *,
    backend_id: str = "",
    provider: str = "",
    knowledge_id: str = "",
    temp: bool = False,
    content_type: str = "",
) -> str:
    """Upload bytes to the selected object store and return its reference."""
    backend_id, provider, config, bucket, region = resolve_write_target(
        tenant_id, backend_id, provider
    )
    if provider not in OBJECT_SCHEMES:
        raise validation("storage backend is not an object store")
    name = Path(str(filename or "upload.bin").replace("\\", "/")).name or "upload.bin"
    extension = Path(name).suffix.lower()
    if temp:
        key = f"{tenant_id}/exports/{uuid.uuid4().hex}{extension}"
    elif knowledge_id:
        key = f"{tenant_id}/{knowledge_id}/{uuid.uuid4().hex}{extension}"
    else:
        key = f"{tenant_id}/exports/{uuid.uuid4().hex}{extension}"
    media_type = content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    url, path_style = _object_url(provider, config, bucket, key)
    parsed = urllib.parse.urlsplit(url)
    headers = _signed_headers(
        provider,
        "PUT",
        parsed,
        config,
        bucket,
        path_style,
        payload_hash=_sha256(data),
        content_type=media_type,
    )
    headers["Content-Type"] = media_type
    try:
        response = url_guard.open_url("PUT", url, headers=headers, body=data, timeout=120)
    except url_guard.URLGuardError as exc:
        raise ObjectStorageError(str(exc)) from exc
    status = response.status
    response.close()
    if not 200 <= status < 300:
        raise ObjectStorageError(f"object storage returned HTTP {status}", status=status)
    return _reference_for(backend_id, provider, bucket, region, key)


def read_object(reference: str, expected_tenant_id: int) -> bytes:
    opened = open_object(reference, expected_tenant_id)
    try:
        return b"".join(opened.iter_bytes())
    finally:
        opened.close()


def copy_object(
    reference: str,
    expected_tenant_id: int,
    knowledge_id: str,
    *,
    filename: str = "",
) -> str:
    """Create an independent object for a cloned knowledge document."""
    backend_id, provider, _ref_bucket, _ref_region, key = _split_reference(reference)
    provider, config, bucket, key, region = resolve_config(reference, expected_tenant_id)
    extension = Path(filename or urllib.parse.unquote(key)).suffix.lower()
    destination_key = f"{expected_tenant_id}/{knowledge_id}/{uuid.uuid4().hex}{extension}"
    destination_url, path_style = _object_url(provider, config, bucket, destination_key)
    parsed = urllib.parse.urlsplit(destination_url)
    copy_header, copy_source = _copy_source(provider, config, bucket, key)
    headers = _signed_headers(
        provider,
        "PUT",
        parsed,
        config,
        bucket,
        path_style,
        payload_hash=_sha256(b""),
        extra_headers={copy_header: copy_source},
    )
    headers[copy_header] = copy_source
    try:
        response = url_guard.open_url("PUT", destination_url, headers=headers, timeout=120)
    except url_guard.URLGuardError as exc:
        raise ObjectStorageError(str(exc)) from exc
    status = response.status
    response.close()
    if not 200 <= status < 300:
        raise ObjectStorageError(f"object storage returned HTTP {status}", status=status)
    return _reference_for(backend_id, provider, bucket, region, destination_key)


def _copy_source(
    provider: str, config: dict[str, Any], bucket: str, key: str
) -> tuple[str, str]:
    encoded_key = urllib.parse.quote(key, safe="/-_.~")
    if provider == "cos":
        host = str(config.get("endpoint") or "").strip()
        parsed = urllib.parse.urlsplit(host if "://" in host else f"https://{host}")
        if parsed.hostname:
            source = f"{parsed.hostname}/{encoded_key}"
        else:
            region = str(config.get("region") or "").strip()
            source = f"{bucket}.cos.{region}.myqcloud.com/{encoded_key}"
        return "x-cos-copy-source", source
    header = {
        "tos": "x-tos-copy-source",
        "oss": "x-oss-copy-source",
        "ks3": "x-ks3-copy-source",
    }.get(provider, "x-amz-copy-source")
    return header, f"/{bucket}/{encoded_key}"


def delete_object(reference: str, expected_tenant_id: int) -> None:
    provider, config, bucket, key, _region = resolve_config(reference, expected_tenant_id)
    url, path_style = _object_url(provider, config, bucket, key)
    parsed = urllib.parse.urlsplit(url)
    headers = _signed_headers(provider, "DELETE", parsed, config, bucket, path_style)
    try:
        response = url_guard.open_url("DELETE", url, headers=headers, timeout=60)
    except url_guard.URLGuardError as exc:
        raise ObjectStorageError(str(exc)) from exc
    status = response.status
    response.close()
    if status == 404:
        return
    if not 200 <= status < 300:
        raise ObjectStorageError(f"object storage returned HTTP {status}", status=status)


def _endpoint_url(provider: str, config: dict[str, Any]) -> tuple[urllib.parse.SplitResult, bool]:
    endpoint = str(config.get("endpoint") or "").strip()
    use_ssl = config.get("use_ssl")
    if not endpoint:
        if provider != "s3":
            raise validation(f"{provider} endpoint is required")
        region = str(config.get("region") or "us-east-1")
        endpoint = f"https://s3.{region}.amazonaws.com"
    if "://" not in endpoint:
        if provider == "minio":
            scheme = "https" if use_ssl is True else "http"
        else:
            scheme = "http" if use_ssl is False else "https"
        endpoint = f"{scheme}://{endpoint}"
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise validation("storage endpoint must be an http or https URL")
    force_path = bool(config.get("force_path_style"))
    host = (parsed.hostname or "").lower()
    if provider == "minio":
        path_style = True
    elif provider == "s3" and not force_path:
        path_style = not (host.endswith("amazonaws.com") or host.endswith(".myqcloud.com"))
    elif provider == "obs":
        try:
            path_style = ipaddress.ip_address(host) is not None
        except ValueError:
            path_style = False
    else:
        path_style = force_path
    return parsed, path_style


def _object_url(
    provider: str, config: dict[str, Any], bucket: str, key: str
) -> tuple[str, bool]:
    endpoint, path_style = _endpoint_url(provider, config)
    encoded_key = urllib.parse.quote(key, safe="/-_.~")
    base_path = endpoint.path.rstrip("/")
    port = f":{endpoint.port}" if endpoint.port else ""
    if path_style:
        path = f"{base_path}/{urllib.parse.quote(bucket, safe='-_.~')}/{encoded_key}"
        return endpoint._replace(path=path).geturl(), True
    host = endpoint.hostname or ""
    if not host.lower().startswith(bucket.lower() + "."):
        host = f"{bucket}.{host}"
    path = f"{base_path}/{encoded_key}"
    return endpoint._replace(netloc=f"{host}{port}", path=path).geturl(), False


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _canonical_query(parsed: urllib.parse.SplitResult) -> str:
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    return "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(value, safe='-_.~')}"
        for key, value in sorted(pairs)
    )


def _sign_aws(
    method: str,
    parsed: urllib.parse.SplitResult,
    config: dict[str, Any],
    *,
    service: str = "s3",
    token: str = "",
    payload_hash: str = "",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    access = str(config.get("access_key_id") or "")
    secret = str(config.get("secret_access_key") or "")
    region = str(config.get("region") or "us-east-1")
    if not access or not secret:
        if access or secret:
            raise validation("storage access key and secret key must be provided together")
        return {}
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = payload_hash or _sha256(b"")
    headers = {
        "host": parsed.netloc,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    for key, value in (extra_headers or {}).items():
        if key.lower().startswith("x-amz-"):
            headers[key.lower()] = value
    if token:
        headers["x-amz-security-token"] = token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    canonical_request = "\n".join([
        method.upper(),
        parsed.path or "/",
        _canonical_query(parsed),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        scope,
        _sha256(canonical_request.encode("utf-8")),
    ])
    key = ("AWS4" + secret).encode("utf-8")
    for part in (date_stamp, region, service, "aws4_request"):
        key = _hmac_sha256(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    result = {
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if token:
        result["x-amz-security-token"] = token
    return result


def _sign_tos(
    method: str,
    parsed: urllib.parse.SplitResult,
    config: dict[str, Any],
    payload_hash: str = "",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    access = str(config.get("access_key_id") or "")
    secret = str(config.get("secret_access_key") or "")
    if not access or not secret:
        if access or secret:
            raise validation("storage access key and secret key must be provided together")
        return {}
    region = str(config.get("region") or "")
    if not region:
        raise validation("storage region is required")
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = payload_hash or _sha256(b"")
    headers = {
        "host": parsed.netloc,
        "x-tos-content-sha256": payload_hash,
        "x-tos-date": amz_date,
    }
    for key, value in (extra_headers or {}).items():
        if key.lower().startswith("x-tos-"):
            headers[key.lower()] = value
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
    canonical_request = "\n".join([
        method.upper(),
        parsed.path or "/",
        _canonical_query(parsed),
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/{region}/tos/request"
    string_to_sign = "\n".join([
        "TOS4-HMAC-SHA256",
        amz_date,
        scope,
        _sha256(canonical_request.encode("utf-8")),
    ])
    key = secret.encode("utf-8")
    for part in (date_stamp, region, "tos", "request"):
        key = _hmac_sha256(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "x-tos-content-sha256": payload_hash,
        "x-tos-date": amz_date,
        "x-tos-credential": f"{access}/{scope}",
        "x-tos-signedheaders": signed_headers,
        "x-tos-signature": signature,
    }


def _sign_cos(
    method: str, parsed: urllib.parse.SplitResult, config: dict[str, Any]
) -> dict[str, str]:
    access = str(config.get("access_key_id") or "")
    secret = str(config.get("secret_access_key") or "")
    if not access or not secret:
        if access or secret:
            raise validation("storage access key and secret key must be provided together")
        return {}
    now = int(time.time())
    key_time = f"{now};{now + 3600}"
    sign_key = hmac.new(
        secret.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1
    ).digest()
    http_string = "\n".join([
        method.lower(),
        parsed.path or "/",
        _canonical_query(parsed),
        f"host={parsed.netloc}\n",
    ])
    string_to_sign = "\n".join([
        "sha1",
        key_time,
        hashlib.sha1(http_string.encode("utf-8")).hexdigest(),
        "",
    ])
    signature = hmac.new(
        sign_key, string_to_sign.encode("utf-8"), hashlib.sha1
    ).hexdigest()
    return {
        "Authorization": (
            "q-sign-algorithm=sha1"
            f"&q-ak={urllib.parse.quote(access, safe='')}"
            f"&q-sign-time={key_time}"
            f"&q-key-time={key_time}"
            "&q-header-list=host"
            "&q-url-param-list="
            f"&q-signature={signature}"
        )
    }


def _canonical_resource(parsed: urllib.parse.SplitResult, bucket: str, path_style: bool) -> str:
    if path_style:
        return parsed.path or "/"
    return f"/{bucket}{parsed.path or '/'}"


def _sign_oss(
    method: str,
    parsed: urllib.parse.SplitResult,
    config: dict[str, Any],
    bucket: str,
    path_style: bool,
    content_type: str = "",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    access = str(config.get("access_key_id") or "")
    secret = str(config.get("secret_access_key") or "")
    if not access or not secret:
        if access or secret:
            raise validation("storage access key and secret key must be provided together")
        return {}
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    resource = _canonical_resource(parsed, bucket, path_style)
    canonical_headers = "".join(
        f"{key}:{value}\n"
        for key, value in sorted(
            (key.lower(), value)
            for key, value in (extra_headers or {}).items()
            if key.lower().startswith("x-oss-")
        )
    )
    string_to_sign = f"{method.upper()}\n\n{content_type}\n{date}\n{canonical_headers}{resource}"
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    return {"Date": date, "Authorization": f"OSS {access}:{signature}"}


def _sign_ks3(
    method: str,
    parsed: urllib.parse.SplitResult,
    config: dict[str, Any],
    bucket: str,
    path_style: bool,
    content_type: str = "",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    access = str(config.get("access_key_id") or "")
    secret = str(config.get("secret_access_key") or "")
    if not access or not secret:
        if access or secret:
            raise validation("storage access key and secret key must be provided together")
        return {}
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    resource = _canonical_resource(parsed, bucket, path_style)
    canonical_headers = "".join(
        f"{key}:{value}\n"
        for key, value in sorted(
            (key.lower(), value)
            for key, value in (extra_headers or {}).items()
            if key.lower().startswith("x-ks3-")
        )
    )
    string_to_sign = f"{method.upper()}\n\n{content_type}\n{date}\n{canonical_headers}{resource}"
    signature = base64.b64encode(
        hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    return {"Date": date, "Authorization": f"AWS {access}:{signature}"}


def _signed_headers(
    provider: str,
    method: str,
    parsed: urllib.parse.SplitResult,
    config: dict[str, Any],
    bucket: str,
    path_style: bool,
    *,
    payload_hash: str = "",
    content_type: str = "",
    extra_headers: dict[str, str] | None = None,
) -> dict[str, str]:
    if provider in {"minio", "s3", "obs"}:
        return _sign_aws(
            method,
            parsed,
            config,
            service="s3",
            token=str(config.get("session_token") or ""),
            payload_hash=payload_hash,
            extra_headers=extra_headers,
        )
    if provider == "cos":
        return _sign_cos(method, parsed, config)
    if provider == "tos":
        return _sign_tos(method, parsed, config, payload_hash, extra_headers)
    if provider == "oss":
        return _sign_oss(method, parsed, config, bucket, path_style, content_type, extra_headers)
    if provider == "ks3":
        return _sign_ks3(method, parsed, config, bucket, path_style, content_type, extra_headers)
    raise validation("unsupported object storage provider")


def open_object(reference: str, expected_tenant_id: int) -> url_guard.StreamedURL:
    provider, config, bucket, key, _region = resolve_config(reference, expected_tenant_id)
    url, path_style = _object_url(provider, config, bucket, key)
    parsed = urllib.parse.urlsplit(url)
    headers = _signed_headers(provider, "GET", parsed, config, bucket, path_style)
    try:
        response = url_guard.open_url("GET", url, headers=headers, timeout=60)
    except url_guard.URLGuardError as exc:
        raise ObjectStorageError(str(exc)) from exc
    if not 200 <= response.status < 300:
        status = response.status
        response.close()
        raise ObjectStorageError(f"object storage returned HTTP {status}", status=status)
    return response

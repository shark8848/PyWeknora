"""Resolve protected file references to tenant-owned local storage."""

from __future__ import annotations

import mimetypes
import urllib.parse
from pathlib import Path

from fastapi.responses import FileResponse
from starlette.responses import StreamingResponse

from .. import config, db
from ..errors import forbidden, not_found, validation
from . import object_storage


_PROVIDER_SCHEMES = {"local", "minio", "cos", "tos", "s3", "oss", "ks3", "obs", "resource"}
_ACTIVE_BROWSER_EXTENSIONS = {".svg", ".svgz", ".html", ".htm", ".xhtml", ".xml", ".js", ".mjs", ".css"}


def _tenant_root(tenant_id: int) -> Path:
    return (config.FILES_DIR / str(tenant_id)).resolve()


def _resolve_resource(reference: str, expected_tenant_id: int) -> tuple[str, str]:
    handle = reference[len("resource://"):].split("/", 1)[0].strip()
    if not handle:
        raise validation("invalid resource reference")
    row = db.fetch_one(
        "SELECT * FROM resources WHERE handle=? AND state='active' AND deleted_at IS NULL",
        (handle,),
    )
    if not row:
        raise not_found("Resource not found")
    if int(row.get("tenant_id") or 0) != expected_tenant_id:
        raise forbidden("resource not accessible")
    physical = str(row.get("physical_path") or "")
    if not physical:
        raise not_found("Resource file not found")
    return physical, str(row.get("original_name") or "")


def _resolve_physical_path(reference: str, expected_tenant_id: int) -> Path:
    value = str(reference or "").strip()
    if not value or ".." in value:
        raise validation("invalid file path")
    if value.startswith("storage://"):
        _, _, remainder = value.partition("storage://")[2].partition("/")
        value = remainder or ""
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.scheme.lower() in _PROVIDER_SCHEMES:
        if parsed.scheme.lower() == "resource":
            raise validation("invalid nested resource reference")
        path_part = urllib.parse.unquote(parsed.path).lstrip("/")
        tenant_part = parsed.netloc.strip()
        if tenant_part.isdigit() and int(tenant_part) != expected_tenant_id:
            raise forbidden("file path not accessible")
        if tenant_part.isdigit():
            candidate = (_tenant_root(expected_tenant_id) / path_part).resolve()
        elif path_part.startswith("files/"):
            candidate = (config.FILES_DIR / path_part[len("files/"):]).resolve()
        else:
            candidate = (config.FILES_DIR / path_part).resolve()
    else:
        candidate = Path(urllib.parse.unquote(value)).resolve()
    root = config.FILES_DIR.resolve()
    tenant_root = _tenant_root(expected_tenant_id)
    if candidate != root and root not in candidate.parents:
        raise forbidden("file path not accessible")
    if candidate != tenant_root and tenant_root not in candidate.parents:
        raise forbidden("file path not accessible")
    if not candidate.is_file():
        raise not_found("File not found")
    return candidate


def resolve_file(reference: str, expected_tenant_id: int) -> tuple[Path, str]:
    value = str(reference or "").strip()
    if value.startswith("resource://"):
        value, resource_name = _resolve_resource(value, expected_tenant_id)
        if object_storage.is_object_reference(value):
            raise not_found("File not found")
        return _resolve_physical_path(value, expected_tenant_id), resource_name
    return _resolve_physical_path(value, expected_tenant_id), ""


def _reference_filename(reference: str) -> str:
    path = urllib.parse.urlsplit(reference).path
    return Path(urllib.parse.unquote(path)).name


def _safe_media_type(filename: str) -> tuple[str, bool]:
    extension = Path(filename).suffix.lower()
    if extension in _ACTIVE_BROWSER_EXTENSIONS:
        return "application/octet-stream", False
    return mimetypes.guess_type(filename)[0] or "application/octet-stream", True


def _content_disposition(filename: str, inline: bool) -> str:
    disposition = "inline" if inline else "attachment"
    if not filename:
        return disposition
    return f"{disposition}; filename*=utf-8''{urllib.parse.quote(filename, safe='')}"


def _object_response(reference: str, expected_tenant_id: int, filename: str) -> StreamingResponse:
    display_name = filename or _reference_filename(reference)
    try:
        opened = object_storage.open_object(reference, expected_tenant_id)
    except object_storage.ObjectStorageError as exc:
        raise not_found("File not found") from exc
    media_type, inline = _safe_media_type(display_name)
    headers = {
        "Cache-Control": "private, max-age=300",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": _content_disposition(display_name, inline),
    }
    length = str(opened.headers.get("content-length") or "")
    if length.isdigit():
        headers["Content-Length"] = length
    response = StreamingResponse(opened.iter_bytes(), media_type=media_type, headers=headers)
    response.object_stream = opened
    return response


def file_response(reference: str, expected_tenant_id: int, *, filename: str = ""):
    value = str(reference or "").strip()
    resource_name = ""
    if value.startswith("resource://"):
        value, resource_name = _resolve_resource(value, expected_tenant_id)
    if object_storage.is_object_reference(value):
        return _object_response(value, expected_tenant_id, filename or resource_name)
    path = _resolve_physical_path(value, expected_tenant_id)
    display_name = filename or resource_name
    media_type, inline = _safe_media_type(display_name or path.name)
    return FileResponse(
        path,
        media_type=media_type,
        filename=display_name or (path.name if not inline else None),
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )

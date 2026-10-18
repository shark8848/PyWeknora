"""Seafile Server (api2) data source connector.

Seafile stores each library as an independent tree. A data source selects one
library and one or more absolute paths inside it. Files are downloaded through
Seafile's authenticated link endpoint and then from the fileserver without the
API token, so the token never follows a vendor-provided download URL.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from . import chunking, url_guard


REPO_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

SUPPORTED_IMPORT_EXTENSIONS = {
    "pdf", "txt", "docx", "doc", "epub",
    "html", "htm", "mhtml", "md", "markdown",
    "xmind", "png", "jpg", "jpeg", "gif",
    "csv", "xlsx", "xls", "pptx", "ppt", "json",
    "mp3", "wav", "m4a", "flac", "ogg",
}

REASON_PERMISSION_DENIED = "seafile_permission_denied"
REASON_NOT_FOUND = "seafile_not_found"
REASON_FILE_TOO_LARGE = "seafile_file_too_large"
REASON_EMPTY_FILE = "seafile_empty_file"
REASON_SOURCE_CHANGED = "seafile_source_changed"
REASON_INVALID_RESPONSE = "seafile_invalid_response"
REASON_SSRF_BLOCKED = "seafile_ssrf_blocked"
REASON_FETCH_FAILED = "seafile_fetch_failed"

REASON_TEXT = {
    REASON_PERMISSION_DENIED: "Permission to access the Seafile resource was denied",
    REASON_NOT_FOUND: "The Seafile resource was not found",
    REASON_FILE_TOO_LARGE: "The Seafile file exceeds the configured size limit",
    REASON_EMPTY_FILE: "The Seafile file is empty",
    REASON_SOURCE_CHANGED: "The Seafile file changed while it was being fetched",
    REASON_INVALID_RESPONSE: "Seafile returned an unexpected response",
    REASON_SSRF_BLOCKED: "The Seafile download URL was blocked by the SSRF policy",
    REASON_FETCH_FAILED: "Failed to fetch the file from Seafile",
}

USER_AGENT = "PyWeKnora-Seafile-Connector/1.0"
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 60.0
LISTING_LIMIT = 32 * 1024 * 1024
SMALL_JSON_LIMIT = 64 * 1024

_sleep = time.sleep


class SeafileError(ValueError):
    """A connector failure with a stable reason code and optional HTTP status."""

    def __init__(
        self,
        message: str,
        *,
        code: str = REASON_FETCH_FAILED,
        status: int = 0,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.fatal = fatal


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _normalise_base_url(raw: Any) -> str:
    value = _clean(raw)
    if not value:
        raise SeafileError("base_url is required", code=REASON_INVALID_RESPONSE)
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SeafileError(
            "base_url must be an http(s) URL without credentials, query or fragment",
            code=REASON_INVALID_RESPONSE,
        )
    return urllib.parse.urlunsplit(parsed).rstrip("/")


def _parse_config(config: dict[str, Any]) -> tuple[str, str]:
    base_url = _normalise_base_url(config.get("base_url"))
    token = _clean(config.get("api_token"))
    if not token or any(character.isspace() or unicodedata.category(character).startswith("C") for character in token):
        raise SeafileError(
            "api_token is required and must not contain whitespace",
            code=REASON_INVALID_RESPONSE,
        )
    return base_url, token


def _normalise_path(value: str) -> str:
    if not value.startswith("/") or "\\" in value or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise SeafileError(
            "seafile path must be absolute without backslashes or control characters",
            code=REASON_INVALID_RESPONSE,
        )
    segments: list[str] = []
    for segment in value.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            raise SeafileError(
                "seafile path must not contain dot segments",
                code=REASON_INVALID_RESPONSE,
            )
        segments.append(segment)
    return "/" + "/".join(segments)


def _parse_resource_id(value: str) -> tuple[str, str]:
    repo_id, separator, raw_path = str(value or "").partition(":")
    if not separator or not REPO_ID_PATTERN.fullmatch(repo_id):
        raise SeafileError(
            "seafile resource id must be <repo_id>:<absolute path>",
            code=REASON_INVALID_RESPONSE,
        )
    return repo_id, _normalise_path(raw_path)


def _resource_ids(config: dict[str, Any]) -> list[str]:
    raw = config.get("resource_ids") or []
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        raw = []
    result: list[str] = []
    for item in raw:
        value = _clean(item)
        if value and value not in result:
            result.append(value)
    return result


def _collapse_roots(ids: list[str]) -> tuple[str, list[str]]:
    if not ids:
        raise SeafileError("seafile requires at least one selected resource", code=REASON_INVALID_RESPONSE)
    repo_id = ""
    selected: set[str] = set()
    for value in ids:
        repo, path = _parse_resource_id(value)
        if repo_id and repo != repo_id:
            raise SeafileError(
                "seafile selections must belong to one library",
                code=REASON_INVALID_RESPONSE,
            )
        repo_id = repo
        selected.add(path)
    roots: list[str] = []
    for path in sorted(selected):
        parent = path
        covered = False
        while parent != "/":
            parent = _parent_path(parent)
            if parent in selected:
                covered = True
                break
        if not covered:
            roots.append(path)
    return repo_id, roots


def _parent_path(path: str) -> str:
    parent = posixpath.dirname(path)
    return parent if parent.startswith("/") else "/"


def _resource_id(repo_id: str, path: str) -> str:
    return repo_id + ":" + path


def _external_id(repo_id: str, path: str) -> str:
    return "seafile:" + repo_id + ":" + path


def _parse_external_id(value: str) -> tuple[str, str] | None:
    prefix = "seafile:"
    if not value.startswith(prefix):
        return None
    repo_id, separator, path = value[len(prefix):].partition(":")
    if not separator or not REPO_ID_PATTERN.fullmatch(repo_id) or not path.startswith("/"):
        return None
    return repo_id, path


def _fingerprint(oid: str, mtime: int, size: int) -> str:
    return f"o:{oid}|m:{mtime}|s:{size}"


def _valid_entry_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and "/" not in name and "\\" not in name and not any(
        unicodedata.category(character).startswith("C") for character in name
    )


def _sanitise_segment(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", value).strip().strip(".")
    return value or "_"


def _knowledge_file_name(repo_name: str, path: str) -> str:
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if not segments:
        return _sanitise_segment(repo_name)
    file_name = segments[-1]
    stem, extension = posixpath.splitext(file_name)
    segments[-1] = _sanitise_segment(stem) + extension
    return _sanitise_segment(repo_name) + "/" + "/".join(segments)


def _extension(path: str) -> str:
    return posixpath.splitext(path)[1].lower().lstrip(".")


def _supported(path: str) -> bool:
    return _extension(path) in SUPPORTED_IMPORT_EXTENSIONS


def _retry_after(value: str) -> float:
    raw = _clean(value)
    if not raw:
        return 0.0
    if raw.isdigit():
        seconds = int(raw)
        if seconds > MAX_RETRY_AFTER:
            raise SeafileError(
                "seafile asked to retry after more than 60 seconds",
                fatal=True,
            )
        return float(seconds)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    wait = (when - datetime.now(timezone.utc)).total_seconds()
    if wait > MAX_RETRY_AFTER:
        raise SeafileError(
            "seafile asked to retry after more than 60 seconds",
            fatal=True,
        )
    return max(0.0, wait)


def _max_file_bytes() -> int:
    try:
        value = int(os.getenv("MAX_FILE_SIZE_MB", ""))
    except ValueError:
        value = 50
    return (value if value > 0 else 50) * 1024 * 1024


def _json(value: bytes, op: str) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise SeafileError(
            f"seafile {op}: unexpected response",
            code=REASON_INVALID_RESPONSE,
        ) from exc


class SeafileClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.token = token

    def _get(
        self,
        url: str,
        op: str,
        *,
        authenticated: bool,
        limit: int,
    ) -> tuple[bytes, dict[str, str]]:
        headers = {"User-Agent": USER_AGENT}
        if authenticated:
            headers.update(
                {
                    "Authorization": "Token " + self.token,
                    "Accept": "application/json",
                }
            )
        for attempt in range(MAX_ATTEMPTS):
            retry_delay = 0.0
            try:
                status, response_headers, body, _final_url = url_guard.request_url(
                    "GET",
                    url,
                    headers=headers,
                    timeout=30 if authenticated else 120,
                    max_bytes=limit,
                )
            except url_guard.URLGuardError as exc:
                message = str(exc).lower()
                if "blocked" in message or "private" in message or "loopback" in message:
                    raise SeafileError(
                        REASON_TEXT[REASON_SSRF_BLOCKED],
                        code=REASON_SSRF_BLOCKED,
                    ) from exc
                if "download limit" in message:
                    raise SeafileError(
                        REASON_TEXT[REASON_FILE_TOO_LARGE],
                        code=REASON_FILE_TOO_LARGE,
                    ) from exc
                if attempt + 1 < MAX_ATTEMPTS and ("timed out" in message or "connect" in message):
                    retry_delay = float(1 << attempt)
                else:
                    raise SeafileError(f"seafile {op}: request failed") from exc
            else:
                if status == 200 or (authenticated and 200 <= status < 300):
                    if len(body) > limit:
                        raise SeafileError(
                            f"seafile {op}: unexpected response",
                            code=REASON_INVALID_RESPONSE,
                        )
                    return body, response_headers
                if status in {429, 502, 503, 504} and attempt + 1 < MAX_ATTEMPTS:
                    retry_delay = _retry_after(str(response_headers.get("retry-after") or ""))
                    if retry_delay <= 0:
                        retry_delay = float(1 << attempt)
                else:
                    raise SeafileError(
                        f"seafile {op}: HTTP {status}",
                        code=REASON_FETCH_FAILED,
                        status=status,
                    )
            _sleep(retry_delay)
        raise SeafileError(f"seafile {op}: request failed")

    def _get_json(self, endpoint: str, op: str, limit: int) -> tuple[Any, dict[str, str]]:
        body, headers = self._get(
            self.base_url + endpoint,
            op,
            authenticated=True,
            limit=limit,
        )
        return _json(body, op), headers

    def ping(self) -> None:
        value, _headers = self._get_json("/api2/auth/ping/", "ping", SMALL_JSON_LIMIT)
        if value != "pong":
            raise SeafileError(
                "seafile ping: unexpected response",
                code=REASON_INVALID_RESPONSE,
            )

    def list_repos(self) -> list[dict[str, Any]]:
        value, _headers = self._get_json("/api2/repos/", "repos", LISTING_LIMIT)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise SeafileError(
                "seafile repos: unexpected response",
                code=REASON_INVALID_RESPONSE,
            )
        return value

    def list_dir(self, repo_id: str, path: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"p": path})
        endpoint = f"/api2/repos/{urllib.parse.quote(repo_id, safe='')}/dir/?{query}"
        value, _headers = self._get_json(endpoint, "dir", LISTING_LIMIT)
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise SeafileError(
                "seafile dir: unexpected response",
                code=REASON_INVALID_RESPONSE,
            )
        return value

    def download_link(self, repo_id: str, path: str) -> tuple[str, str]:
        query = urllib.parse.urlencode({"p": path, "reuse": "1"})
        endpoint = f"/api2/repos/{urllib.parse.quote(repo_id, safe='')}/file/?{query}"
        value, headers = self._get_json(endpoint, "file", SMALL_JSON_LIMIT)
        link = value if isinstance(value, str) else ""
        parsed = urllib.parse.urlsplit(link)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise SeafileError(
                "seafile file: unexpected response",
                code=REASON_INVALID_RESPONSE,
            )
        return link, str(headers.get("oid") or "")

    def download(self, link: str, expected_size: int, limit: int) -> bytes:
        body, _headers = self._get(link, "download", authenticated=False, limit=limit)
        if not body:
            raise SeafileError(REASON_TEXT[REASON_EMPTY_FILE], code=REASON_EMPTY_FILE)
        if len(body) != expected_size:
            raise SeafileError(
                REASON_TEXT[REASON_SOURCE_CHANGED],
                code=REASON_SOURCE_CHANGED,
            )
        return body


def _client(config: dict[str, Any]) -> SeafileClient:
    base_url, token = _parse_config(config)
    return SeafileClient(base_url, token)


def _repo(repos: list[dict[str, Any]], repo_id: str) -> dict[str, Any]:
    for repo in repos:
        if _clean(repo.get("id")) != repo_id:
            continue
        if bool(repo.get("encrypted")):
            raise SeafileError(
                "encrypted Seafile libraries are not supported",
                code=REASON_INVALID_RESPONSE,
            )
        return repo
    raise SeafileError(
        f"Seafile library {repo_id}",
        code=REASON_NOT_FOUND,
    )


def _map_directory_error(exc: SeafileError, path: str) -> SeafileError:
    if exc.status == 404:
        return SeafileError(f"Seafile path {path}", code=REASON_NOT_FOUND, status=404)
    if exc.status == 403:
        return SeafileError(
            f"{REASON_PERMISSION_DENIED}: Seafile path {path}",
            code=REASON_PERMISSION_DENIED,
            status=403,
        )
    return exc


def _validate_listing(entries: list[dict[str, Any]], path: str) -> None:
    names: set[str] = set()
    for entry in entries:
        name = _clean(entry.get("name"))
        kind = _clean(entry.get("type"))
        try:
            mtime = int(entry.get("mtime"))
        except (TypeError, ValueError):
            mtime = -1
        size = entry.get("size")
        invalid = (
            not _valid_entry_name(name)
            or name in names
            or kind not in {"file", "dir"}
            or mtime < 0
            or (kind == "file" and (not isinstance(size, int) or isinstance(size, bool) or size < 0))
        )
        if invalid:
            raise SeafileError(
                f"{REASON_INVALID_RESPONSE}: directory listing {path}",
                code=REASON_INVALID_RESPONSE,
            )
        names.add(name)


def _error_item(
    repo_id: str,
    repo_name: str,
    root: str,
    path: str,
    entry: dict[str, Any],
    exc: SeafileError,
    *,
    known: bool,
) -> dict[str, Any]:
    mtime = int(entry.get("mtime") or 0)
    item = {
        "external_id": _external_id(repo_id, path),
        "title": repo_name + "/" + path.lstrip("/"),
        "file_name": _knowledge_file_name(repo_name, path),
        "file_type": _extension(path),
        "content": "",
        "url": "seafile://" + repo_id + path,
        "resource_id": _resource_id(repo_id, root),
        "updated_at": _iso_time(mtime),
        "error": REASON_TEXT.get(exc.code, REASON_TEXT[REASON_FETCH_FAILED]),
        "metadata": {
            "channel": "seafile",
            "source_type": "seafile",
            "seafile_path": path,
            "error": REASON_TEXT.get(exc.code, REASON_TEXT[REASON_FETCH_FAILED]),
            "error_reason_code": exc.code,
            "error_reason": REASON_TEXT.get(exc.code, REASON_TEXT[REASON_FETCH_FAILED]),
        },
    }
    if exc.status:
        item["metadata"]["error_reason_code_value"] = str(exc.status)
    if known:
        item["fingerprint"] = ""
    return item


def _iso_time(value: int) -> str:
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def _content_item(
    repo_id: str,
    repo_name: str,
    root: str,
    path: str,
    entry: dict[str, Any],
    body: bytes,
) -> dict[str, Any]:
    file_name = _knowledge_file_name(repo_name, path)
    extension = _extension(path)
    if "." + extension not in chunking.SUPPORTED_EXTENSIONS:
        raise SeafileError(
            "The Seafile file type has no supported text extractor",
            code=REASON_FETCH_FAILED,
        )
    text = chunking.extract_text(file_name, body)
    if not text.strip():
        raise SeafileError(
            "No extractable text found in the Seafile file",
            code=REASON_EMPTY_FILE,
        )
    mtime = int(entry.get("mtime") or 0)
    return {
        "external_id": _external_id(repo_id, path),
        "title": repo_name + "/" + path.lstrip("/"),
        "file_name": file_name,
        "file_type": extension,
        "content": text,
        "url": "seafile://" + repo_id + path,
        "resource_id": _resource_id(repo_id, root),
        "updated_at": _iso_time(mtime),
        "fingerprint": _fingerprint(_clean(entry.get("id")), mtime, int(entry.get("size") or 0)),
        "metadata": {
            "channel": "seafile",
            "source_type": "seafile",
            "seafile_repo_id": repo_id,
            "seafile_repo_name": repo_name,
            "seafile_path": path,
            "seafile_object_id": _clean(entry.get("id")),
            "seafile_mtime": str(mtime),
            "seafile_size": str(int(entry.get("size") or 0)),
        },
    }


def list_resources(
    config: dict[str, Any],
    parent_id: str = "",
) -> list[dict[str, Any]]:
    client = _client(config)
    result: list[dict[str, Any]] = []
    if parent_id:
        repo_id, path = _parse_resource_id(parent_id)
        try:
            entries = client.list_dir(repo_id, path)
        except SeafileError as exc:
            raise _map_directory_error(exc, path) from exc
        for entry in entries:
            name = _clean(entry.get("name"))
            kind = _clean(entry.get("type"))
            if not _valid_entry_name(name):
                continue
            if kind == "file" and not _supported(name):
                continue
            if kind not in {"file", "dir"}:
                continue
            resource_path = posixpath.join(path, name)
            result.append(
                {
                    "external_id": _resource_id(repo_id, resource_path),
                    "name": name,
                    "type": "directory" if kind == "dir" else "file",
                    "description": "",
                    "parent_id": parent_id,
                    "has_children": kind == "dir",
                    "modified_at": _iso_time(int(entry.get("mtime") or 0)),
                }
            )
    else:
        for repo in client.list_repos():
            if bool(repo.get("encrypted")):
                continue
            repo_id = _clean(repo.get("id"))
            if not REPO_ID_PATTERN.fullmatch(repo_id):
                continue
            result.append(
                {
                    "external_id": _resource_id(repo_id, "/"),
                    "name": _clean(repo.get("name")),
                    "type": "library",
                    "description": _clean(repo.get("type")),
                    "has_children": True,
                }
            )
    result.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("external_id") or "")))
    return result


def validate(config: dict[str, Any]) -> dict[str, Any]:
    client = _client(config)
    ids = _resource_ids(config)
    repo_id = ""
    if ids:
        repo_id, _roots = _collapse_roots(ids)
    client.ping()
    if repo_id:
        _repo(client.list_repos(), repo_id)
    return {"valid": True, "items": 0, "resources": len(ids)}


def resource_ancestors(ids: list[str]) -> list[str]:
    result: list[str] = []
    for value in ids:
        try:
            repo_id, path = _parse_resource_id(value)
        except SeafileError:
            continue
        if path == "/":
            continue
        candidates = [_resource_id(repo_id, "/")]
        for index, character in enumerate(path):
            if character == "/" and index > 0:
                candidates.append(_resource_id(repo_id, path[:index]))
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
    return sorted(result)


def _previous_files(
    previous_cursor: dict[str, Any] | None,
    repo_id: str,
) -> dict[str, str]:
    if not isinstance(previous_cursor, dict):
        return {}
    connector = previous_cursor.get("connector_cursor")
    if not isinstance(connector, dict):
        return {}
    bound_repo_id = _clean(connector.get("repo_id"))
    if bound_repo_id and bound_repo_id != repo_id:
        bound_repo_name = _clean(connector.get("repo_name")) or bound_repo_id
        raise SeafileError(
            "this data source already synced a different Seafile library "
            f"({bound_repo_name}); restore that library or create a new data source",
            code=REASON_INVALID_RESPONSE,
        )
    raw_items = connector.get("items")
    if not isinstance(raw_items, dict):
        return {}
    result: dict[str, str] = {}
    for external_id, value in raw_items.items():
        parsed = _parse_external_id(str(external_id))
        if parsed is None:
            continue
        previous_repo, path = parsed
        if previous_repo != repo_id:
            raise SeafileError(
                "this data source already synced a different Seafile library; "
                "restore that library or create a new data source",
                code=REASON_INVALID_RESPONSE,
            )
        result[path] = str(value or "")
    return result


def _scan_file(
    client: SeafileClient,
    repo_id: str,
    repo_name: str,
    root: str,
    path: str,
    entry: dict[str, Any],
    known: dict[str, str],
    limit: int,
) -> dict[str, Any] | None:
    size = int(entry.get("size") or 0)
    fingerprint = _fingerprint(_clean(entry.get("id")), int(entry.get("mtime") or 0), size)
    if known.get(path) and known[path] == fingerprint:
        return None
    if size > limit:
        return _error_item(
            repo_id,
            repo_name,
            root,
            path,
            entry,
            SeafileError(REASON_TEXT[REASON_FILE_TOO_LARGE], code=REASON_FILE_TOO_LARGE),
            known=path in known,
        )
    try:
        link, oid = client.download_link(repo_id, path)
        if oid and _clean(entry.get("id")) and oid != _clean(entry.get("id")):
            raise SeafileError(REASON_TEXT[REASON_SOURCE_CHANGED], code=REASON_SOURCE_CHANGED)
        body = client.download(link, size, limit)
        return _content_item(repo_id, repo_name, root, path, entry, body)
    except SeafileError as exc:
        if exc.fatal:
            raise
        if exc.code == REASON_FETCH_FAILED and exc.status == 403:
            exc = SeafileError(REASON_TEXT[REASON_PERMISSION_DENIED], code=REASON_PERMISSION_DENIED, status=403)
        elif exc.code == REASON_FETCH_FAILED and exc.status == 404:
            exc = SeafileError(REASON_TEXT[REASON_NOT_FOUND], code=REASON_NOT_FOUND, status=404)
        return _error_item(
            repo_id,
            repo_name,
            root,
            path,
            entry,
            exc,
            known=path in known,
        )


def _walk_root(
    client: SeafileClient,
    repo_id: str,
    repo_name: str,
    root: str,
    entries: list[dict[str, Any]],
    known: dict[str, str],
    limit: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack: list[tuple[str, list[dict[str, Any]]]] = [(root, entries)]
    while stack:
        directory, current = stack.pop()
        _validate_listing(current, directory)
        current = sorted(current, key=lambda entry: _clean(entry.get("name")))
        for entry in current:
            name = _clean(entry.get("name"))
            kind = _clean(entry.get("type"))
            path = posixpath.join(directory, name)
            if kind == "file" and _supported(path):
                if path in seen:
                    continue
                seen.add(path)
                item = _scan_file(
                    client, repo_id, repo_name, root, path, entry, known, limit
                )
                if item is not None:
                    items.append(item)
        for entry in reversed(current):
            if _clean(entry.get("type")) != "dir":
                continue
            child = posixpath.join(directory, _clean(entry.get("name")))
            try:
                child_entries = client.list_dir(repo_id, child)
            except SeafileError as exc:
                raise _map_directory_error(exc, child) from exc
            stack.append((child, child_entries))
    return items, seen


def fetch_items(
    config: dict[str, Any],
    previous_cursor: dict[str, Any] | None = None,
    *,
    force_full: bool = False,
    cursor_meta: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    client = _client(config)
    repo_id, roots = _collapse_roots(_resource_ids(config))
    known = _previous_files(previous_cursor, repo_id)
    repo = _repo(client.list_repos(), repo_id)
    repo_name = _clean(repo.get("name"))
    if cursor_meta is not None:
        cursor_meta.update({"repo_id": repo_id, "repo_name": repo_name})
    skip_known = {} if force_full else known
    limit = _max_file_bytes()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        try:
            entries = client.list_dir(repo_id, root)
        except SeafileError as exc:
            if exc.status != 404 or root == "/":
                raise _map_directory_error(exc, root) from exc
            parent = _parent_path(root)
            try:
                parent_entries = client.list_dir(repo_id, parent)
            except SeafileError as parent_exc:
                raise _map_directory_error(parent_exc, parent) from parent_exc
            _validate_listing(parent_entries, parent)
            match = next(
                (
                    entry
                    for entry in parent_entries
                    if _clean(entry.get("name")) == posixpath.basename(root)
                    and _clean(entry.get("type")) == "file"
                ),
                None,
            )
            if match is None:
                raise SeafileError(f"Seafile path {root}", code=REASON_NOT_FOUND, status=404)
            if not _supported(root):
                continue
            seen.add(root)
            item = _scan_file(
                client, repo_id, repo_name, root, root, match, skip_known, limit
            )
            if item is not None:
                items.append(item)
            continue
        scanned, root_seen = _walk_root(
            client, repo_id, repo_name, root, entries, skip_known, limit
        )
        items.extend(scanned)
        seen.update(root_seen)

    for path in sorted(set(known) - seen):
        items.append(
            {
                "external_id": _external_id(repo_id, path),
                "title": repo_name + "/" + path.lstrip("/"),
                "content": "",
                "is_deleted": True,
                "resource_id": _resource_id(repo_id, "/"),
                "metadata": {"channel": "seafile", "seafile_path": path},
            }
        )
    return items

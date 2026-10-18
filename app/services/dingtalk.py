"""DingTalk knowledge-base connector.

This is the Python counterpart of the Go connector's online-document path. It
reads native DingTalk documents through the wiki and document-block APIs.
Uploaded Office/PDF files are intentionally outside this build: the sync
pipeline currently stores markdown text, not downloaded binary documents.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import chunking, url_guard


API_BASE_URL = "https://api.dingtalk.com"
MAX_PAGES = 1000
MAX_TRAVERSAL_NODES = 1_000_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_RENDER_DEPTH = 32
RESOURCE_ID_PREFIX = "dingtalk:v1?"
MAX_RESOURCE_ID_BYTES = 8 * 1024
MAX_RESOURCE_DEPTH = 64
MAX_VALIDATE_PROBES = 5
MAX_VALIDATE_LISTINGS = 20
MAX_UPLOADED_DOCUMENT_BYTES = 64 * 1024 * 1024
BINARY_DOCUMENT_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}


class DingTalkError(ValueError):
    """A DingTalk configuration, API, or resource error."""


@dataclass(frozen=True)
class Workspace:
    id: str
    root_node_id: str = ""
    name: str = ""
    description: str = ""
    url: str = ""
    modified_time: str = ""


@dataclass(frozen=True)
class Node:
    id: str
    workspace_id: str = ""
    name: str = ""
    type: str = ""
    category: str = ""
    extension: str = ""
    url: str = ""
    modified_time: str = ""
    modified_timestamp: int = 0
    has_children: bool = False

    @property
    def title(self) -> str:
        return self.name.strip() or self.id

    @property
    def is_folder(self) -> bool:
        return self.type.upper() == "FOLDER"

    @property
    def is_online_document(self) -> bool:
        return (
            self.type.upper() == "FILE"
            and self.category.upper() == "ALIDOC"
            and self.extension.lower() == "adoc"
        )

    @property
    def is_binary_document(self) -> bool:
        return (
            self.type.upper() == "FILE"
            and self.category.upper() == "DOCUMENT"
            and self.extension.lower() in BINARY_DOCUMENT_TYPES
        )

    @property
    def content_type(self) -> str:
        return BINARY_DOCUMENT_TYPES.get(self.extension.lower(), "application/octet-stream")

    @property
    def revision(self) -> str:
        return str(self.modified_timestamp) if self.modified_timestamp > 0 else self.modified_time.strip()

    @property
    def updated_at(self) -> str:
        if self.modified_timestamp > 0:
            value = datetime.fromtimestamp(self.modified_timestamp / 1000, tz=timezone.utc)
            return value.isoformat().replace("+00:00", "Z")
        return self.modified_time.strip()


@dataclass(frozen=True)
class ResourceRef:
    workspace_id: str
    node_id: str = ""
    ancestors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Scope:
    resource_id: str
    reference: ResourceRef
    start_node_id: str = ""
    document: Node | None = None


@dataclass
class RenderResult:
    markdown: str
    unknown_types: list[str] = field(default_factory=list)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    return default


def _node(raw: dict[str, Any]) -> Node:
    return Node(
        id=_clean(raw.get("nodeId")),
        workspace_id=_clean(raw.get("workspaceId")),
        name=_clean(raw.get("name")),
        type=_clean(raw.get("type")),
        category=_clean(raw.get("category")),
        extension=_clean(raw.get("extension")),
        url=_clean(raw.get("url")),
        modified_time=_clean(raw.get("modifiedTime")),
        modified_timestamp=int(raw.get("modifiedTimestamp") or 0),
        has_children=bool(raw.get("hasChildren")),
    )


def _workspace(raw: dict[str, Any]) -> Workspace:
    return Workspace(
        id=_clean(raw.get("workspaceId")),
        root_node_id=_clean(raw.get("rootNodeId")),
        name=_clean(raw.get("name")),
        description=_clean(raw.get("description")),
        url=_clean(raw.get("url")),
        modified_time=_clean(raw.get("modifiedTime")),
    )


def _parse_config(config: dict[str, Any]) -> dict[str, str]:
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}

    def value(name: str) -> str:
        return _clean(config.get(name) or settings.get(name))

    result = {
        "client_id": value("client_id"),
        "client_secret": value("client_secret"),
        "operator_id": value("operator_id"),
    }
    for name, label in (
        ("client_id", "client_id"),
        ("client_secret", "client_secret"),
        ("operator_id", "operator_id"),
    ):
        if not result[name]:
            raise DingTalkError(f"{label} is required")
    return result


def _includes_uploaded_files(config: dict[str, Any]) -> bool:
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    return _as_bool(settings.get("include_uploaded_files"), False)


class DingTalkClient:
    def __init__(self, config: dict[str, Any], *, base_url: str = API_BASE_URL) -> None:
        values = _parse_config(config)
        self.base_url = base_url.rstrip("/")
        self.client_id = values["client_id"]
        self.client_secret = values["client_secret"]
        self.operator_id = values["operator_id"]
        self._token = ""
        self._token_expiry = 0.0

    def _call(
        self, method: str, path: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes, str]:
        try:
            return url_guard.request_url(
                method,
                self.base_url + path,
                headers=headers,
                body=body,
                timeout=30,
                max_bytes=MAX_RESPONSE_BYTES,
            )
        except Exception as exc:  # noqa: BLE001 - normalized and redacted below
            message = str(exc)
            for secret in (self.client_id, self.client_secret, self.operator_id, self._token):
                if secret:
                    message = message.replace(secret, "[REDACTED]")
            raise DingTalkError("execute DingTalk request: " + message) from exc

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry:
            return self._token
        body = json.dumps({"appKey": self.client_id, "appSecret": self.client_secret}).encode()
        status, _headers, raw, _url = self._call(
            "POST",
            "/v1.0/oauth2/accessToken",
            body,
            {"Accept": "application/json", "Content-Type": "application/json"},
        )
        if status < 200 or status >= 300:
            raise self._api_error(status, raw)
        try:
            value = json.loads(raw or b"{}")
        except ValueError as exc:
            raise DingTalkError("decode DingTalk access token response") from exc
        token = _clean(value.get("accessToken") if isinstance(value, dict) else "")
        if not token:
            raise DingTalkError("DingTalk returned an empty access token")
        ttl = int(value.get("expireIn") or 0) if isinstance(value, dict) else 0
        if ttl <= 0:
            ttl = 90 * 60
        if ttl > 5 * 60:
            ttl -= 5 * 60
        self._token = token
        self._token_expiry = time.time() + ttl
        return token

    def _request_json(self, method: str, path: str, body: Any = None, *, authenticated: bool = True) -> Any:
        payload = None if body is None else json.dumps(body).encode()
        refreshed = False
        attempt = 0
        while True:
            headers = {"Accept": "application/json", "Content-Type": "application/json"}
            token = ""
            if authenticated:
                token = self._access_token()
                headers["x-acs-dingtalk-access-token"] = token
            status, response_headers, raw, _url = self._call(method, path, payload, headers)
            if authenticated and status == 401 and not refreshed:
                if self._token == token:
                    self._token = ""
                    self._token_expiry = 0.0
                refreshed = True
                continue
            if status == 429 or status >= 500:
                if attempt < 2:
                    delay = min(2 ** attempt, 8)
                    retry_after = _clean(response_headers.get("retry-after"))
                    if retry_after:
                        try:
                            delay = max(0.0, float(retry_after))
                        except ValueError:
                            pass
                    attempt += 1
                    time.sleep(delay)
                    continue
            if status < 200 or status >= 300:
                raise self._api_error(status, raw)
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError as exc:
                raise DingTalkError("decode DingTalk response") from exc

    def _api_error(self, status: int, raw: bytes) -> DingTalkError:
        code = message = ""
        try:
            value = json.loads(raw or b"{}")
            if isinstance(value, dict):
                code = _clean(value.get("code"))
                message = _clean(value.get("message"))
        except ValueError:
            pass
        text = f"DingTalk API status={status}"
        if code:
            text += f" code={code}"
        if message:
            text += f": {message}"
        for secret in (self.client_id, self.client_secret, self.operator_id, self._token):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return DingTalkError(text)

    def list_workspaces(self) -> list[Workspace]:
        result: list[Workspace] = []
        next_token = ""
        seen: set[str] = set()
        for _page in range(MAX_PAGES):
            query: dict[str, Any] = {"maxResults": 30, "operatorId": self.operator_id}
            if next_token:
                query["nextToken"] = next_token
            payload = self._request_json("GET", "/v2.0/wiki/workspaces?" + urllib.parse.urlencode(query))
            if not isinstance(payload, dict):
                raise DingTalkError("DingTalk workspace response is invalid")
            result.extend(_workspace(item) for item in payload.get("workspaces") or [] if isinstance(item, dict))
            next_token = _clean(payload.get("nextToken"))
            if not next_token:
                return result
            if next_token in seen:
                raise DingTalkError("DingTalk workspace pagination repeated nextToken")
            seen.add(next_token)
        raise DingTalkError(f"DingTalk workspace pagination exceeded {MAX_PAGES} pages")

    def list_nodes(self, parent_node_id: str) -> list[Node]:
        result: list[Node] = []
        next_token = ""
        seen: set[str] = set()
        for _page in range(MAX_PAGES):
            nodes, next_token = self.list_nodes_page(parent_node_id, next_token)
            result.extend(nodes)
            if not next_token:
                return result
            if next_token in seen:
                raise DingTalkError("DingTalk node pagination repeated nextToken")
            seen.add(next_token)
        raise DingTalkError(f"DingTalk node pagination exceeded {MAX_PAGES} pages")

    def list_nodes_page(self, parent_node_id: str, next_token: str = "") -> tuple[list[Node], str]:
        query: dict[str, Any] = {
            "maxResults": 50,
            "operatorId": self.operator_id,
            "parentNodeId": parent_node_id,
        }
        if next_token:
            query["nextToken"] = next_token
        payload = self._request_json("GET", "/v2.0/wiki/nodes?" + urllib.parse.urlencode(query))
        if not isinstance(payload, dict):
            raise DingTalkError("DingTalk node response is invalid")
        nodes = [_node(item) for item in payload.get("nodes") or [] if isinstance(item, dict)]
        return nodes, _clean(payload.get("nextToken"))

    def document_blocks(self, document_id: str) -> list[Any]:
        result: list[Any] = []
        page_size = 100
        for page in range(MAX_PAGES):
            start = page * page_size
            query = {
                "endIndex": start + page_size - 1,
                "operatorId": self.operator_id,
                "startIndex": start,
            }
            path = (
                "/v1.0/doc/suites/documents/"
                + urllib.parse.quote(document_id, safe="")
                + "/blocks?"
                + urllib.parse.urlencode(query)
            )
            payload = self._request_json("GET", path)
            if not isinstance(payload, dict) or payload.get("success") is not True:
                raise DingTalkError("DingTalk document blocks request was unsuccessful")
            container = payload.get("result")
            data = container.get("data") if isinstance(container, dict) else None
            if not isinstance(data, list):
                raise DingTalkError("DingTalk document blocks response is invalid")
            result.extend(data)
            if len(data) < page_size:
                return result
        raise DingTalkError(f"DingTalk document block pagination exceeded {MAX_PAGES} pages")

    def resolve_dentry(self, document_id: str) -> dict[str, str]:
        path = (
            "/v2.0/doc/dentries/"
            + urllib.parse.quote(document_id, safe="")
            + "/queryDentryId?"
            + urllib.parse.urlencode({"operatorId": self.operator_id})
        )
        payload = self._request_json("GET", path)
        if not isinstance(payload, dict):
            raise DingTalkError("DingTalk dentry response is invalid")
        dentry_id = _clean(payload.get("dentryId"))
        space_id = _clean(payload.get("spaceId"))
        if not dentry_id or not space_id:
            raise DingTalkError("DingTalk returned no storage location for the document")
        return {
            "dentry_id": dentry_id,
            "space_id": space_id,
            "dentry_uuid": _clean(payload.get("dentryUuid")),
        }

    def request_download_url(self, ref: dict[str, str]) -> tuple[str, dict[str, str]]:
        path = (
            "/v1.0/storage/spaces/"
            + urllib.parse.quote(ref["space_id"], safe="")
            + "/dentries/"
            + urllib.parse.quote(ref["dentry_id"], safe="")
            + "/downloadInfos/query?"
            + urllib.parse.urlencode({"unionId": self.operator_id})
        )
        payload = self._request_json(
            "POST",
            path,
            {"option": {"version": 1, "preferIntranet": False}},
        )
        info = payload.get("headerSignatureInfo") if isinstance(payload, dict) else None
        urls = info.get("resourceUrls") if isinstance(info, dict) else None
        if not isinstance(urls, list) or not urls or not _clean(urls[0]):
            protocol = _clean(payload.get("protocol")) if isinstance(payload, dict) else ""
            raise DingTalkError(
                f"DingTalk returned no download URL for the document (protocol {protocol!r})"
            )
        headers = info.get("headers") if isinstance(info.get("headers"), dict) else {}
        return _clean(urls[0]), {
            str(name): str(value) for name, value in headers.items()
        }

    def verify_document_download(self, document_id: str) -> None:
        ref = self.resolve_dentry(document_id)
        self.request_download_url(ref)

    def download_document(self, document_id: str) -> bytes:
        ref = self.resolve_dentry(document_id)
        last_error = ""
        for attempt in range(3):
            if attempt:
                time.sleep(0.5 * attempt)
            try:
                url, headers = self.request_download_url(ref)
                headers = {**headers, "Accept-Encoding": "identity"}
                status, _response_headers, body, _final_url = url_guard.request_url(
                    "GET",
                    url,
                    headers=headers,
                    timeout=300,
                    max_bytes=MAX_UPLOADED_DOCUMENT_BYTES,
                )
            except DingTalkError as exc:
                raise exc
            except Exception as exc:  # noqa: BLE001 - signed URLs must not leak in errors
                last_error = type(exc).__name__
                continue
            if status == 200:
                if len(body) > MAX_UPLOADED_DOCUMENT_BYTES:
                    raise DingTalkError("DingTalk document exceeds the 64 MiB download limit")
                return body
            last_error = f"HTTP {status}"
            if status not in {401, 403, 404, 408, 425, 429} and status < 500:
                break
        raise DingTalkError(f"download DingTalk document failed: {last_error or 'request failed'}")


def _valid_external_id(value: str) -> str:
    value = _clean(value)
    if not value or value != str(value).strip() or any(char < " " or char == "\x7f" for char in value):
        raise DingTalkError("DingTalk resource has an invalid ID")
    return value


def _encode_ref(ref: ResourceRef) -> str:
    workspace_id = _valid_external_id(ref.workspace_id)
    node_id = _valid_external_id(ref.node_id) if ref.node_id else ""
    if not node_id:
        if ref.ancestors:
            raise DingTalkError("DingTalk workspace resource cannot have ancestors")
        return workspace_id
    if len(ref.ancestors) > MAX_RESOURCE_DEPTH:
        raise DingTalkError(f"DingTalk resource depth exceeds {MAX_RESOURCE_DEPTH}")
    ancestors = [_valid_external_id(value) for value in ref.ancestors]
    if len(set(ancestors)) != len(ancestors) or node_id in ancestors:
        raise DingTalkError("DingTalk resource contains an ancestor cycle")
    query = [("ancestor", value) for value in ancestors]
    query.extend((("node", node_id), ("workspace", workspace_id)))
    encoded = RESOURCE_ID_PREFIX + urllib.parse.urlencode(query)
    if len(encoded.encode()) > MAX_RESOURCE_ID_BYTES:
        raise DingTalkError(f"DingTalk resource ID exceeds {MAX_RESOURCE_ID_BYTES} bytes")
    return encoded


def _decode_ref(raw: str) -> ResourceRef:
    value = str(raw or "")
    if not value or len(value.encode()) > MAX_RESOURCE_ID_BYTES:
        raise DingTalkError("invalid DingTalk resource ID")
    if not value.startswith(RESOURCE_ID_PREFIX):
        return ResourceRef(workspace_id=_valid_external_id(value))
    parsed = urllib.parse.parse_qs(value[len(RESOURCE_ID_PREFIX):], keep_blank_values=True)
    if set(parsed) - {"workspace", "node", "ancestor"}:
        raise DingTalkError("DingTalk resource ID contains an unsupported field")
    workspaces = parsed.get("workspace") or []
    nodes = parsed.get("node") or []
    if len(workspaces) != 1 or len(nodes) != 1:
        raise DingTalkError("DingTalk resource ID has invalid workspace or node fields")
    ref = ResourceRef(_valid_external_id(workspaces[0]), _valid_external_id(nodes[0]), tuple(parsed.get("ancestor") or []))
    _encode_ref(ref)
    return ref


def _child_ref(parent: ResourceRef, node_id: str) -> ResourceRef:
    ancestors = parent.ancestors + ((parent.node_id,) if parent.node_id else ())
    return ResourceRef(parent.workspace_id, node_id, ancestors)


def _scope_id(ref: ResourceRef) -> str:
    return _encode_ref(ref)


def _resolve_scope(
    client: DingTalkClient,
    workspaces: dict[str, Workspace],
    resource_id: str,
    children_cache: dict[str, list[Node]],
    include_uploaded: bool = False,
) -> Scope:
    ref = _decode_ref(resource_id)
    canonical = _encode_ref(ref)
    workspace = workspaces.get(ref.workspace_id)
    if workspace is None:
        raise DingTalkError(f"DingTalk workspace {ref.workspace_id!r} is unavailable")
    root_node_id = workspace.root_node_id.strip()
    if not root_node_id:
        raise DingTalkError(f"DingTalk workspace {ref.workspace_id!r} has no root node")
    if not ref.node_id:
        return Scope(canonical, ref, start_node_id=root_node_id)

    def children(parent_id: str) -> list[Node]:
        if parent_id not in children_cache:
            children_cache[parent_id] = client.list_nodes(parent_id)
        return children_cache[parent_id]

    parent_id = root_node_id
    for ancestor_id in ref.ancestors:
        ancestor = next((item for item in children(parent_id) if item.id == ancestor_id), None)
        if ancestor is None or not ancestor.is_folder:
            raise DingTalkError(f"DingTalk ancestor {ancestor_id!r} is unavailable")
        if ancestor.workspace_id and ancestor.workspace_id != ref.workspace_id:
            raise DingTalkError(f"DingTalk ancestor {ancestor_id!r} belongs to a different workspace")
        parent_id = ancestor.id
    selected = next((item for item in children(parent_id) if item.id == ref.node_id), None)
    if selected is None:
        raise DingTalkError(f"DingTalk node {ref.node_id!r} is unavailable")
    if selected.workspace_id and selected.workspace_id != ref.workspace_id:
        raise DingTalkError(f"DingTalk node {ref.node_id!r} belongs to a different workspace")
    if selected.is_folder:
        return Scope(canonical, ref, start_node_id=selected.id)
    if selected.is_online_document or (include_uploaded and selected.is_binary_document):
        return Scope(canonical, ref, document=selected)
    raise DingTalkError(f"DingTalk node {ref.node_id!r} is not a supported document or folder")


def _contains(scope: Scope, candidate: Scope) -> bool:
    if scope.reference.workspace_id != candidate.reference.workspace_id:
        return False
    if not scope.reference.node_id:
        return True
    if scope.document is not None:
        return scope.reference.node_id == candidate.reference.node_id
    if scope.reference.node_id == candidate.reference.node_id:
        return True
    return scope.reference.node_id in candidate.reference.ancestors


def _resolve_scopes(
    client: DingTalkClient, resource_ids: list[str], include_uploaded: bool = False
) -> tuple[list[Scope], dict[str, str]]:
    workspaces = {item.id: item for item in client.list_workspaces()}
    cache: dict[str, list[Node]] = {}
    scopes: list[Scope] = []
    failures: dict[str, str] = {}
    for resource_id in _unique(resource_ids):
        try:
            scopes.append(
                _resolve_scope(client, workspaces, resource_id, cache, include_uploaded)
            )
        except DingTalkError as exc:
            failures[resource_id] = str(exc)
    scopes.sort(key=lambda item: (len(item.reference.ancestors) + (1 if item.reference.node_id else 0), item.resource_id))
    compacted: list[Scope] = []
    for scope in scopes:
        if not any(_contains(parent, scope) for parent in compacted):
            compacted.append(scope)
    return compacted, failures


def _unique(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = _clean(value)
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _scan_scope(client: DingTalkClient, scope: Scope, include_uploaded: bool = False) -> list[Node]:
    if scope.document is not None:
        return [scope.document]
    queue = [scope.start_node_id]
    visited: set[str] = set()
    seen_nodes: set[str] = set()
    documents: list[Node] = []
    while queue:
        parent_id = queue.pop(0)
        if parent_id in visited:
            continue
        visited.add(parent_id)
        for child in client.list_nodes(parent_id):
            if not child.id or child.id in seen_nodes:
                continue
            if child.workspace_id and child.workspace_id != scope.reference.workspace_id:
                raise DingTalkError(f"DingTalk node {child.id!r} belongs to a different workspace")
            seen_nodes.add(child.id)
            if len(seen_nodes) > MAX_TRAVERSAL_NODES:
                raise DingTalkError(f"DingTalk workspace exceeds {MAX_TRAVERSAL_NODES} nodes")
            if child.is_online_document or (include_uploaded and child.is_binary_document):
                documents.append(child)
            if child.is_folder or child.has_children:
                queue.append(child.id)
    documents.sort(key=lambda item: item.id)
    return documents


def _fingerprint(title: str, url: str, content: str) -> str:
    payload = json.dumps([title, url, content], ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def _document_url(document: Node) -> str:
    return document.url or "https://alidocs.dingtalk.com/i/nodes/" + urllib.parse.quote(document.id, safe="")


def _read_document(client: DingTalkClient, scope: Scope, document: Node) -> dict[str, Any]:
    url = _document_url(document)
    metadata = {
        "workspace_id": scope.reference.workspace_id,
        "node_id": document.id,
        "category": document.category,
        "extension": document.extension,
    }
    if document.is_online_document:
        rendered = render_document(document.title, client.document_blocks(document.id))
        content = rendered.markdown
        if rendered.unknown_types:
            metadata["unknown_block_types"] = ",".join(rendered.unknown_types)
    else:
        extension = document.extension.lower()
        filename = f"{document.title}.{extension}" if extension else document.title
        data = client.download_document(document.id)
        extracted = chunking.normalise_text(chunking.extract_text(filename, data))
        if not extracted:
            raise DingTalkError(
                f"DingTalk uploaded file {document.title!r} has no extractable text"
            )
        content = f"# {_one_line(document.title)}\n\n{extracted}\n"
        metadata["content_type"] = document.content_type
        metadata["file_name"] = filename
    return {
        "external_id": document.id,
        "title": document.title,
        "content": content,
        "url": url,
        "updated_at": document.updated_at,
        "resource_id": scope.resource_id,
        "fingerprint": _fingerprint(document.title, url, content),
        "metadata": metadata,
    }


def fetch_items(config: dict[str, Any], previous_cursor: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    include_uploaded = _includes_uploaded_files(config)
    selected = _unique(config.get("resource_ids") or [])
    if not selected:
        raise DingTalkError("no DingTalk resources selected")
    client = DingTalkClient(config)
    scopes, failures = _resolve_scopes(client, selected, include_uploaded)
    items: list[dict[str, Any]] = []
    for resource_id, message in failures.items():
        items.append({
            "external_id": "dingtalk-resource:" + resource_id,
            "title": resource_id,
            "content": "",
            "resource_id": resource_id,
            "error": message,
        })

    seen_documents: set[str] = set()
    resources: dict[str, str] = {}
    complete = not failures
    for scope in scopes:
        try:
            documents = _scan_scope(client, scope, include_uploaded)
        except DingTalkError as exc:
            complete = False
            items.append({
                "external_id": "dingtalk-resource:" + scope.resource_id,
                "title": scope.resource_id,
                "content": "",
                "resource_id": scope.resource_id,
                "error": str(exc),
            })
            continue
        for document in documents:
            seen_documents.add(document.id)
            try:
                item = _read_document(client, scope, document)
            except DingTalkError as exc:
                complete = False
                items.append({
                    "external_id": document.id,
                    "title": document.title,
                    "content": "",
                    "resource_id": scope.resource_id,
                    "error": str(exc),
                })
                continue
            resources[document.id] = scope.resource_id
            items.append(item)

    connector_cursor = previous_cursor.get("connector_cursor") if isinstance(previous_cursor, dict) else None
    connector_cursor = connector_cursor if isinstance(connector_cursor, dict) else {}
    previous_resources = connector_cursor.get("resources")
    if complete and isinstance(previous_resources, dict):
        current_scope_ids = {scope.resource_id for scope in scopes}
        for external_id, resource_id in previous_resources.items():
            external_id = _clean(external_id)
            resource_id = _clean(resource_id)
            if external_id and external_id not in seen_documents and resource_id in current_scope_ids:
                items.append({
                    "external_id": external_id,
                    "title": external_id,
                    "content": "",
                    "resource_id": resource_id,
                    "is_deleted": True,
                })
    return items


def list_resources(config: dict[str, Any], parent_id: str = "") -> list[dict[str, Any]]:
    include_uploaded = _includes_uploaded_files(config)
    client = DingTalkClient(config)
    if not parent_id:
        result = []
        for workspace in client.list_workspaces():
            if not workspace.id:
                continue
            result.append({
                "external_id": _encode_ref(ResourceRef(workspace.id)),
                "name": workspace.name or workspace.id,
                "type": "wiki_space",
                "description": workspace.description,
                "url": workspace.url,
                "has_children": bool(workspace.root_node_id),
                "metadata": {"workspace_id": workspace.id},
            })
        return sorted(result, key=lambda item: (item["name"].lower(), item["external_id"]))

    workspaces = {item.id: item for item in client.list_workspaces()}
    cache: dict[str, list[Node]] = {}
    parent_ref = _decode_ref(parent_id)
    if parent_ref.node_id:
        scope = _resolve_scope(client, workspaces, parent_id, cache, include_uploaded)
        if scope.document is not None:
            return []
        parent_node_id = scope.start_node_id
    else:
        workspace = workspaces.get(parent_ref.workspace_id)
        if workspace is None:
            raise DingTalkError(f"DingTalk workspace {parent_ref.workspace_id!r} is unavailable")
        parent_node_id = workspace.root_node_id
        if not parent_node_id:
            return []

    result = []
    for child in client.list_nodes(parent_node_id):
        if not child.is_folder and not child.is_online_document and not (
            include_uploaded and child.is_binary_document
        ):
            continue
        if child.workspace_id and child.workspace_id != parent_ref.workspace_id:
            raise DingTalkError(f"DingTalk node {child.id!r} belongs to a different workspace")
        child_ref = _child_ref(parent_ref, child.id)
        result.append({
            "external_id": _encode_ref(child_ref),
            "name": child.title,
            "type": "folder" if child.is_folder else "document",
            "url": child.url,
            "parent_id": parent_id,
            "has_children": child.is_folder,
            "metadata": {
                "workspace_id": parent_ref.workspace_id,
                "node_id": child.id,
                "category": child.category,
                "extension": child.extension,
            },
        })
    return sorted(result, key=lambda item: (item["name"].lower(), item["external_id"]))


def validate(config: dict[str, Any]) -> dict[str, Any]:
    include_uploaded = _includes_uploaded_files(config)
    client = DingTalkClient(config)
    try:
        workspaces = client.list_workspaces()
    except DingTalkError as exc:
        raise DingTalkError(f"validate DingTalk data source: {exc}") from exc

    state: dict[str, Any] = {
        "probes": 0,
        "listings": 0,
        "saw_document": False,
        "last_error": None,
    }
    inconclusive = False
    workspace_failures = 0
    probe_cap_hit = False
    for index, workspace in enumerate(workspaces):
        root_node_id = workspace.root_node_id.strip()
        if not root_node_id:
            continue
        try:
            readable, unexplored, hit_cap = _sample_workspace(
                client, workspace.name, root_node_id, state, include_uploaded
            )
        except DingTalkError as exc:
            state["last_error"] = DingTalkError(f'workspace "{workspace.name}": {exc}')
            workspace_failures += 1
            continue
        if readable:
            return {"valid": True, "items": 1, "resources": len(workspaces)}
        if unexplored:
            inconclusive = True
        if int(state["probes"]) >= MAX_VALIDATE_PROBES:
            probe_cap_hit = hit_cap or index + 1 < len(workspaces)
            break

    if inconclusive:
        return {"valid": True, "items": 0, "resources": len(workspaces)}
    if not state["saw_document"] and workspace_failures == 0:
        return {"valid": True, "items": 0, "resources": len(workspaces)}
    last_error = state["last_error"]
    if last_error is None:
        return {"valid": True, "items": 0, "resources": len(workspaces)}
    if probe_cap_hit:
        raise DingTalkError(
            f"validate DingTalk data source: {last_error}; only the first "
            f"{MAX_VALIDATE_PROBES} documents were probed"
        )
    raise DingTalkError(f"validate DingTalk data source: {last_error}")


def _sample_workspace(
    client: DingTalkClient,
    workspace_name: str,
    root_node_id: str,
    state: dict[str, Any],
    include_uploaded: bool,
) -> tuple[bool, bool, bool]:
    queue = [root_node_id]
    visited = {root_node_id}
    unexplored = False
    uncovered_skip = False
    while queue:
        parent_id = queue.pop(0)
        subfolders: list[str] = []
        page_token = ""
        seen_tokens: set[str] = set()
        while True:
            if page_token and int(state["listings"]) + len(queue) >= MAX_VALIDATE_LISTINGS:
                if not queue:
                    unexplored = True
                else:
                    uncovered_skip = True
                break
            root_page = parent_id == root_node_id and not page_token
            if not root_page:
                if int(state["listings"]) >= MAX_VALIDATE_LISTINGS:
                    return False, True, False
                state["listings"] = int(state["listings"]) + 1
            try:
                children, next_token = client.list_nodes_page(parent_id, page_token)
            except DingTalkError:
                if parent_id == root_node_id:
                    raise
                unexplored = True
                break
            for child_index, child in enumerate(children):
                if child.is_online_document or (include_uploaded and child.is_binary_document):
                    state["probes"] = int(state["probes"]) + 1
                    state["saw_document"] = True
                    uncovered_skip = False
                    try:
                        if child.is_online_document:
                            client.document_blocks(child.id)
                        else:
                            client.verify_document_download(child.id)
                        return True, False, False
                    except DingTalkError as exc:
                        state["last_error"] = DingTalkError(
                            f'workspace "{workspace_name}" document "{child.name}": {exc}'
                        )
                        if int(state["probes"]) >= MAX_VALIDATE_PROBES:
                            more = (
                                child_index + 1 < len(children)
                                or bool(subfolders)
                                or bool(next_token)
                                or bool(queue)
                            )
                            return False, unexplored, more
                        continue
                if child.is_folder or child.has_children:
                    if child.id and child.id not in visited:
                        visited.add(child.id)
                        subfolders.append(child.id)
            if not next_token:
                break
            if next_token in seen_tokens:
                unexplored = True
                break
            seen_tokens.add(next_token)
            page_token = next_token
        queue.extend(subfolders)
    if uncovered_skip:
        unexplored = True
    return False, unexplored, False


def _escape_text(value: Any) -> str:
    text = str(value or "").replace("\x00", "")
    replacements = {"\\": "\\\\", "`": "\\`", "*": "\\*", "_": "\\_", "{": "\\{", "}": "\\}",
                    "[": "\\[", "]": "\\]", "<": "\\<", ">": "\\>", "#": "\\#", "+": "\\+",
                    "-": "\\-", "!": "\\!"}
    return "".join(replacements.get(char, char) for char in text)


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _escape_table_cell(value: str) -> str:
    return _escape_text(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def _one_line(value: Any) -> str:
    return _clean(str(value or "").replace("\r", " ").replace("\n", " "))


def _safe_url(value: Any) -> str:
    text = _clean(value)
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https", "mailto"}:
        return ""
    for old, new in (("\\", "%5C"), (" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        text = text.replace(old, new)
    return text


def _code_span(value: str) -> str:
    delimiter = "`"
    while delimiter in value:
        delimiter += "`"
    return delimiter + value + delimiter if len(delimiter) == 1 else delimiter + " " + value + " " + delimiter


def _style_text(text: Any, inline: dict[str, Any]) -> str:
    value = str(text or "").replace("\x00", "")
    if not value:
        return ""
    value = _code_span(value) if _clean(inline.get("fonts")).lower() == "monospace" else _escape_text(value)
    if inline.get("bold"):
        value = "**" + value + "**"
    if inline.get("italic"):
        value = "*" + value + "*"
    if inline.get("strike") or inline.get("stike"):
        value = "~~" + value + "~~"
    return value


def _render_inlines(children: Any, depth: int, unknown: set[str]) -> str:
    if depth > MAX_RENDER_DEPTH:
        unknown.add("inline_max_depth")
        return ""
    result: list[str] = []
    for raw in children if isinstance(children, list) else []:
        if not isinstance(raw, dict):
            unknown.add("invalid_inline_json")
            continue
        element = _clean(raw.get("elementType")).lower()
        properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        if element in {"", "text"}:
            result.append(_style_text(raw.get("text"), raw))
        elif element == "sticker":
            result.append(_escape_text(properties.get("code")))
        elif element == "image":
            src = _safe_url(properties.get("src"))
            if src:
                result.append(f"![image]({src})")
        elif element == "link":
            label = _render_inlines(raw.get("children"), depth + 1, unknown)
            href = _safe_url(properties.get("href"))
            if not href:
                result.append(label)
            elif not label:
                result.append(f"[{_escape_label(href)}]({href})")
            else:
                result.append(f"[{label}]({href})")
        else:
            unknown.add("inline_" + element)
            text = _escape_text(raw.get("text")) if _clean(raw.get("text")) else ""
            result.append(text or _render_inlines(raw.get("children"), depth + 1, unknown))
    return "".join(result)


def _flexible_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = _clean(value).lower().removeprefix("heading-").removeprefix("h")
    try:
        return int(text)
    except ValueError:
        return 0


def _list_text(value: dict[str, Any], block_type: str) -> str:
    payload = value.get("orderedList" if block_type == "orderedlist" else "unorderedList")
    return _clean(payload.get("text")) if isinstance(payload, dict) else ""


def _render_list(value: dict[str, Any], block_type: str, depth: int, min_level: int, unknown: set[str]) -> str:
    if depth > MAX_RENDER_DEPTH:
        unknown.add("max_depth")
        return ""
    payload = value.get("orderedList" if block_type == "orderedlist" else "unorderedList")
    payload = payload if isinstance(payload, dict) else {}
    nested = payload.get("list") if isinstance(payload.get("list"), dict) else {}
    level = max(min_level, _flexible_int(nested.get("level")))
    level = min(max(level, 0), MAX_RENDER_DEPTH)
    indent = "  " * level
    marker = "1. " if block_type == "orderedlist" else "- "
    lines: list[str] = []
    own_text = _list_text(value, block_type)
    if own_text:
        lines.append(_write_list_item(indent, marker, _escape_text(own_text)))
    pending: list[str] = []

    def flush() -> None:
        if pending:
            lines.append(_write_list_item(indent, marker, "".join(pending)))
            pending.clear()

    for child in value.get("children") if isinstance(value.get("children"), list) else []:
        if not isinstance(child, dict):
            pending.append(_render_inlines([child], depth + 1, unknown))
            continue
        child_type = _clean(child.get("blockType")).lower() if not _clean(child.get("elementType")) else ""
        if not child_type:
            if own_text:
                unknown.add("list_text_and_inline_children")
            else:
                pending.append(_render_inlines([child], depth + 1, unknown))
            continue
        flush()
        if child_type in {"orderedlist", "unorderedlist"}:
            lines.append(_render_list(child, child_type, depth + 1, level + 1, unknown))
        else:
            rendered = _render_block(child, depth + 1, unknown).strip()
            if rendered:
                lines.append(_write_list_item(indent, marker, rendered))
    flush()
    return "".join(lines)


def _write_list_item(indent: str, marker: str, text: str) -> str:
    lines = text.split("\n")
    result = [indent + marker + lines[0] + "\n"]
    continuation = " " * (len(indent) + len(marker))
    for line in lines[1:]:
        line = line.rstrip(" \t")
        if line:
            result.append(continuation + line + "\n")
    return "".join(result)


def _table_rows(raw: Any) -> list[list[str]]:
    if not isinstance(raw, list):
        return []
    result: list[list[str]] = []
    for row in raw:
        if not isinstance(row, list):
            continue
        result.append([_table_cell(cell) for cell in row])
    return result


def _table_cell(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, bool):
        return "true" if cell else "false"
    if isinstance(cell, (int, float)):
        return format(cell, "g")
    if isinstance(cell, dict):
        text = cell.get("text")
        if isinstance(text, str) and text.strip():
            return text
        children = cell.get("children")
        if isinstance(children, list):
            return "".join(_table_cell(child) for child in children)
    return ""


def _render_table(rows: list[list[str]]) -> str:
    columns = max((len(row) for row in rows), default=0)
    if not columns:
        return ""

    def row_line(row: list[str], escape: bool) -> str:
        values = []
        for index in range(columns):
            value = row[index] if index < len(row) else ""
            values.append(_escape_table_cell(value) if escape else value)
        return "| " + " | ".join(values) + " |\n"

    result = [row_line(rows[0], True), "| " + " | ".join(["---"] * columns) + " |\n"]
    result.extend(row_line(row, True) for row in rows[1:])
    return "".join(result) + "\n"


def _code_fence_length(text: str) -> int:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return 3 if longest < 2 else longest + 1


def _human_size(size: int) -> str:
    if size <= 0:
        return ""
    if size < 1024:
        return f"{size:.1f} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def _render_block(value: dict[str, Any], depth: int, unknown: set[str]) -> str:
    if depth > MAX_RENDER_DEPTH:
        unknown.add("max_depth")
        return ""
    block_type = _clean(value.get("blockType")).lower()
    if not block_type:
        unknown.add("missing_block_type")
        return ""
    children = value.get("children") if isinstance(value.get("children"), list) else []
    if block_type == "paragraph":
        payload = value.get("paragraph") if isinstance(value.get("paragraph"), dict) else {}
        text = _render_inlines(children, depth + 1, unknown) or _escape_text(payload.get("text"))
        return text + "\n\n" if text else ""
    if block_type == "heading":
        payload = value.get("heading") if isinstance(value.get("heading"), dict) else {}
        text = _render_inlines(children, depth + 1, unknown) or _escape_text(payload.get("text"))
        if not text:
            return ""
        level = min(max(_flexible_int(payload.get("level")), 1), 6)
        return "#" * level + " " + text + "\n\n"
    if block_type == "blockquote":
        payload = value.get("blockquote") if isinstance(value.get("blockquote"), dict) else {}
        text = _render_inlines(children, depth + 1, unknown) or _escape_text(payload.get("text"))
        if not text:
            return ""
        return "".join("> " + line + "\n" for line in text.split("\n")) + "\n"
    if block_type in {"orderedlist", "unorderedlist"}:
        return _render_list(value, block_type, depth, 0, unknown)
    if block_type in {"callout", "columns"}:
        if not children:
            unknown.add("nested_blocks_unavailable")
            return ""
        return "".join(_render_block(child, depth + 1, unknown) for child in children if isinstance(child, dict))
    if block_type == "table":
        payload = value.get("table") if isinstance(value.get("table"), dict) else {}
        return _render_table(_table_rows(payload.get("cells")))
    if block_type == "code":
        payload = value.get("code") if isinstance(value.get("code"), dict) else {}
        text = str(payload.get("text") or "")
        if not text.strip():
            unknown.add("code")
            return "".join(_render_block(child, depth + 1, unknown) for child in children if isinstance(child, dict))
        result = ""
        title = _one_line(payload.get("title"))
        if title:
            result += f"**{_escape_text(title)}**\n\n"
        fence = "`" * _code_fence_length(text)
        syntax = _clean(payload.get("syntax"))
        info = "" if not syntax or any(char in syntax for char in "`\r\n") else syntax
        return result + fence + info + "\n" + text + ("" if text.endswith("\n") else "\n") + fence + "\n\n"
    if block_type == "attachment":
        payload = value.get("attachment") if isinstance(value.get("attachment"), dict) else {}
        name = _one_line(payload.get("name"))
        if not name:
            unknown.add("attachment")
            return "".join(_render_block(child, depth + 1, unknown) for child in children if isinstance(child, dict))
        line = "Attachment: " + _escape_text(name)
        size = _human_size(int(payload.get("size") or 0))
        if size:
            line += " (" + size + ")"
        return line + "\n\n"
    marker = block_type
    payload = value.get("unknown") if isinstance(value.get("unknown"), dict) else {}
    raw_type = _clean(payload.get("rawType"))
    if raw_type:
        marker = "unknown:" + raw_type
    unknown.add(marker)
    result = ""
    text = _clean(payload.get("text"))
    if text:
        result += _escape_text(text) + "\n\n"
    result += "".join(_render_block(child, depth + 1, unknown) for child in children if isinstance(child, dict))
    return result


def render_document(title: str, blocks: list[Any]) -> RenderResult:
    unknown: set[str] = set()
    result = ""
    clean_title = _one_line(title)
    if clean_title:
        result += "# " + _escape_text(clean_title) + "\n\n"
    for block in blocks:
        if not isinstance(block, dict):
            unknown.add("invalid_json")
            continue
        result += _render_block(block, 0, unknown)
    markdown = result.strip()
    if markdown:
        markdown += "\n"
    return RenderResult(markdown, sorted(unknown))

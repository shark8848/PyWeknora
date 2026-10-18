"""Public, token-authenticated MCP endpoint (Streamable HTTP, stateless)."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import threading
import time
import urllib.parse
from collections import deque
from types import SimpleNamespace
from typing import Any, Callable
from xml.sax.saxutils import escape as xml_escape

import anyio.to_thread
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import db
from ..api_keys import hash_token
from ..services import agents as agent_service
from ..services import model_runtime
from ..services import url_guard
from ..services.access import knowledge_base_share_permission, readable_knowledge_base, shared_knowledge_base_ids
from ..services.chunking import extract_text
from ..services.retrieval import retrieve
from ..services.wiki_ingest import wiki_enabled
from ..utils import dumps, loads
from .chat import _create_message, _update_message, stream_chat_events
from .knowledge import _create_knowledge, _insert_chunks, _refresh_knowledge_indexes


router = APIRouter(include_in_schema=False)

SERVER_NAME = "weknora"
SERVER_VERSION = "1.0.0"
MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOLS = (
    MODERN_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
INSTRUCTIONS = (
    "WeKnora knowledge workspace. Start with list_knowledge_bases to see what is in scope, then use "
    "search_knowledge for semantic questions, grep_chunks for exact keywords, read_document to read a "
    "whole document, and ask to get a synthesized answer with citations. Wiki tools browse the generated "
    "wiki when a knowledge base has one. Write tools (add/update/delete_document) exist only on endpoints "
    "that enabled them."
)

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}
ASK_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
MODERN_REMOVED_METHODS = {
    "initialize",
    "ping",
    "logging/setLevel",
    "resources/subscribe",
    "resources/unsubscribe",
}
MODERN_ERROR_HTTP_STATUS = {
    -32700: 400,
    -32600: 400,
    -32602: 400,
    -32601: 404,
}

MCP_ASK_TIMEOUT_SECONDS = 240

_rate_lock = threading.Lock()
_rate_windows: dict[str, deque[float]] = {}
_touch_lock = threading.Lock()
_last_touch: dict[str, float] = {}


class _AuthFailure(Exception):
    def __init__(self, status: int, message: str, *, authenticate: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.authenticate = authenticate


class _ToolError(Exception):
    pass


class _RPCRejected(Exception):
    def __init__(self, response: dict[str, Any] | None, status_code: int) -> None:
        super().__init__()
        self.response = response
        self.status_code = status_code


def _bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization") or "").strip()
    if len(header) > 7 and header[:7].lower() == "bearer ":
        return header[7:].strip()
    return ""


def _authenticate(endpoint_id: str, request: Request) -> dict[str, Any]:
    token = _bearer_token(request)
    if not token:
        raise _AuthFailure(401, "unauthorized: missing bearer token", authenticate=True)
    try:
        endpoint = db.fetch_one(
            "SELECT * FROM mcp_endpoints WHERE id=? AND deleted_at IS NULL",
            (endpoint_id,),
        )
    except Exception as exc:  # noqa: BLE001 - auth lookups must not expose internals
        raise _AuthFailure(500, "authentication unavailable") from exc
    expected = str((endpoint or {}).get("token_hash") or "")
    if not endpoint or not expected or not hmac.compare_digest(expected, hash_token(token)):
        raise _AuthFailure(401, "unauthorized: invalid token", authenticate=True)
    if not bool(endpoint.get("enabled")):
        raise _AuthFailure(403, "mcp endpoint disabled")
    tenant = db.fetch_one(
        "SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL",
        (int(endpoint.get("tenant_id") or 0),),
    )
    if not tenant:
        raise _AuthFailure(500, "workspace unavailable")
    if str(tenant.get("status") or "").lower() in {"inactive", "disabled"}:
        raise _AuthFailure(403, "workspace inactive")
    return endpoint


def _auth_response(exc: _AuthFailure) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Bearer realm="weknora-mcp"'} if exc.authenticate else {}
    return JSONResponse({"error": exc.message}, status_code=exc.status, headers=headers)


def _allow_call(endpoint: dict[str, Any]) -> bool:
    endpoint_id = str(endpoint["id"])
    rate = int(endpoint.get("rate_limit_per_minute") or 60)
    if rate <= 0:
        rate = 60
    now = time.monotonic()
    with _rate_lock:
        window = _rate_windows.setdefault(endpoint_id, deque())
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= rate:
            return False
        window.append(now)
    return True


def _touch_last_used(endpoint_id: str) -> None:
    now = time.monotonic()
    with _touch_lock:
        if now - _last_touch.get(endpoint_id, 0.0) < 60:
            return
        _last_touch[endpoint_id] = now
    try:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE mcp_endpoints SET last_used_at=? WHERE id=?",
                (db.utcnow(), endpoint_id),
            )
    except Exception:  # noqa: BLE001 - activity tracking must not fail a tool call
        pass


def _tool_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def _array_strings(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


_TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "list_knowledge_bases": {
        "description": (
            "List the knowledge bases this endpoint can access, including each base's id, name, "
            "description and retrieval capabilities. Call this first to learn what is in scope."
        ),
        "inputSchema": _tool_schema({}),
        "annotations": READ_ONLY,
    },
    "search_knowledge": {
        "description": (
            "Search the knowledge bases in scope and return relevant passages with document ids and "
            "titles. mode hybrid combines semantic and keyword retrieval; keyword matches exact terms."
        ),
        "inputSchema": _tool_schema(
            {
                "query": {"type": "string", "description": "Question, topic or exact terms to match"},
                "mode": {"type": "string", "enum": ["hybrid", "semantic", "keyword"]},
                "limit": {"type": "number", "description": "Maximum passages, default 10, max 30"},
                "knowledge_base_ids": _array_strings("Optional knowledge base ids or exact names"),
            },
            ["query"],
        ),
        "annotations": READ_ONLY,
    },
    "grep_chunks": {
        "description": (
            "Case-insensitive regular-expression search over text chunks. Use for exact identifiers, "
            "error codes and product names; invalid regular expressions are matched literally."
        ),
        "inputSchema": _tool_schema(
            {
                "query": {"type": "string", "description": "Regular expression or literal text"},
                "limit": {"type": "number", "description": "Maximum passages, default 10, max 30"},
                "knowledge_base_ids": _array_strings("Optional knowledge base ids or exact names"),
            },
            ["query"],
        ),
        "annotations": READ_ONLY,
    },
    "list_documents": {
        "description": "List documents in one knowledge base with pagination and optional title filtering.",
        "inputSchema": _tool_schema(
            {
                "knowledge_base_id": {"type": "string", "description": "Knowledge base id or exact name"},
                "keyword": {"type": "string", "description": "Optional title substring"},
                "page": {"type": "number", "description": "1-based page number"},
                "page_size": {"type": "number", "description": "Documents per page, default 20, max 100"},
            },
            ["knowledge_base_id"],
        ),
        "annotations": READ_ONLY,
    },
    "read_document": {
        "description": (
            "Read a document's metadata and text chunks in order. Page with offset and limit, or pass "
            "query to return matching chunks with one chunk of context on each side."
        ),
        "inputSchema": _tool_schema(
            {
                "knowledge_id": {"type": "string", "description": "Document id"},
                "offset": {"type": "number", "description": "Chunk offset, default 0"},
                "limit": {"type": "number", "description": "Chunks to return, default 20, max 100"},
                "query": {"type": "string", "description": "Optional case-insensitive phrase"},
            },
            ["knowledge_id"],
        ),
        "annotations": READ_ONLY,
    },
    "ask": {
        "description": (
            "Ask the workspace a question and get a synthesized answer with citations. WeKnora runs the "
            "agent configured on this endpoint; agent-mode runs may take a few minutes. Pass the returned "
            "session_id on follow-up questions to continue the conversation."
        ),
        "inputSchema": _tool_schema(
            {
                "question": {"type": "string", "description": "The question to answer"},
                "session_id": {"type": "string", "description": "Previous ask session id"},
                "knowledge_base_ids": _array_strings("Optional knowledge base ids or exact names"),
            },
            ["question"],
        ),
        "annotations": ASK_ANNOTATIONS,
    },
    "wiki_search": {
        "description": "Search generated wiki pages by title, aliases and content.",
        "inputSchema": _tool_schema(
            {
                "query": {"type": "string", "description": "Search terms or regular expression"},
                "regex": {"type": "boolean", "description": "Require or disable regular-expression matching"},
                "knowledge_base_ids": _array_strings("Optional knowledge base ids or exact names"),
                "limit": {"type": "number", "description": "Maximum pages, default 10, max 50"},
            },
            ["query"],
        ),
        "annotations": READ_ONLY,
    },
    "wiki_read_page": {
        "description": "Read one wiki page by slug, including content and outgoing links.",
        "inputSchema": _tool_schema(
            {
                "slug": {"type": "string", "description": "Page slug"},
                "knowledge_base_id": {"type": "string", "description": "Optional knowledge base id or name"},
            },
            ["slug"],
        ),
        "annotations": READ_ONLY,
    },
    "wiki_index": {
        "description": "Browse the table of contents of one knowledge base's wiki.",
        "inputSchema": _tool_schema(
            {
                "knowledge_base_id": {"type": "string", "description": "Knowledge base id or exact name"},
                "limit": {"type": "number", "description": "Entries per page type, default 50, max 200"},
            },
            ["knowledge_base_id"],
        ),
        "annotations": READ_ONLY,
    },
    "add_document": {
        "description": "Add a document from Markdown text or from a URL.",
        "inputSchema": _tool_schema(
            {
                "knowledge_base_id": {"type": "string", "description": "Knowledge base id or exact name"},
                "title": {"type": "string", "description": "Document title"},
                "content": {"type": "string", "description": "Markdown content"},
                "url": {"type": "string", "description": "URL to import instead of content"},
                "publish": {"type": "boolean", "description": "Publish immediately, default true"},
            },
            ["knowledge_base_id", "title"],
        ),
        "annotations": DESTRUCTIVE,
    },
    "update_document": {
        "description": "Replace the content and optionally the title of a Markdown document.",
        "inputSchema": _tool_schema(
            {
                "knowledge_id": {"type": "string", "description": "Document id"},
                "content": {"type": "string", "description": "New Markdown content"},
                "title": {"type": "string", "description": "Optional new title"},
                "publish": {"type": "boolean", "description": "Publish immediately, default true"},
            },
            ["knowledge_id", "content"],
        ),
        "annotations": DESTRUCTIVE,
    },
    "delete_document": {
        "description": "Permanently delete a document and its index data.",
        "inputSchema": _tool_schema(
            {"knowledge_id": {"type": "string", "description": "Document id"}},
            ["knowledge_id"],
        ),
        "annotations": DESTRUCTIVE,
    },
}


def _tool_definitions(endpoint: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = set(loads(endpoint.get("tools"), []))
    result = []
    for name in sorted(_TOOL_DEFINITIONS):
        if name not in allowed:
            continue
        definition = dict(_TOOL_DEFINITIONS[name])
        definition["name"] = name
        result.append(definition)
    return result


def _text_result(text: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if data is not None:
        result["structuredContent"] = data
    return result


def _json_result(data: dict[str, Any]) -> dict[str, Any]:
    return _text_result(json.dumps(data, ensure_ascii=False, indent=2), data)


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _xml_attr(value: Any) -> str:
    return xml_escape(str(value), {'"': "&quot;"})


def _string_arg(args: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = str(args.get(key) or "").strip()
    if required and not value:
        raise _ToolError(f"{key} is required")
    return value


def _raw_string_arg(args: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = str(args.get(key) or "")
    if required and not value.strip():
        raise _ToolError(f"{key} is required")
    return value


def _int_arg(args: dict[str, Any], key: str, default: int, minimum: int, maximum: int) -> int:
    value = args.get(key)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _selectors(args: dict[str, Any]) -> list[str]:
    value = args.get("knowledge_base_ids")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _allowed_knowledge_bases(endpoint: dict[str, Any]) -> list[dict[str, Any]]:
    tenant_id = int(endpoint.get("tenant_id") or 0)
    pinned = [str(value) for value in loads(endpoint.get("knowledge_base_ids"), []) if value]
    if pinned:
        result = []
        for kb_id in pinned:
            kb = readable_knowledge_base(tenant_id, kb_id, "viewer")
            if kb:
                result.append(kb)
        return result
    shared = sorted(shared_knowledge_base_ids(tenant_id, "viewer"))
    clauses = ["deleted_at IS NULL", "tenant_id=?"]
    params: list[Any] = [tenant_id]
    if shared:
        clauses = ["deleted_at IS NULL", "(tenant_id=? OR id IN (" + ",".join("?" for _ in shared) + "))"]
        params.extend(shared)
    return db.fetch_all(
        "SELECT * FROM knowledge_bases WHERE " + " AND ".join(clauses)
        + " ORDER BY is_pinned DESC, updated_at DESC",
        params,
    )


def _match_knowledge_base(knowledge_bases: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    selector = selector.strip()
    for kb in knowledge_bases:
        if str(kb.get("id")) == selector:
            return kb
    lowered = selector.casefold()
    for kb in knowledge_bases:
        if str(kb.get("name") or "").strip().casefold() == lowered:
            return kb
    return None


def _select_knowledge_bases(
    endpoint: dict[str, Any], selectors: list[str]
) -> list[dict[str, Any]]:
    allowed = _allowed_knowledge_bases(endpoint)
    if not allowed:
        raise _ToolError("no knowledge base is available on this endpoint")
    if not selectors:
        return allowed
    result = []
    seen: set[str] = set()
    for selector in selectors:
        kb = _match_knowledge_base(allowed, selector)
        if kb is None:
            raise _ToolError(
                f"knowledge base {selector!r} was not found or is outside this endpoint's scope; "
                "call list_knowledge_bases to see what is available"
            )
        kb_id = str(kb["id"])
        if kb_id not in seen:
            seen.add(kb_id)
            result.append(kb)
    return result


def _kb_capabilities(kb: dict[str, Any]) -> list[str]:
    strategy = loads(kb.get("indexing_strategy"), {})
    if not isinstance(strategy, dict):
        strategy = {}
    capabilities = []
    if bool(strategy.get("vector_enabled", True)):
        capabilities.append("semantic")
    if bool(strategy.get("keyword_enabled", True)):
        capabilities.append("keyword")
    if wiki_enabled(kb):
        capabilities.append("wiki")
    return capabilities


def _document_summary(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": row.get("id"),
        "title": row.get("title") or "",
        "parse_status": row.get("parse_status") or "",
        "created_at": row.get("created_at") or "",
        "updated_at": row.get("updated_at") or "",
    }
    for key in ("file_name", "file_type", "source", "description"):
        if row.get(key):
            result[key] = row[key]
    return result


def _tool_list_knowledge_bases(endpoint: dict[str, Any], _args: dict[str, Any]) -> dict[str, Any]:
    result = []
    for kb in _allowed_knowledge_bases(endpoint):
        item = {
            "id": kb["id"],
            "name": kb.get("name") or "",
            "type": kb.get("type") or "document",
            "capabilities": _kb_capabilities(kb),
        }
        if kb.get("description"):
            item["description"] = kb["description"]
        result.append(item)
    return _json_result({"knowledge_bases": result, "total": len(result)})


def _search_rows(
    endpoint: dict[str, Any],
    knowledge_bases: list[dict[str, Any]],
    query: str,
    mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    kb_ids = [str(kb["id"]) for kb in knowledge_bases]
    rows = retrieve(
        int(endpoint.get("tenant_id") or 0),
        query,
        knowledge_base_ids=kb_ids,
        limit=max(limit, 30),
        embedding_config=model_runtime.embedding_runtime(
            int(endpoint.get("tenant_id") or 0), kb_ids
        ),
    )
    if mode == "keyword":
        from ..services.retrieval import keyword_score

        rows = [row for row in rows if keyword_score(query, str(row.get("content") or "")) > 0]
        rows.sort(key=lambda row: keyword_score(query, str(row.get("content") or "")), reverse=True)
    return rows[:limit]


def _search_data(
    query: str,
    mode: str,
    knowledge_bases: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    formatted = []
    for index, row in enumerate(rows, start=1):
        content = str(row.get("content") or "")
        formatted.append(
            {
                "result_index": index,
                "content": content,
                "knowledge_id": row.get("knowledge_id") or "",
                "knowledge_base_id": row.get("knowledge_base_id") or "",
                "knowledge_title": row.get("title") or row.get("file_name") or "",
                "knowledge_base_type": row.get("knowledge_type") or "document",
                "match_type": mode,
                "match_snippet": content[:240],
                "score": row.get("score", 0),
                "source_query": query,
                "query_type": mode,
                "chunk_id": row.get("id") or "",
                "chunk_index": row.get("chunk_index", 0),
            }
        )
    return {
        "knowledge_base_ids": [str(kb["id"]) for kb in knowledge_bases],
        "query": query,
        "queries": [query],
        "mode": mode,
        "results": formatted,
        "count": len(formatted),
        "display_type": "search_results",
    }


def _search_text(data: dict[str, Any]) -> str:
    rows = data["results"]
    lines = [
        f'<search_results count="{len(rows)}" mode="{_xml_attr(data["mode"])}">',
        f'<query>{xml_escape(data["query"])}</query>',
    ]
    for row in rows:
        lines.append(
            f'<chunk rank="{row["result_index"]}" chunk_id="{_xml_attr(row["chunk_id"])}" '
            f'knowledge_id="{_xml_attr(row["knowledge_id"])}" '
            f'knowledge_base_id="{_xml_attr(row["knowledge_base_id"])}" '
            f'knowledge_title="{_xml_attr(row["knowledge_title"])}">'
        )
        lines.append(f'<content>{xml_escape(row["content"])}</content>')
        lines.append("</chunk>")
    lines.append("</search_results>")
    return "\n".join(lines)


def _tool_search_knowledge(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    query = _string_arg(args, "query", required=True)
    mode = _string_arg(args, "mode").lower() or "hybrid"
    if mode not in {"hybrid", "semantic", "keyword"}:
        raise _ToolError("mode must be one of hybrid, semantic, keyword")
    limit = _int_arg(args, "limit", 10, 1, 30)
    knowledge_bases = _select_knowledge_bases(endpoint, _selectors(args))
    searchable = [
        kb for kb in knowledge_bases if {"semantic", "keyword"}.intersection(_kb_capabilities(kb))
    ]
    if not searchable:
        raise _ToolError("none of the selected knowledge bases supports semantic or keyword retrieval")
    rows = _search_rows(endpoint, searchable, query, mode, limit)
    data = _search_data(query, mode, searchable, rows)
    text = _search_text(data) if rows else (
        f"No matching chunks for {json.dumps(query)} (mode={mode}) in {len(searchable)} knowledge base(s)."
    )
    return _text_result(text, data)


def _grep_pattern(query: str) -> tuple[re.Pattern[str], str]:
    try:
        pattern = re.compile("(?i)" + query)
        source = re.sub(r"\\[A-Za-z]", " ", query)
    except re.error:
        pattern = re.compile("(?i)" + re.escape(query))
        source = query
    terms = re.findall(r"[\w\u3400-\u9fff-]+", source)
    deduped = list(dict.fromkeys(term for term in terms if term))
    return pattern, " ".join(deduped)


def _tool_grep_chunks(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    query = _string_arg(args, "query", required=True)
    limit = _int_arg(args, "limit", 10, 1, 30)
    knowledge_bases = _select_knowledge_bases(endpoint, _selectors(args))
    searchable = [
        kb for kb in knowledge_bases if {"semantic", "keyword"}.intersection(_kb_capabilities(kb))
    ]
    if not searchable:
        raise _ToolError("none of the selected knowledge bases has a chunk index (vector or keyword)")
    pattern, terms = _grep_pattern(query)
    if not terms:
        raise _ToolError(
            "the pattern contains no literal text to look up; include at least one word, "
            "identifier or phrase"
        )
    rows = _search_rows(endpoint, searchable, terms, "keyword", 30)
    rows = [row for row in rows if pattern.search(str(row.get("content") or ""))][:limit]
    data = _search_data(query, "keyword", searchable, rows)
    text = _search_text(data) if rows else f"No matching chunks for {json.dumps(query)}."
    return _text_result(text, data)


def _tool_list_documents(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    selector = _string_arg(args, "knowledge_base_id", required=True)
    kb = _select_knowledge_bases(endpoint, [selector])[0]
    page = _int_arg(args, "page", 1, 1, 1_000_000)
    page_size = _int_arg(args, "page_size", 20, 1, 100)
    clauses = ["knowledge_base_id=?", "tenant_id=?", "deleted_at IS NULL"]
    params: list[Any] = [kb["id"], int(kb["tenant_id"])]
    keyword = _string_arg(args, "keyword")
    if keyword:
        clauses.append("(lower(title) LIKE ? OR lower(COALESCE(file_name,'')) LIKE ?)")
        needle = f"%{keyword.lower()}%"
        params.extend([needle, needle])
    where = " AND ".join(clauses)
    total = int(db.fetch_one(f"SELECT COUNT(*) AS n FROM knowledges WHERE {where}", params)["n"])
    rows = db.fetch_all(
        f"SELECT * FROM knowledges WHERE {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    )
    return _json_result(
        {
            "knowledge_base_id": kb["id"],
            "total": total,
            "page": page,
            "page_size": page_size,
            "documents": [_document_summary(row) for row in rows],
        }
    )


def _document_in_scope(
    endpoint: dict[str, Any], knowledge_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    knowledge_id = knowledge_id.strip()
    row = db.fetch_one(
        "SELECT * FROM knowledges WHERE id=? AND deleted_at IS NULL",
        (knowledge_id,),
    )
    if not row:
        raise _ToolError(f"document {knowledge_id!r} was not found")
    allowed = _allowed_knowledge_bases(endpoint)
    kb = _match_knowledge_base(allowed, str(row.get("knowledge_base_id") or ""))
    if kb is None or int(kb.get("tenant_id") or 0) != int(row.get("tenant_id") or 0):
        raise _ToolError(f"document {knowledge_id!r} is outside this endpoint's scope")
    return row, kb


def _tool_read_document(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    knowledge_id = _string_arg(args, "knowledge_id", required=True)
    knowledge, kb = _document_in_scope(endpoint, knowledge_id)
    limit = _int_arg(args, "limit", 20, 1, 100)
    offset = _int_arg(args, "offset", 0, 0, 1_000_000)
    query = _string_arg(args, "query")
    rows = db.fetch_all(
        "SELECT * FROM chunks WHERE knowledge_id=? AND tenant_id=? AND deleted_at IS NULL "
        "AND is_enabled=1 AND chunk_type IN ('text', 'faq') ORDER BY chunk_index",
        (knowledge_id, int(knowledge["tenant_id"])),
    )
    total = len(rows)
    selected: list[tuple[dict[str, Any], str]] = []
    if query:
        needle = query.casefold()
        indexes: set[int] = set()
        for index, row in enumerate(rows):
            if needle in str(row.get("content") or "").casefold():
                indexes.update({max(0, index - 1), index, min(len(rows) - 1, index + 1)})
        ordered = sorted(indexes)[offset:]
        for index in ordered[:limit]:
            row = rows[index]
            role = "match" if needle in str(row.get("content") or "").casefold() else "context"
            selected.append((row, role))
    else:
        selected = [(row, "") for row in rows[offset:offset + limit]]
    chunks = []
    source_chunk_id = ""
    for index, (row, role) in enumerate(selected, start=offset + 1):
        if not source_chunk_id and loads(row.get("source_locators"), []):
            source_chunk_id = str(row.get("id") or "")
        item = {
            "seq": index,
            "chunk_id": row.get("id") or "",
            "chunk_index": row.get("chunk_index", 0),
            "content": row.get("content") or "",
            "chunk_type": row.get("chunk_type") or "text",
            "knowledge_id": knowledge_id,
            "knowledge_base": kb["id"],
            "start_at": row.get("start_at", 0),
            "end_at": row.get("end_at", 0),
            "parent_chunk_id": row.get("parent_chunk_id") or "",
        }
        if role:
            item["role"] = role
        chunks.append(item)
    data = {
        "display_type": "knowledge_chunks_list",
        "knowledge_id": knowledge_id,
        "knowledge_base_id": kb["id"],
        "knowledge_title": knowledge.get("title") or "",
        "chunk_ids": [chunk["chunk_id"] for chunk in chunks],
        "source_chunk_id": source_chunk_id,
        "total_chunks": total,
        "fetched_chunks": len(chunks),
        "chunks": chunks,
        "document": _document_summary(knowledge),
        "knowledge_base": {"id": kb["id"], "name": kb.get("name") or ""},
    }
    lines = [
        f'<document knowledge_id="{_xml_attr(knowledge_id)}" '
        f'title="{_xml_attr(knowledge.get("title") or "")}" '
        f'total_chunks="{total}" fetched="{len(chunks)}">'
    ]
    for chunk in chunks:
        lines.append(
            f'<chunk chunk_id="{_xml_attr(chunk["chunk_id"])}" '
            f'chunk_index="{chunk["chunk_index"]}" type="{_xml_attr(chunk["chunk_type"])}">'
        )
        lines.append(f'<content>{xml_escape(chunk["content"])}</content>')
        lines.append("</chunk>")
    lines.append("</document>")
    return _text_result("\n".join(lines), data)


def _wiki_rows(knowledge_bases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for kb in knowledge_bases:
        rows = db.fetch_all(
            "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND deleted_at IS NULL "
            "ORDER BY page_type, sort_order, title",
            (kb["id"],),
        )
        for row in rows:
            row["_kb_name"] = kb.get("name") or ""
            result.append(row)
    return result


def _wiki_page_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "knowledge_base_id": row.get("knowledge_base_id") or "",
        "knowledge_base_name": row.get("_kb_name") or "",
        "slug": row.get("slug") or "",
        "title": row.get("title") or "",
        "page_type": row.get("page_type") or "",
        "summary": row.get("summary") or "",
    }


def _wiki_pattern(query: str, regex: bool | None) -> re.Pattern[str]:
    if regex is False:
        return re.compile(re.escape(query), re.IGNORECASE)
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error as exc:
        if regex is True:
            raise _ToolError(f"invalid regular expression: {exc}") from exc
        return re.compile(re.escape(query), re.IGNORECASE)


def _tool_wiki_search(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    query = _string_arg(args, "query", required=True)
    limit = _int_arg(args, "limit", 10, 1, 50)
    regex = args.get("regex") if isinstance(args.get("regex"), bool) else None
    pattern = _wiki_pattern(query, regex)
    knowledge_bases = _select_knowledge_bases(endpoint, _selectors(args))
    wiki_bases = [kb for kb in knowledge_bases if "wiki" in _kb_capabilities(kb)]
    if not wiki_bases:
        raise _ToolError(
            "none of the selected knowledge bases has a wiki; call list_knowledge_bases and pick "
            'one whose capabilities include "wiki"'
        )
    scored = []
    for row in _wiki_rows(wiki_bases):
        aliases = " ".join(str(value) for value in loads(row.get("aliases"), []))
        title = str(row.get("title") or "")
        slug = str(row.get("slug") or "")
        summary = str(row.get("summary") or "")
        content = str(row.get("content") or "")
        rank = 0
        if pattern.search(title) or pattern.search(aliases) or pattern.search(slug):
            rank = 2
        elif pattern.search(summary):
            rank = 1
        elif pattern.search(content):
            rank = 0
        else:
            continue
        scored.append((rank, str(row.get("updated_at") or ""), row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    pages = [_wiki_page_summary(row) for _, _, row in scored[:limit]]
    data = {"query": query, "results": pages, "count": len(pages)}
    text = "\n".join(
        f'<wiki_page slug="{_xml_attr(page["slug"])}" title="{_xml_attr(page["title"])}" '
        f'knowledge_base_id="{_xml_attr(page["knowledge_base_id"])}">{xml_escape(page["summary"])}'
        "</wiki_page>"
        for page in pages
    )
    return _text_result(text or f"No wiki pages matched {json.dumps(query)}.", data)


def _tool_wiki_read_page(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    slug = _string_arg(args, "slug", required=True)
    selector = _string_arg(args, "knowledge_base_id")
    knowledge_bases = _select_knowledge_bases(endpoint, [selector] if selector else [])
    wiki_bases = [kb for kb in knowledge_bases if "wiki" in _kb_capabilities(kb)]
    if not wiki_bases:
        raise _ToolError("none of the selected knowledge bases has a wiki")
    matches = [row for row in _wiki_rows(wiki_bases) if str(row.get("slug") or "") == slug]
    if not matches:
        raise _ToolError(f"wiki page {slug!r} was not found in scope")
    if len(matches) > 1 and not selector:
        options = ", ".join(
            f"{row.get('knowledge_base_id')}:{row.get('slug')}" for row in matches
        )
        raise _ToolError(
            f"wiki page {slug!r} exists in multiple knowledge bases ({options}); "
            "pass knowledge_base_id to choose one"
        )
    row = matches[0]
    page = _wiki_page_summary(row)
    page.update(
        {
            "content": row.get("content") or "",
            "out_links": loads(row.get("out_links"), []),
            "in_links": loads(row.get("in_links"), []),
            "source_refs": loads(row.get("source_refs"), []),
            "version": int(row.get("version") or 1),
        }
    )
    text = (
        f'<wiki_page slug="{_xml_attr(page["slug"])}" title="{_xml_attr(page["title"])}" '
        f'knowledge_base_id="{_xml_attr(page["knowledge_base_id"])}">\n'
        f'<content>{xml_escape(page["content"])}</content>\n</wiki_page>'
    )
    return _text_result(text, page)


def _tool_wiki_index(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    selector = _string_arg(args, "knowledge_base_id", required=True)
    kb = _select_knowledge_bases(endpoint, [selector])[0]
    if "wiki" not in _kb_capabilities(kb):
        raise _ToolError(f"knowledge base {selector!r} does not have a wiki")
    limit = _int_arg(args, "limit", 50, 1, 200)
    rows = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND deleted_at IS NULL "
        "ORDER BY page_type, sort_order, title",
        (kb["id"],),
    )
    intro_row = next((row for row in rows if row.get("slug") in {"index", "wiki-index"}), None)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row.get("page_type") or "summary"), []).append(
            {
                "slug": row.get("slug") or "",
                "title": row.get("title") or "",
                "summary": row.get("summary") or "",
                "parent_slug": row.get("parent_slug") or "",
                "wiki_path": row.get("wiki_path") or "",
                "depth": int(row.get("depth") or 0),
                "sort_order": int(row.get("sort_order") or 0),
            }
        )
    data = {
        "knowledge_base_id": kb["id"],
        "intro": (intro_row or {}).get("content") or "",
        "groups": [
            {"type": page_type, "total": len(items), "items": items[:limit]}
            for page_type, items in groups.items()
        ],
    }
    return _json_result(data)


def _require_write(endpoint: dict[str, Any], kb: dict[str, Any]) -> None:
    if int(kb.get("tenant_id") or 0) == int(endpoint.get("tenant_id") or 0):
        return
    permission = knowledge_base_share_permission(
        int(endpoint.get("tenant_id") or 0), str(kb["id"]), "viewer"
    )
    if permission not in {"editor", "admin"}:
        raise _ToolError(f"this endpoint is not allowed to write to knowledge base {kb['id']!r}")


def _endpoint_context(kb: dict[str, Any], endpoint: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=int(kb["tenant_id"]),
        user_id=f"mcp-{endpoint['id']}",
        role="viewer",
        api_key=None,
    )


def _tool_add_document(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    selector = _string_arg(args, "knowledge_base_id", required=True)
    title = _string_arg(args, "title", required=True)
    content = _raw_string_arg(args, "content")
    url = _string_arg(args, "url")
    if not content.strip() and not url:
        raise _ToolError("either content or url is required")
    kb = _select_knowledge_bases(endpoint, [selector])[0]
    _require_write(endpoint, kb)
    ctx = _endpoint_context(kb, endpoint)
    if url:
        if not re.match(r"^https?://", url):
            raise _ToolError("a valid http(s) URL is required")
        try:
            raw, headers, _ = url_guard.fetch_public_url(
                url,
                headers={"User-Agent": f"PyWeKnora/{SERVER_VERSION}"},
                timeout=30,
                max_bytes=10 * 1024 * 1024,
            )
            parsed = urllib.parse.urlparse(url)
            filename = parsed.path.rsplit("/", 1)[-1] or parsed.netloc or "page.html"
            text = extract_text(filename, raw)
            content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip()
            if content_type.startswith("text/") and not filename:
                filename = parsed.netloc or "page.html"
        except Exception as exc:  # noqa: BLE001 - URL imports surface as tool errors
            raise _ToolError(f"failed to add document: {exc}") from exc
        knowledge = _create_knowledge(
            ctx,
            kb,
            title=title,
            text=text,
            knowledge_type="url",
            source=url,
            channel="mcp",
            file_name=filename,
            file_type="html",
            metadata={"url": url},
        )
    else:
        knowledge = _create_knowledge(
            ctx,
            kb,
            title=title,
            text=content,
            knowledge_type="manual",
            source="manual",
            channel="mcp",
            file_name=title,
            file_type="md",
            metadata={"status": "publish" if args.get("publish", True) else "draft"},
        )
    return _json_result(
        {
            "knowledge_base_id": kb["id"],
            "document": _document_summary(knowledge),
            "note": "Indexing runs asynchronously; the document becomes searchable once parse_status is completed.",
        }
    )


def _tool_update_document(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    knowledge_id = _string_arg(args, "knowledge_id", required=True)
    content = _raw_string_arg(args, "content", required=True)
    knowledge, kb = _document_in_scope(endpoint, knowledge_id)
    _require_write(endpoint, kb)
    title = _string_arg(args, "title") or str(knowledge.get("title") or "")
    chunking = loads(kb.get("chunking_config"), {})
    chunk_size = int(chunking.get("chunk_size") or chunking.get("max_chunk_size") or 512)
    overlap = int(chunking.get("chunk_overlap") or chunking.get("overlap_size") or 50)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE knowledges SET title=?, updated_at=?, processed_at=?, parse_status='completed' "
            "WHERE id=?",
            (title, now, now, knowledge_id),
        )
        conn.execute("DELETE FROM chunks WHERE knowledge_id=?", (knowledge_id,))
        _insert_chunks(conn, int(kb["tenant_id"]), str(kb["id"]), knowledge_id, content, chunk_size, overlap)
    updated = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,)) or knowledge
    _refresh_knowledge_indexes(int(kb["tenant_id"]), kb, updated)
    return _json_result(
        {"document": _document_summary(updated), "note": "Re-indexing runs asynchronously."}
    )


def _tool_delete_document(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    knowledge_id = _string_arg(args, "knowledge_id", required=True)
    knowledge, kb = _document_in_scope(endpoint, knowledge_id)
    _require_write(endpoint, kb)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE knowledges SET deleted_at=?, parse_status='deleting', updated_at=? WHERE id=?",
            (now, now, knowledge_id),
        )
        conn.execute(
            "UPDATE chunks SET deleted_at=?, updated_at=? WHERE knowledge_id=?",
            (now, now, knowledge_id),
        )
    return _json_result(
        {"deleted": True, "knowledge_id": knowledge_id, "title": knowledge.get("title") or ""}
    )


def _resolve_ask_agent(endpoint: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    agent_id = str(endpoint.get("default_agent_id") or "").strip() or "builtin-quick-answer"
    if agent_id in agent_service.USER_BUILTIN_AGENT_IDS:
        config = agent_service.agent_config(int(endpoint.get("tenant_id") or 0), agent_id)
        return agent_id, config
    row = db.fetch_one(
        "SELECT id, config FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (agent_id, int(endpoint.get("tenant_id") or 0)),
    )
    if not row:
        raise _ToolError(f"the agent configured on this endpoint ({agent_id!r}) is not available")
    config = loads(row.get("config"), {})
    return agent_id, config if isinstance(config, dict) else {}


def _ask_knowledge_bases(
    endpoint: dict[str, Any], requested: list[str], agent_config: dict[str, Any]
) -> list[dict[str, Any]]:
    if requested:
        return _select_knowledge_bases(endpoint, requested)
    if loads(endpoint.get("knowledge_base_ids"), []):
        return _allowed_knowledge_bases(endpoint)
    allowed = _allowed_knowledge_bases(endpoint)
    mode = str(agent_config.get("kb_selection_mode") or "").strip().lower()
    configured = agent_config.get("knowledge_bases") or agent_config.get("knowledge_base_ids") or []
    if mode == "selected" and isinstance(configured, list) and configured:
        wanted = {
            str(value.get("id") if isinstance(value, dict) else value)
            for value in configured
            if value
        }
        selected = [kb for kb in allowed if str(kb["id"]) in wanted]
        if not selected:
            raise _ToolError("the agent has no knowledge base available on this endpoint")
        return selected
    return allowed


def _ask_session(endpoint: dict[str, Any], session_id: str, question: str, agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
    owner = f"mcp-{endpoint['id']}"
    if session_id:
        row = db.fetch_one(
            "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (session_id, int(endpoint.get("tenant_id") or 0)),
        )
        if not row or str(row.get("user_id") or "") != owner:
            raise _ToolError(f"session {session_id!r} was not found on this endpoint")
        return row
    session_id = db.new_id()
    now = db.utcnow()
    title = question[:60]
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sessions(id, tenant_id, title, description, max_rounds,
              enable_rewrite, fallback_strategy, fallback_response, keyword_threshold,
              vector_threshold, embedding_top_k, rerank_top_k, rerank_threshold,
              summary_parameters, agent_config, context_config, agent_id, user_id,
              is_pinned, created_at, updated_at, sandbox_config_tenant_id)
            VALUES (?, ?, ?, ?, 5, 1, 'fixed', '很抱歉，我暂时无法回答这个问题。', 0.5,
              0.5, 10, 10, 0.65, '{}', ?, '{}', ?, ?, 0, ?, ?, 0)
            """,
            (
                session_id,
                int(endpoint.get("tenant_id") or 0),
                title,
                "MCP endpoint " + str(endpoint.get("name") or ""),
                dumps(config),
                agent_id,
                owner,
                now,
                now,
            ),
        )
    return db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,)) or {}


async def _tool_ask(endpoint: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    question = _string_arg(args, "question", required=True)
    agent_id, agent_config = _resolve_ask_agent(endpoint)
    knowledge_bases = _ask_knowledge_bases(endpoint, _selectors(args), agent_config)
    session = _ask_session(
        endpoint, _string_arg(args, "session_id"), question, agent_id, agent_config
    )
    kb_ids = [str(kb["id"]) for kb in knowledge_bases]
    tenant_id = int(endpoint.get("tenant_id") or 0)
    request_id = db.new_id()
    user_message_id = _create_message(
        session_id=str(session["id"]),
        role="user",
        content=question,
        request_id=request_id,
        tenant_id=tenant_id,
        channel="mcp",
    )
    assistant_id = _create_message(
        session_id=str(session["id"]),
        role="assistant",
        content="",
        request_id=request_id,
        tenant_id=tenant_id,
        is_completed=False,
        channel="mcp",
    )
    answer = ""
    refs: list[dict[str, Any]] = []
    events = stream_chat_events(
        session=session,
        session_id=str(session["id"]),
        tenant_id=tenant_id,
        payload={"query": question, "knowledge_base_ids": kb_ids},
        agent_mode=str(agent_config.get("agent_mode") or "") == "smart-reasoning",
        channel="mcp",
        precreated_user_message_id=user_message_id,
        precreated_assistant_message_id=assistant_id,
        precreated_request_id=request_id,
        noninteractive=True,
    )
    try:
        async with asyncio.timeout(MCP_ASK_TIMEOUT_SECONDS):
            async for raw_frame in events:
                event = _decode_stream_frame(raw_frame)
                response_type = str(event.get("response_type") or "")
                if response_type == "references":
                    data = event.get("data")
                    candidate = data.get("references") if isinstance(data, dict) else None
                    if isinstance(candidate, list):
                        refs = [item for item in candidate if isinstance(item, dict)]
                elif response_type == "complete":
                    data = event.get("data")
                    if isinstance(data, dict):
                        answer = str(data.get("final_content") or "")
    except TimeoutError:
        _update_message(
            assistant_id,
            content="The answer was interrupted before completion.",
        )
        raise _ToolError("answer timed out") from None
    except Exception as exc:
        _update_message(assistant_id, content=f"The answer failed: {exc}")
        raise

    if not answer.strip():
        answer = "No answer could be produced for this question."
    ask_refs = [
        {
            "knowledge_id": row.get("knowledge_id") or "",
            "knowledge_title": row.get("knowledge_title") or "",
            "chunk_id": row.get("chunk_id") or "",
            "score": row.get("score", 0),
            "excerpt": str(row.get("content") or "")[:600],
        }
        for row in refs
    ]
    data = {
        "answer": answer,
        "session_id": session["id"],
        "agent_id": agent_id,
        "references": ask_refs,
    }
    lines = [answer]
    if ask_refs:
        lines.extend(["", "Sources:"])
        for index, ref in enumerate(ask_refs, start=1):
            lines.append(
                f'[{index}] {ref["knowledge_title"] or ref["knowledge_id"]} '
                f'(document {ref["knowledge_id"]})'
            )
    lines.extend(["", f'session_id: {session["id"]}'])
    return _text_result("\n".join(lines), data)


def _decode_stream_frame(raw_frame: bytes) -> dict[str, Any]:
    for line in raw_frame.decode("utf-8").splitlines():
        if not line.startswith("data: "):
            continue
        value = json.loads(line[6:])
        return value if isinstance(value, dict) else {}
    return {}


_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = {
    "list_knowledge_bases": _tool_list_knowledge_bases,
    "search_knowledge": _tool_search_knowledge,
    "grep_chunks": _tool_grep_chunks,
    "list_documents": _tool_list_documents,
    "read_document": _tool_read_document,
    "wiki_search": _tool_wiki_search,
    "wiki_read_page": _tool_wiki_read_page,
    "wiki_index": _tool_wiki_index,
    "add_document": _tool_add_document,
    "update_document": _tool_update_document,
    "delete_document": _tool_delete_document,
}

_ASYNC_TOOL_HANDLERS: dict[str, Callable[[dict[str, Any], dict[str, Any]], Any]] = {
    "ask": _tool_ask,
}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _modern_protocol(params: dict[str, Any], requested: bool = False) -> bool:
    if requested:
        return True
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return False
    return str(meta.get("io.modelcontextprotocol/protocolVersion") or "") == MODERN_PROTOCOL_VERSION


def _modern_result(result: dict[str, Any], *, cacheable: bool = False) -> dict[str, Any]:
    decorated = dict(result)
    decorated.setdefault("resultType", "complete")
    meta = decorated.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault(
        "io.modelcontextprotocol/serverInfo",
        {"name": SERVER_NAME, "version": SERVER_VERSION},
    )
    decorated["_meta"] = meta
    if cacheable:
        decorated.setdefault("cacheScope", "private")
        decorated.setdefault("ttlMs", 0)
    return decorated


async def _dispatch(
    endpoint: dict[str, Any], payload: Any, *, modern_protocol: bool = False
) -> dict[str, Any] | list[dict[str, Any]] | None:
    if isinstance(payload, list):
        responses = []
        for item in payload:
            try:
                response = await _dispatch(endpoint, item, modern_protocol=modern_protocol)
            except _RPCRejected as exc:
                response = exc.response
            if isinstance(response, list):
                responses.extend(response)
            elif response is not None:
                responses.append(response)
        return responses or None
    if not isinstance(payload, dict):
        return _rpc_error(None, -32600, "Invalid Request")
    request_id = payload.get("id")
    notification = "id" not in payload
    if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        return None if notification else _rpc_error(request_id, -32600, "Invalid Request")
    method = str(payload["method"])
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
    modern = _modern_protocol(params, modern_protocol)
    if modern and method in MODERN_REMOVED_METHODS:
        message = f'"{method}" was removed in protocol version {MODERN_PROTOCOL_VERSION}'
        raise _RPCRejected(
            None if notification else _rpc_error(request_id, -32601, message),
            404,
        )
    if not modern and method == "server/discover":
        return _rpc_error(
            request_id,
            -32601,
            f'"{method}" requires protocol version {MODERN_PROTOCOL_VERSION} or later',
        )
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "")
        protocol = requested if requested in SUPPORTED_PROTOCOLS else LEGACY_PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS,
            },
        }
    if method == "server/discover":
        result = {
            "supportedVersions": [MODERN_PROTOCOL_VERSION],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": _modern_result(result, cacheable=True),
        }
    if method == "notifications/initialized":
        return None if notification else {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        result: dict[str, Any] = {"tools": _tool_definitions(endpoint)}
        if modern:
            result = _modern_result(result, cacheable=True)
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }
    if method == "tools/call":
        name = str(params.get("name") or "").strip()
        allowed = set(loads(endpoint.get("tools"), []))
        if name not in allowed or (name not in _TOOL_HANDLERS and name not in _ASYNC_TOOL_HANDLERS):
            return _rpc_error(
                request_id, -32602, f'tool "{name}" is not enabled on this endpoint'
            )
        if not _allow_call(endpoint):
            error_result = _tool_error("rate limit exceeded for this endpoint, retry shortly")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _modern_result(error_result) if modern else error_result,
            }
        _touch_last_used(str(endpoint["id"]))
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            async_handler = _ASYNC_TOOL_HANDLERS.get(name)
            if async_handler is not None:
                result = await async_handler(endpoint, arguments)
            else:
                result = await anyio.to_thread.run_sync(
                    _TOOL_HANDLERS[name], endpoint, arguments
                )
        except _ToolError as exc:
            result = _tool_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - tool failures are results, not transport failures
            result = _tool_error(f"{name} failed: {exc}")
        if modern:
            result = _modern_result(result)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if notification:
        return None
    return _rpc_error(request_id, -32601, f"Method not found: {method}")


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001 - malformed JSON is a JSON-RPC parse error
        return None


@router.post("/mcp/{endpoint_id}")
async def mcp_post(endpoint_id: str, request: Request):
    try:
        endpoint = _authenticate(endpoint_id, request)
    except _AuthFailure as exc:
        return _auth_response(exc)
    header_modern = request.headers.get("MCP-Protocol-Version") == MODERN_PROTOCOL_VERSION
    payload = await _json_body(request)
    if payload is None:
        return JSONResponse(
            _rpc_error(None, -32700, "Parse error"),
            status_code=400 if header_modern else 200,
        )
    try:
        response = await _dispatch(endpoint, payload, modern_protocol=header_modern)
    except _RPCRejected as exc:
        if exc.response is None:
            return Response(status_code=exc.status_code)
        return JSONResponse(exc.response, status_code=exc.status_code)
    if response is None:
        return Response(status_code=202)
    modern = header_modern
    if isinstance(payload, dict):
        params = payload.get("params")
        if isinstance(params, dict):
            modern = modern or _modern_protocol(params)
    if modern and isinstance(response, dict) and isinstance(response.get("error"), dict):
        code = response["error"].get("code")
        status_code = MODERN_ERROR_HTTP_STATUS.get(code, 200)
        return JSONResponse(response, status_code=status_code)
    return JSONResponse(response)


@router.get("/mcp/{endpoint_id}")
def mcp_get(endpoint_id: str, request: Request):
    try:
        _authenticate(endpoint_id, request)
    except _AuthFailure as exc:
        return _auth_response(exc)
    if request.headers.get("MCP-Protocol-Version") == MODERN_PROTOCOL_VERSION:
        return JSONResponse(
            {"error": f"GET is not supported in protocol version {MODERN_PROTOCOL_VERSION}"},
            status_code=405,
            headers={"Allow": "POST"},
        )
    return JSONResponse(
        {"error": "method not allowed for stateless MCP endpoint"},
        status_code=405,
        headers={"Allow": "POST, DELETE"},
    )


@router.delete("/mcp/{endpoint_id}", status_code=204)
def mcp_delete(endpoint_id: str, request: Request):
    try:
        _authenticate(endpoint_id, request)
    except _AuthFailure as exc:
        return _auth_response(exc)
    if request.headers.get("MCP-Protocol-Version") == MODERN_PROTOCOL_VERSION:
        return JSONResponse(
            {"error": f"DELETE is not supported in protocol version {MODERN_PROTOCOL_VERSION}"},
            status_code=405,
            headers={"Allow": "POST"},
        )
    return Response(status_code=204)

"""Sessions, messages, streaming RAG chat, artifacts and suggestions."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, urlsplit

import anyio.to_thread
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .. import db
from ..api_keys import authorize_knowledge_bases
from ..deps import AuthContext, parse_auth
from ..errors import AppError, conflict, forbidden, not_found, unauthorized, validation
from ..security import decode_token, issue_token
from ..services.agent_tools import (
    MCPOAuthRequired,
    ToolApprovalRequired,
    builtin_edit_sandbox_file_tool,
    builtin_knowledge_tool,
    builtin_list_sandbox_files_tool,
    builtin_local_browser_tool,
    builtin_read_file_tool,
    builtin_shell_exec_tool,
    builtin_web_search_tool,
    builtin_write_sandbox_file_tool,
    execute_tool,
    load_mcp_tools,
)
from ..services import approvals, browser_skill, file_proxy, im_attachments, mcp_oauth, model_runtime, query_understanding, session_sandbox, wiki_agent_tools
from ..services.agents import resolve_config, selected_mcp_service_ids
from ..services.llm import (
    answer as generate_answer,
    answer_with_tools_async,
    current_turn_stats,
    reset_turn_stats,
)
from ..services.memory import auto_remember, detect_explicit_memory, recall as recall_memory, remember_explicit
from ..services.retrieval import build_context, context_images, references, retrieve, subset_note
from ..services.sandbox_terminal import SandboxTerminalClosed
from ..services.skills import selected_skill_rows, skills_prompt
from ..utils import dumps, loads, ok, pagination, resolve_locale, truthy


router = APIRouter(tags=["chat"])


logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task[None]] = set()
_desktop_sessions: set[str] = set()
_desktop_last_activity: dict[str, float] = {}
_desktop_lease_keys: dict[str, str] = {}
_desktop_sessions_lock = threading.Lock()
_desktop_node_id = secrets.token_hex(16)
DESKTOP_IDLE_SECONDS = 15 * 60
DESKTOP_WATCH_INTERVAL = 30.0
DESKTOP_AUTH_RECHECK_SECONDS = 60.0
DESKTOP_LEASE_SECONDS = 45.0
DESKTOP_LEASE_RENEW_SECONDS = 15.0
DESKTOP_RFB_HANDSHAKE_SECONDS = 10.0
DESKTOP_RFB_VERSION = b"RFB 003.008\n"
DESKTOP_LAST_SANDBOX_SECONDS = 7 * 24 * 60 * 60
TERMINAL_IDLE_SECONDS = 15 * 60
TERMINAL_WATCH_INTERVAL = 30.0
TERMINAL_AUTH_RECHECK_SECONDS = 60.0
TERMINAL_MAX_GEOMETRY = 500
TERMINAL_MAX_PER_SESSION = 5
_terminal_counts: dict[str, int] = {}
_terminal_counts_lock = threading.Lock()
STEER_CONSUMED_STATUSES = frozenset({"injected", "dropped"})
MAX_STEER_PERSIST_ATTEMPTS = 3


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _session(ctx: AuthContext, session_id: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (session_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Session not found")
    if row.get("user_id") and row["user_id"] != ctx.user_id:
        raise not_found("Session not found")
    return row


_CHANNEL_SESSION_PREFIXES = (
    "api_tenant_key:",
    "api_external_user:",
    "embed_session:",
    "im:",
    "mcp-",
    "mcp_endpoint:",
)


def _channel_managed_session(row: dict[str, Any]) -> bool:
    owner = str(row.get("user_id") or "")
    description = str(row.get("description") or "")
    return owner.startswith(_CHANNEL_SESSION_PREFIXES) or description.startswith(
        "embed_channel:"
    )


def _can_manage_session(ctx: AuthContext, row: dict[str, Any]) -> bool:
    owner = str(row.get("user_id") or "")
    if not owner or owner == ctx.user_id:
        return True
    return (
        ctx.api_key is None
        and ctx.role in {"admin", "owner"}
        and _channel_managed_session(row)
    )


def _session_for_manage(ctx: AuthContext, session_id: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (session_id, ctx.tenant_id),
    )
    if not row or not _can_manage_session(ctx, row):
        raise not_found("Session not found")
    return row


def _string_references_file(value: str, reference: str) -> bool:
    if not value or not reference:
        return False
    for match in re.finditer(re.escape(reference), value):
        before = value[match.start() - 1] if match.start() else ""
        after = value[match.end()] if match.end() < len(value) else ""
        if not re.match(r"[0-9A-Za-z_./-]", before) and not re.match(r"[0-9A-Za-z_./-]", after):
            return True
    return False


def _value_references_file(value: Any, reference: str) -> bool:
    if isinstance(value, str):
        return _string_references_file(value, reference)
    if isinstance(value, list):
        return any(_value_references_file(item, reference) for item in value)
    if isinstance(value, dict):
        return any(_value_references_file(item, reference) for item in value.values())
    return False


def _message_references_file(message: dict[str, Any], reference: str) -> bool:
    artifacts = db.fetch_all(
        "SELECT deleted_at FROM message_artifacts WHERE session_id=? AND message_id=? AND url=?",
        (message["session_id"], message["id"], reference),
    )
    if artifacts:
        return any(not row.get("deleted_at") for row in artifacts)
    for key in ("content", "knowledge_references", "images", "artifacts", "agent_steps"):
        value = loads(message.get(key), None)
        if value is not None and _value_references_file(value, reference):
            return True
        if value is None and _string_references_file(str(message.get(key) or ""), reference):
            return True
    return False


def _serialize_session(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("agent_config", "context_config", "summary_parameters", "fork_bootstrap"):
        result[key] = loads(result.get(key), {}) if key != "fork_bootstrap" else loads(result.get(key), None)
    result["is_pinned"] = bool(result.get("is_pinned"))
    result["enable_rewrite"] = bool(result.get("enable_rewrite"))
    return result


def _serialize_message(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key, default in (
        ("knowledge_references", []), ("agent_steps", None), ("mentioned_items", []),
        ("images", []), ("artifacts", []), ("attachments", []), ("used_memories", []),
        ("execution_context", {}), ("usage", None), ("sandbox_checkpoint", None),
    ):
        result[key] = loads(result.get(key), default)
    result["is_completed"] = bool(result.get("is_completed"))
    result["is_fallback"] = bool(result.get("is_fallback"))
    return result


def _upload_only_question(locale: str) -> str:
    return "请根据我上传的内容回答。" if str(locale).lower().startswith("zh") else "Please answer based on what I uploaded."


def _query_matches_suggestion_text(query: str, suggestion: str) -> bool:
    q = query.strip()
    s = suggestion.strip()
    if q == s:
        return True
    marker = "[Host context]\n"
    if not s or not q.startswith(marker):
        return False
    rest = q[len(marker):]
    if not rest.endswith(s):
        return False
    head = rest[:-len(s)].rstrip(" \t")
    if not head.endswith("\n\n"):
        return False
    return bool(head[:-2].strip())


def _valid_suggestion_attribution(
    tenant_id: int,
    session_id: str,
    query: str,
    attribution: Any,
) -> bool:
    if attribution is None:
        return True
    if not isinstance(attribution, dict):
        return False
    set_id = str(attribution.get("suggestion_set_id") or "").strip()
    question_id = str(attribution.get("question_id") or "").strip()
    if not set_id or not question_id:
        return False
    row = db.fetch_one(
        "SELECT status, questions FROM message_suggestion_sets "
        "WHERE id=? AND tenant_id=? AND session_id=? ORDER BY created_at DESC LIMIT 1",
        (set_id, tenant_id, session_id),
    )
    if not row or str(row.get("status") or "") != "ready":
        return False
    questions = loads(row.get("questions"), [])
    if not isinstance(questions, list):
        return False
    for question in questions:
        if not isinstance(question, dict) or str(question.get("id") or "") != question_id:
            continue
        if _query_matches_suggestion_text(query, str(question.get("text") or "")):
            return True
    return False


def _request_images(payload: dict[str, Any]) -> list[dict[str, str]]:
    values = payload.get("images") if isinstance(payload.get("images"), list) else []
    images: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, str):
            data = item.strip()
        elif isinstance(item, dict):
            data = str(item.get("data") or "").strip()
        else:
            data = ""
        if data.startswith("data:"):
            images.append({"data": data})
    return images


def _payload_has_upload(payload: dict[str, Any]) -> bool:
    if _request_images(payload):
        return True
    uploads = payload.get("attachment_uploads") if isinstance(payload.get("attachment_uploads"), list) else []
    return any(isinstance(item, dict) and str(item.get("data") or "").strip() for item in uploads)


def _decode_attachment_data(value: str) -> bytes:
    encoded = value.split(",", 1)[-1].strip()
    encoded += "=" * (-len(encoded) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return decoder(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
    raise validation("attachment decode failed")


def _inline_attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    from ..services.chunking import extract_text

    uploads = payload.get("attachment_uploads") if isinstance(payload.get("attachment_uploads"), list) else []
    if len(uploads) > 5:
        raise validation("at most 5 attachments are allowed per request")
    try:
        max_file_bytes = int(os.getenv("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024
    except ValueError:
        max_file_bytes = 50 * 1024 * 1024
    total = 0
    result: list[dict[str, Any]] = []
    for index, upload in enumerate(uploads, start=1):
        if not isinstance(upload, dict) or not str(upload.get("data") or "").strip():
            continue
        raw = _decode_attachment_data(str(upload["data"]))
        if len(raw) > max_file_bytes:
            raise validation(f"attachment {index} exceeds size limit of {max_file_bytes} bytes")
        total += len(raw)
        if total > 100 * 1024 * 1024:
            raise validation("attachments exceed total request limit of 104857600 bytes")
        filename = str(upload.get("file_name") or "attachment.txt")
        result.append(
            {
                "file_name": filename,
                "file_type": filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt",
                "file_size": len(raw),
                "content": extract_text(filename, raw),
            }
        )
    return result


def _session_knowledge_bases(
    session: dict[str, Any],
    requested: list[str] | None = None,
    config: dict[str, Any] | None = None,
    ctx: AuthContext | None = None,
) -> list[str]:
    if requested:
        values = [str(value) for value in requested if value]
        if ctx is not None and ctx.api_key is not None:
            authorize_knowledge_bases(ctx.api_key, values)
        return values
    config = config if config is not None else loads(session.get("agent_config"), {})
    values = config.get("knowledge_bases") or config.get("knowledge_base_ids") or []
    ids: list[str] = []
    for value in values:
        if isinstance(value, dict):
            candidate = value.get("id") or value.get("knowledge_base_id")
        else:
            candidate = value
        if candidate:
            ids.append(str(candidate))
    if ctx is not None and ctx.api_key is not None:
        authorize_knowledge_bases(ctx.api_key, ids)
        if ctx.api_key.is_kb_restricted and not ids:
            ids = list(ctx.api_key.knowledge_base_ids)
    return ids


@router.get("/sessions")
def list_sessions(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    p, size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 100)
    clauses = [
        "tenant_id=?",
        "deleted_at IS NULL",
        "(user_id=? OR user_id IS NULL)",
        "COALESCE(json_extract(agent_config, '$.skill_install'), 0) != 1",
    ]
    params: list[Any] = [ctx.tenant_id, ctx.user_id]
    total = db.fetch_one(f"SELECT COUNT(*) AS n FROM sessions WHERE {' AND '.join(clauses)}", params)["n"]
    rows = db.fetch_all(
        f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY is_pinned DESC, COALESCE(updated_at, created_at) DESC LIMIT ? OFFSET ?",
        [*params, size, offset],
    )
    return {
        "success": True,
        "data": [_serialize_session(row) for row in rows],
        "total": int(total),
        "page": p,
        "page_size": size,
    }


@router.post("/sessions", status_code=201)
async def create_session(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    session_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sessions(id, tenant_id, title, description, max_rounds,
              enable_rewrite, fallback_strategy, fallback_response, keyword_threshold,
              vector_threshold, embedding_top_k, rerank_top_k, rerank_threshold,
              summary_parameters, agent_config, context_config, agent_id, user_id,
              is_pinned, created_at, updated_at, sandbox_config_tenant_id)
            VALUES (?, ?, ?, ?, 5, 1, 'fixed', '很抱歉，我暂时无法回答这个问题。', 0.5,
              0.5, 10, 10, 0.65, '{}', ?, ?, ?, ?, 0, ?, ?, 0)
            """,
            (
                session_id, ctx.tenant_id, str(data.get("title") or "新会话"),
                str(data.get("description") or ""), dumps(data.get("agent_config") or {}),
                dumps(data.get("context_config") or {}), data.get("agent_id") or None,
                ctx.user_id, now, now,
            ),
        )
    return ok(_serialize_session(_session(ctx, session_id)))


@router.delete("/sessions/batch")
async def batch_delete_sessions(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    requested_ids = [str(value).strip() for value in data.get("ids") or [] if str(value).strip()]
    delete_all = bool(data.get("delete_all"))
    if not delete_all and not requested_ids:
        raise validation("ids are required when delete_all is false")
    ids = list(dict.fromkeys(requested_ids))
    now = db.utcnow()
    scope = browser_skill.scope_key(ctx.tenant_id, ctx.user_id)
    if delete_all:
        rows = db.fetch_all(
            "SELECT id FROM sessions WHERE tenant_id=? AND user_id=? AND deleted_at IS NULL",
            (ctx.tenant_id, ctx.user_id),
        )
    else:
        placeholders = ",".join("?" for _ in ids)
        rows = db.fetch_all(
            f"SELECT * FROM sessions WHERE tenant_id=? AND deleted_at IS NULL "
            f"AND id IN ({placeholders})",
            (ctx.tenant_id, *ids),
        )
        rows = [row for row in rows if _can_manage_session(ctx, row)]
        if not rows:
            raise not_found("Session not found")
    visible_ids = [str(row["id"]) for row in rows]
    with db.transaction() as conn:
        if delete_all:
            conn.execute("UPDATE sessions SET deleted_at=?, updated_at=? WHERE tenant_id=? AND user_id=? AND deleted_at IS NULL", (now, now, ctx.tenant_id, ctx.user_id))
        elif visible_ids:
            placeholders = ",".join("?" for _ in visible_ids)
            conn.execute(
                f"UPDATE sessions SET deleted_at=?, updated_at=? WHERE tenant_id=? "
                f"AND deleted_at IS NULL AND id IN ({placeholders})",
                (now, now, ctx.tenant_id, *visible_ids),
            )
    if delete_all:
        browser_skill.forget_all(scope)
    else:
        for row in rows:
            browser_skill.forget(
                browser_skill.scope_key(ctx.tenant_id, str(row.get("user_id") or "")),
                [str(row["id"])],
            )
    if visible_ids:
        await anyio.to_thread.run_sync(
            lambda: [session_sandbox.delete(ctx.tenant_id, session_id) for session_id in visible_ids]
        )
    return ok({"deleted": "all" if delete_all else len(visible_ids)})


@router.get("/sessions/continue-stream/{session_id}")
def continue_stream(session_id: str, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    return Response(status_code=204)


@router.get("/sessions/{session_id}")
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(_serialize_session(_session(ctx, session_id)))


@router.put("/sessions/{session_id}")
async def update_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    session = _session_for_manage(ctx, session_id)
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    if "title" in data:
        updates["title"] = str(data.get("title") or "新会话")
    if "description" in data:
        updates["description"] = str(data.get("description") or "")
    if "agent_config" in data:
        updates["agent_config"] = dumps(data["agent_config"])
    with db.transaction() as conn:
        db.update("sessions", updates, "id=?", (session_id,), conn)
    return ok(_serialize_session(session | updates))


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    session = _session_for_manage(ctx, session_id)
    browser_skill.forget(
        browser_skill.scope_key(ctx.tenant_id, str(session.get("user_id") or "")),
        [session_id],
    )
    session_sandbox.delete(ctx.tenant_id, session_id)
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET deleted_at=?, updated_at=? WHERE id=?", (db.utcnow(), db.utcnow(), session_id))
    return ok(message="Session deleted")


@router.delete("/sessions/{session_id}/messages")
def clear_messages(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    with db.transaction() as conn:
        conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    return ok(message="Messages cleared")


@router.post("/sessions/{session_id}/pin")
def pin_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET is_pinned=1, pinned_at=?, updated_at=? WHERE id=?", (db.utcnow(), db.utcnow(), session_id))
    return ok(_serialize_session(_session(ctx, session_id)))


@router.delete("/sessions/{session_id}/pin")
def unpin_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET is_pinned=0, pinned_at=NULL, updated_at=? WHERE id=?", (db.utcnow(), session_id))
    return ok(_serialize_session(_session(ctx, session_id)))


@router.post("/sessions/{session_id}/generate_title")
async def generate_title(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    session = _session(ctx, session_id)
    data = await body(request)
    title = str(data.get("query") or data.get("title") or session.get("title") or "")
    title = re.sub(r"\s+", " ", title).strip()[:40] or "新会话"
    with db.transaction() as conn:
        conn.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title, db.utcnow(), session_id))
    return ok({"session_id": session_id, "title": title})


@router.post("/sessions/{session_id}/title")
async def set_session_title(session_id: str, request: Request) -> dict[str, Any]:
    return await generate_title(session_id, request)


@router.post("/agent/tool-approvals/{pending_id}")
async def resolve_tool_approval(pending_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    decision = str(data.get("decision") or "")
    if decision not in {"approve", "reject"}:
        raise validation("decision must be approve or reject")
    reason = str(data.get("reason") or "")
    modified_args = data.get("modified_args")
    if modified_args is not None and not isinstance(modified_args, dict):
        raise validation("modified_args must be a non-null JSON object")
    resolution = approvals.Decision(
        approved=decision == "approve",
        modified_args=modified_args if decision == "approve" else None,
        reason=reason,
    )
    try:
        approvals.resolve(
            pending_id,
            resolution,
            kind="tool",
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
    except approvals.PendingNotFound as exc:
        raise not_found("pending approval not found or already completed") from exc
    except approvals.AlreadyResolved as exc:
        raise validation("pending approval already resolved (timeout / cancel raced your action)") from exc
    except approvals.TenantMismatch as exc:
        raise validation("workspace mismatch") from exc
    except approvals.UserMismatch as exc:
        raise validation("user mismatch: only the session owner may resolve this approval") from exc
    return ok({"pending_id": pending_id, "decision": decision, "resolved": True})


@router.post("/agent/mcp-oauth-resolutions/{pending_id}")
async def resolve_mcp_oauth(pending_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    service_id = str(data.get("service_id") or "").strip()
    if not service_id:
        raise validation("service_id is required")
    service = db.fetch_one(
        "SELECT id FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (service_id, ctx.tenant_id),
    )
    if not service:
        raise not_found("MCP service not found")
    decision = str(data.get("decision") or "authorize").strip().lower()
    if decision in {"cancel", "reject", "skip"}:
        try:
            approvals.resolve(
                pending_id,
                approvals.Decision(approved=False, reason="user canceled", canceled=True),
                kind="oauth",
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                service_id=service_id,
            )
        except approvals.PendingNotFound as exc:
            raise not_found("pending authorization not found or already completed") from exc
        except approvals.AlreadyResolved as exc:
            raise validation("pending authorization already resolved (timeout / cancel raced your action)") from exc
        except approvals.TenantMismatch as exc:
            raise validation("workspace mismatch") from exc
        except approvals.UserMismatch as exc:
            raise validation("user mismatch: only the session owner may resolve this prompt") from exc
        return ok({"pending_id": pending_id, "decision": decision, "resolved": True})
    if decision != "authorize":
        raise validation("decision must be authorize or cancel")
    if not mcp_oauth.is_authorized(ctx.tenant_id, ctx.user_id, service_id):
        raise conflict("authorization not completed yet for this MCP service")
    try:
        approvals.resolve(
            pending_id,
            approvals.Decision(approved=True),
            kind="oauth",
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            service_id=service_id,
        )
    except approvals.PendingNotFound as exc:
        raise not_found("pending authorization not found or already completed") from exc
    except approvals.AlreadyResolved as exc:
        raise validation("pending authorization already resolved (timeout / cancel raced your action)") from exc
    except approvals.TenantMismatch as exc:
        raise validation("workspace mismatch") from exc
    except approvals.UserMismatch as exc:
        raise validation("user mismatch: only the session owner may resolve this prompt") from exc
    return ok({"pending_id": pending_id, "decision": decision, "resolved": True})


@router.post("/agent/mcp-oauth-resolutions/{pending_id}/cancel")
def cancel_mcp_oauth(pending_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    try:
        approvals.resolve(
            pending_id,
            approvals.Decision(approved=False, reason="user canceled", canceled=True),
            kind="oauth",
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
        )
    except approvals.PendingNotFound as exc:
        raise not_found("pending authorization not found or already completed") from exc
    except approvals.AlreadyResolved as exc:
        raise validation("pending authorization already resolved (timeout / cancel raced your action)") from exc
    except approvals.TenantMismatch as exc:
        raise validation("workspace mismatch") from exc
    except approvals.UserMismatch as exc:
        raise validation("user mismatch: only the session owner may resolve this prompt") from exc
    return ok({"pending_id": pending_id, "cancelled": True})


@router.post("/sessions/{session_id}/stop")
def stop_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE session_steer_messages SET status='canceled', updated_at=? WHERE tenant_id=? AND session_id=? AND status='queued'",
            (db.utcnow(), ctx.tenant_id, session_id),
        )
    return ok({"stopped": True})


@router.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    source = _session(ctx, session_id)
    data = await body(request)
    message_id = str(data.get("message_id") or "")
    new_id = db.new_id()
    now = db.utcnow()
    values = dict(source)
    values.update({"id": new_id, "title": str(data.get("title") or (source.get("title") or "新会话") + " 分支"), "parent_session_id": session_id, "forked_from_message_id": message_id or None, "created_at": now, "updated_at": now, "is_pinned": 0, "pinned_at": None})
    values.pop("deleted_at", None)
    with db.transaction() as conn:
        db.insert("sessions", values, conn)
        messages = conn.execute("SELECT * FROM messages WHERE session_id=? AND deleted_at IS NULL ORDER BY created_at", (session_id,)).fetchall()
        for original in messages:
            if message_id and original["id"] == message_id:
                copied = dict(original)
                copied.update({"id": db.new_id(), "session_id": new_id, "created_at": now, "updated_at": now})
                db.insert("messages", copied, conn)
                break
            copied = dict(original)
            copied.update({"id": db.new_id(), "session_id": new_id, "created_at": now, "updated_at": now})
            db.insert("messages", copied, conn)
    return ok(_serialize_session(_session(ctx, new_id)))


@router.post("/sessions/{session_id}/rewind")
async def rewind_session(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    data = await body(request)
    message_id = str(data.get("message_id") or "")
    target = db.fetch_one("SELECT created_at FROM messages WHERE id=? AND session_id=?", (message_id, session_id))
    if not target:
        raise not_found("Message not found")
    with db.transaction() as conn:
        conn.execute("UPDATE messages SET deleted_at=?, updated_at=? WHERE session_id=? AND created_at>?", (db.utcnow(), db.utcnow(), session_id, target["created_at"]))
    return ok({"rewound": True})


@router.post("/sessions/{session_id}/attachments", status_code=201)
async def upload_attachment(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    form = await request.form()
    uploads = [value for key, value in form.multi_items() if key in {"file", "files"} and hasattr(value, "read")]
    results: list[dict[str, Any]] = []
    for upload in uploads:
        from ..services.chunking import extract_text

        raw = await upload.read()
        filename = str(getattr(upload, "filename", "") or "attachment.txt")
        text = extract_text(filename, raw)
        attachment_id = db.new_id()
        now = db.utcnow()
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO temporary_documents(id, tenant_id, session_id, resource_ref,
                  file_name, file_type, mime_type, file_size, status, content, chunks,
                  image_refs, metadata, processing_options, token_count, chunk_count,
                  error_message, expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, '[]', '[]', '{}', '{}', ?, 0, '', datetime('now', '+1 day'), ?, ?)
                """,
                (
                    attachment_id, ctx.tenant_id, session_id, attachment_id, filename,
                    filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt",
                    getattr(upload, "content_type", "") or "application/octet-stream", len(raw), text,
                    len(text) // 4, now, now,
                ),
            )
        results.append({"id": attachment_id, "file_name": filename, "file_size": len(raw), "status": "ready"})
    return ok(results)


@router.get("/sessions/{session_id}/attachments")
def list_attachments(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    rows = db.fetch_all("SELECT * FROM temporary_documents WHERE session_id=? AND deleted_at IS NULL", (session_id,))
    return ok(rows)


@router.get("/sessions/{session_id}/attachments/{attachment_id}")
def get_attachment(session_id: str, attachment_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    row = db.fetch_one("SELECT * FROM temporary_documents WHERE id=? AND session_id=? AND deleted_at IS NULL", (attachment_id, session_id))
    if not row:
        raise not_found("Attachment not found")
    return ok(row)


@router.get("/sessions/{session_id}/attachments/{attachment_id}/preview")
def preview_attachment(session_id: str, attachment_id: str, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    row = db.fetch_one("SELECT * FROM temporary_documents WHERE id=? AND session_id=? AND deleted_at IS NULL", (attachment_id, session_id))
    if not row:
        raise not_found("Attachment not found")
    return Response(row.get("content") or "", media_type="text/plain")


@router.get("/sessions/{session_id}/messages/{message_id}/files")
def message_file(session_id: str, message_id: str, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    session = _session(ctx, session_id)
    message = db.fetch_one(
        "SELECT * FROM messages WHERE id=? AND session_id=? AND deleted_at IS NULL",
        (message_id, session_id),
    )
    if not message:
        raise not_found("Message not found")
    reference = str(request.query_params.get("file_path") or "").strip()
    if not reference:
        raise validation("missing required parameter: file_path")
    if not _message_references_file(message, reference):
        raise forbidden("file not referenced by message")
    owner_tenant = int(message.get("agent_tenant_id") or session["tenant_id"])
    return file_proxy.file_response(reference, owner_tenant)


@router.delete("/sessions/{session_id}/attachments/{attachment_id}")
def delete_attachment(session_id: str, attachment_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    with db.transaction() as conn:
        conn.execute("UPDATE temporary_documents SET deleted_at=? WHERE id=? AND session_id=?", (db.utcnow(), attachment_id, session_id))
    return ok(message="Attachment deleted")


@router.get("/messages/{session_id}/load")
def load_messages(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    limit = min(200, max(1, int(request.query_params.get("limit") or 20)))
    before = request.query_params.get("before_time")
    clauses = ["session_id=?", "deleted_at IS NULL"]
    params: list[Any] = [session_id]
    if before:
        clauses.append("created_at<?")
        params.append(before)
    rows = db.fetch_all(
        f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY created_at DESC, rowid DESC LIMIT ?",
        [*params, limit],
    )
    rows.reverse()
    return {"success": True, "data": [_serialize_message(row) for row in rows], "total": len(rows)}


@router.delete("/messages/{session_id}/{message_id}")
def delete_message(session_id: str, message_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    with db.transaction() as conn:
        conn.execute("UPDATE messages SET deleted_at=?, updated_at=? WHERE id=? AND session_id=?", (db.utcnow(), db.utcnow(), message_id, session_id))
    return ok(message="Message deleted")


@router.post("/messages/search")
async def search_messages(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    keyword = str(data.get("query") or data.get("keyword") or "").strip()
    rows = db.fetch_all(
        """
        SELECT m.*, s.title AS session_title FROM messages m
        JOIN sessions s ON s.id=m.session_id
        WHERE s.tenant_id=? AND s.deleted_at IS NULL AND m.deleted_at IS NULL
          AND m.content LIKE ? ORDER BY m.created_at DESC LIMIT 100
        """,
        (ctx.tenant_id, f"%{keyword}%"),
    )
    return ok([_serialize_message(row) for row in rows])


@router.get("/messages/chat-history-stats")
def chat_history_stats(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    counts = db.fetch_one(
        "SELECT (SELECT COUNT(*) FROM sessions WHERE tenant_id=? AND deleted_at IS NULL) AS sessions, (SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.tenant_id=? AND m.deleted_at IS NULL) AS messages",
        (ctx.tenant_id, ctx.tenant_id),
    )
    return ok(counts)


def _insert_message(
    conn: Any,
    *,
    session_id: str,
    role: str,
    content: str,
    request_id: str,
    tenant_id: int,
    ctx: AuthContext | None = None,
    knowledge_references: list[dict[str, Any]] | None = None,
    is_completed: bool = True,
    channel: str = "web",
    artifacts: list[dict[str, Any]] | None = None,
    used_memories: list[dict[str, Any]] | None = None,
    mentioned_items: list[dict[str, Any]] | None = None,
    images: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    message_id = message_id or db.new_id()
    now = db.utcnow()
    conn.execute(
        """
        INSERT INTO messages(id, request_id, session_id, role, content, rendered_content,
          knowledge_references, agent_steps, mentioned_items, images, artifacts,
          is_completed, is_fallback, channel, agent_id, agent_tenant_id, model_id,
          execution_context, used_memories, created_at, updated_at, attachments, usage)
        VALUES (?, ?, ?, ?, ?, '', ?, NULL, ?, ?, ?, ?, 0, ?, '', ?, '', '{}', ?, ?, ?, ?, NULL)
        """,
        (
            message_id, request_id, session_id, role, content,
            dumps(knowledge_references or []), dumps(mentioned_items or []),
            dumps(images or []),
            dumps(artifacts or []), 1 if is_completed else 0, channel, tenant_id,
            dumps(used_memories or []), now, now, dumps(attachments or []),
        ),
    )
    conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
    return message_id


def _create_message(
    *,
    session_id: str,
    role: str,
    content: str,
    request_id: str,
    tenant_id: int,
    ctx: AuthContext | None = None,
    knowledge_references: list[dict[str, Any]] | None = None,
    is_completed: bool = True,
    channel: str = "web",
    artifacts: list[dict[str, Any]] | None = None,
    used_memories: list[dict[str, Any]] | None = None,
    mentioned_items: list[dict[str, Any]] | None = None,
    images: list[dict[str, Any]] | None = None,
    message_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> str:
    with db.transaction() as conn:
        return _insert_message(
            conn,
            session_id=session_id,
            role=role,
            content=content,
            request_id=request_id,
            tenant_id=tenant_id,
            ctx=ctx,
            knowledge_references=knowledge_references,
            is_completed=is_completed,
            channel=channel,
            artifacts=artifacts,
            used_memories=used_memories,
            mentioned_items=mentioned_items,
            images=images,
            message_id=message_id,
            attachments=attachments,
        )


def _update_message(
    message_id: str,
    *,
    content: str,
    knowledge_references: list[dict[str, Any]] | None = None,
    used_memories: list[dict[str, Any]] | None = None,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE messages SET content=?, knowledge_references=?, used_memories=?,
              is_completed=1, updated_at=?
            WHERE id=?
            """,
            (
                content,
                dumps(knowledge_references or []),
                dumps(used_memories or []),
                db.utcnow(),
                message_id,
            ),
        )


def _history(session_id: str, limit: int = 8, locale: str = "zh-CN") -> list[dict[str, str]]:
    rows = db.fetch_all(
        "SELECT role, content, images, attachments FROM messages WHERE session_id=? AND deleted_at IS NULL AND is_completed=1 ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (session_id, limit),
    )
    rows.reverse()
    history: list[dict[str, str]] = []
    for row in rows:
        if row["role"] not in {"user", "assistant"}:
            continue
        content = str(row.get("content") or "")
        if row["role"] == "user" and not content.strip():
            images = loads(row.get("images"), [])
            attachments = loads(row.get("attachments"), [])
            if isinstance(images, list) and images:
                content = _upload_only_question(locale)
            elif isinstance(attachments, list) and attachments:
                content = _upload_only_question(locale)
        history.append({"role": row["role"], "content": content})
    return history


def _live_assistant_id(session_id: str) -> str:
    row = db.fetch_one(
        """
        SELECT id FROM messages
        WHERE session_id=? AND role='assistant' AND is_completed=0 AND deleted_at IS NULL
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (session_id,),
    )
    return str(row["id"]) if row else ""


def _browser_scope_key(ctx: AuthContext) -> str:
    return hashlib.sha256(f"{ctx.tenant_id}:{ctx.user_id}".encode()).hexdigest()[:32]


def _browser_status(ctx: AuthContext, session_id: str) -> dict[str, Any]:
    enabled = bool(os.getenv("BROWSERSKILL_BINARY"))
    scope_key = _browser_scope_key(ctx)
    result = browser_skill.status(scope_key, session_id)
    result["enabled"] = enabled and bool(result.get("enabled"))
    if not result["enabled"]:
        result["connected"] = False
        result["extension_version"] = ""
    return result


def _browser_extension_path() -> str:
    value = str(os.getenv("BROWSERSKILL_EXTENSION_PATH") or "").strip()
    if not value:
        return ""
    path = Path(value).expanduser()
    return str(path) if path.is_file() else ""


def _browser_device_connected(device: dict[str, Any] | None) -> bool:
    if not device or device.get("revoked_at"):
        return False
    now = db.utcnow()
    return str(device.get("expires_at") or "") > now and str(device.get("lease_until") or "") > now


def _browser_device_json(device: dict[str, Any] | None) -> dict[str, Any] | None:
    if not device:
        return None
    return {
        "id": str(device.get("id") or ""),
        "label": str(device.get("label") or ""),
        "created_at": device.get("created_at"),
        "last_seen_at": device.get("last_seen_at"),
        "expires_at": device.get("expires_at"),
        "renew_after": device.get("renew_after"),
    }


def _browser_account_status(ctx: AuthContext) -> dict[str, Any]:
    enabled = bool(os.getenv("BROWSERSKILL_BINARY"))
    device = db.fetch_one(
        "SELECT * FROM browser_devices WHERE scope_key=?",
        (_browser_scope_key(ctx),),
    )
    now = db.utcnow()
    active_device = (
        device
        if device and not device.get("revoked_at") and str(device.get("expires_at") or "") > now
        else None
    )
    connected = enabled and _browser_device_connected(device)
    status: dict[str, Any] = {}
    if enabled and active_device:
        try:
            status = browser_skill.status(_browser_scope_key(ctx), "")
        except browser_skill.BrowserSkillError:
            status = {}
    try:
        version = browser_skill.extension_version(_browser_scope_key(ctx)) if connected else ""
    except browser_skill.BrowserSkillError:
        version = ""
    result: dict[str, Any] = {
        "extension_available": bool(_browser_extension_path()),
        "action_elapsed_ms": int(status.get("action_elapsed_ms") or 0),
        "stopping": bool(status.get("stopping")),
        "idle": bool(status.get("idle")),
        "needs_help": bool(status.get("needs_help")),
        "enabled": enabled,
        "selected": bool(status.get("selected")),
        "connected": bool(status.get("connected", connected)),
        "paused": bool(status.get("paused")),
    }
    for key in ("action", "page_url", "last_error", "help_prompt", "extension_version", "task_id"):
        value = version if key == "extension_version" else status.get(key)
        if value:
            result[key] = value
    device_data = _browser_device_json(active_device)
    if device_data is not None:
        result["device"] = device_data
    return result


def _browser_pairing_link(origin: str) -> str:
    public_url = str(os.getenv("WEKNORA_PUBLIC_URL") or os.getenv("BROWSERSKILL_PUBLIC_URL") or "").strip()
    if public_url:
        parsed = urlsplit(public_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (parsed.scheme == "https" or local)
        ):
            scheme = "wss" if parsed.scheme == "https" else "ws"
            return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/api/v1/local-browser/extension"
    parsed = urlsplit(origin)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (parsed.scheme == "http" and not local)
    ):
        raise validation("invalid BrowserSkill page origin")
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/api/v1/local-browser/extension"


def _browser_timestamp(offset_seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds))


@router.get("/me/browser")
def browser_account(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(_browser_account_status(ctx))


@router.post("/me/browser")
async def browser_account_action(request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    if not os.getenv("BROWSERSKILL_BINARY"):
        return JSONResponse({"error": "local browser is unavailable"}, status_code=503)
    raw = await request.body()
    if len(raw) > 4096:
        return JSONResponse({"error": "invalid browser action"}, status_code=400)
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        return JSONResponse({"error": "invalid browser action"}, status_code=400)
    data = parsed if isinstance(parsed, dict) else {}
    action = str(data.get("action") or "").strip().lower()
    if action == "pair":
        link = _browser_pairing_link(str(data.get("origin") or ""))
        token = secrets.token_urlsafe(32)
        now = db.utcnow()
        expires_at = _browser_timestamp(300)
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO browser_pairings(scope_key, token_hash, tenant, \"user\", expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    _browser_scope_key(ctx),
                    hashlib.sha256(token.encode()).hexdigest(),
                    ctx.tenant_id,
                    ctx.user_id,
                    expires_at,
                ),
            )
        return ok({"pairing_link": link + "#" + token, "expires_at": expires_at})
    if action == "revoke":
        now = db.utcnow()
        scope_key = _browser_scope_key(ctx)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE browser_devices SET revoked_at=? WHERE scope_key=?",
                (now, scope_key),
            )
            conn.execute("DELETE FROM browser_pairings WHERE scope_key=?", (scope_key,))
            conn.execute("DELETE FROM browser_task_interruptions WHERE scope_key=?", (scope_key,))
        browser_skill.clear_tasks(scope_key)
        return ok(_browser_account_status(ctx))
    return JSONResponse({"error": "invalid account browser action"}, status_code=400)


@router.get("/me/browser/extension")
def browser_extension_download(request: Request) -> FileResponse:
    parse_auth(request)
    path = _browser_extension_path()
    if not path:
        raise not_found("extension package is not configured")
    return FileResponse(
        path,
        media_type="application/zip",
        filename="browser-skill-weknora.zip",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/local-browser/extension/authorize")
async def browser_extension_authorize(request: Request) -> JSONResponse:
    if not os.getenv("BROWSERSKILL_BINARY"):
        raise AppError("browser unavailable", status=503, code=1006)
    origin = str(request.headers.get("origin") or "")
    if not browser_skill.valid_extension_origin(origin):
        raise AppError("extension origin required", status=403, code=1002)
    authorization = str(request.headers.get("authorization") or "")
    if not authorization.lower().startswith("bearer "):
        raise validation("invalid authorization request")
    token = authorization.split(" ", 1)[1].strip()
    data = await body(request)
    action = str(data.get("action") or "").strip().lower()
    next_token = str(data.get("next_token") or "").strip()
    if not token or not next_token or token == next_token:
        raise validation("invalid authorization request")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    next_hash = hashlib.sha256(next_token.encode()).hexdigest()
    now = db.utcnow()
    device_id = ""
    expires_at = ""
    renew_after = ""
    if action == "pair":
        pairing = db.fetch_one(
            "SELECT * FROM browser_pairings WHERE token_hash=? AND expires_at>?",
            (token_hash, now),
        )
        if not pairing:
            raise AppError("authorization expired or already used", status=401, code=1001)
        label = str(data.get("label") or "Chrome").strip()[:100] or "Chrome"
        device_id = uuid.uuid4().hex
        expires_at = _browser_timestamp(30 * 24 * 60 * 60)
        renew_after = _browser_timestamp(60 * 60)
        with db.transaction() as conn:
            conn.execute("DELETE FROM browser_pairings WHERE scope_key=?", (pairing["scope_key"],))
            conn.execute(
                "INSERT OR REPLACE INTO browser_devices(scope_key, id, tenant, \"user\", label, "
                "token_hash, previous_hash, previous_until, expires_at, renew_after, created_at, "
                "last_seen_at, revoked_at, owner, owner_url, lease_key, lease_until) "
                "VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, NULL, '', '', '', ?)",
                (
                    pairing["scope_key"], device_id, int(pairing["tenant"]), str(pairing["user"]),
                    label, next_hash, now, expires_at, renew_after, now, now, now,
                ),
            )
    elif action == "renew":
        device = db.fetch_one(
            "SELECT * FROM browser_devices WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
            (token_hash, now),
        )
        if not device:
            raise AppError("authorization expired or already used", status=401, code=1001)
        device_id = str(device["id"])
        expires_at = _browser_timestamp(30 * 24 * 60 * 60)
        renew_after = _browser_timestamp(60 * 60)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE browser_devices SET token_hash=?, previous_hash=?, previous_until=?, "
                "expires_at=?, renew_after=?, last_seen_at=?, lease_until=? WHERE scope_key=?",
                (
                    next_hash, token_hash, _browser_timestamp(300), expires_at,
                    renew_after, now, _browser_timestamp(30), device["scope_key"],
                ),
            )
    else:
        raise validation("invalid action")
    return JSONResponse(
        {
            "device_id": device_id,
            "service_name": "WeKnora",
            "expires_at": expires_at,
            "renew_after": renew_after,
        }
    )


@router.post("/local-browser/internal", include_in_schema=False)
async def browser_skill_internal(request: Request) -> JSONResponse:
    raw = await request.body()
    if len(raw) > 1024 * 1024:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    try:
        browser_skill.verify_internal(
            str(request.headers.get("x-browser-timestamp") or ""),
            str(request.headers.get("x-browser-nonce") or ""),
            str(request.headers.get("x-browser-signature") or ""),
            raw,
        )
    except browser_skill.InternalRequestError as exc:
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    scope_data = payload.get("scope") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("node") != browser_skill.NODE_ID
        or not isinstance(scope_data, dict)
    ):
        return JSONResponse({"error": "invalid owner request"}, status_code=409)
    try:
        tenant_id = int(scope_data.get("tenant") or 0)
    except (TypeError, ValueError):
        tenant_id = 0
    user_id = str(scope_data.get("user") or "")
    if tenant_id <= 0 or not user_id:
        return JSONResponse({"error": "invalid owner request"}, status_code=409)
    scope = browser_skill.scope_key(tenant_id, user_id)
    if not browser_skill.owns(scope):
        return JSONResponse({"error": "browser owner changed; refresh status"})
    session = str(payload.get("session") or "")
    operation = str(payload.get("operation") or "")
    method = str(payload.get("method") or "")
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    error: browser_skill.BrowserSkillError | None = None
    data: Any = None
    try:
        if operation == "status":
            data = browser_skill.status(scope, session)
        elif operation == "call":
            tool_params = params.get("tool_params")
            keep_open = params.get("keep_open")
            data = await anyio.to_thread.run_sync(
                lambda: browser_skill.call_tool(
                    scope,
                    session,
                    method,
                    tool_params if isinstance(tool_params, dict) else {},
                    keep_open=bool(keep_open) if isinstance(keep_open, bool) else None,
                )
            )
        elif operation == "control":
            await anyio.to_thread.run_sync(browser_skill.control, scope, session, method)
        elif operation == "preview":
            data = await browser_skill.ui_call(scope, session, "ui.task_preview")
        elif operation == "focus":
            data = await browser_skill.ui_call(scope, session, "ui.task_focus")
        elif operation == "finish_turn":
            keep_open = params.get("keep_open")
            await anyio.to_thread.run_sync(
                lambda: browser_skill.finish_turn(scope, session, keep_open=bool(keep_open))
            )
        elif operation == "forget":
            await anyio.to_thread.run_sync(browser_skill.forget, scope, [session])
        else:
            raise browser_skill.BrowserSkillError("unsupported browser operation")
    except browser_skill.BrowserSkillError as exc:
        error = exc
    if error is not None:
        result: dict[str, Any] = {"error": str(error)}
        if error.code or error.data is not None:
            result["rpc_error"] = {
                "code": error.code,
                "message": str(error),
                "data": browser_skill.bounded_error_data(error.data),
            }
        return JSONResponse(result)
    return JSONResponse({"data": data})


@router.websocket("/local-browser/extension")
async def browser_extension_gateway(websocket: WebSocket) -> None:
    if not browser_skill.enabled():
        await websocket.close(code=4403)
        return
    origin = str(websocket.headers.get("origin") or "")
    if not browser_skill.valid_extension_origin(origin):
        await websocket.close(code=4403)
        return
    protocols = list(websocket.scope.get("subprotocols") or [])
    if len(protocols) != 1 or not protocols[0].startswith(browser_skill.AUTH_PROTOCOL):
        await websocket.close(code=4401)
        return
    token = protocols[0][len(browser_skill.AUTH_PROTOCOL):]
    if len(token) != 43:
        await websocket.close(code=4401)
        return
    device = browser_skill.authenticate(token)
    if not device:
        await websocket.close(code=4401)
        return
    try:
        browser_skill.reserve_connection()
    except browser_skill.BrowserSkillError:
        await websocket.close(code=1013)
        return
    reserved = True
    try:
        _, port = await anyio.to_thread.run_sync(browser_skill.ensure_daemon)
        upstream = await browser_skill.connect_upstream(port, origin)
    except Exception:  # noqa: BLE001 - the extension only needs an actionable close
        browser_skill.release_connection_reservation()
        reserved = False
        await websocket.close(code=1011)
        return
    try:
        lease_key = browser_skill.claim(str(device["id"]), origin)
    except browser_skill.BrowserSkillError:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001 - best-effort transport cleanup
            pass
        browser_skill.release_connection_reservation()
        reserved = False
        await websocket.close(code=4409)
        return
    scope = str(device["scope_key"])
    try:
        await websocket.accept(subprotocol=protocols[0])
    except Exception:
        browser_skill.release(str(device["id"]), lease_key)
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001 - best-effort transport cleanup
            pass
        browser_skill.release_connection_reservation()
        reserved = False
        raise
    heartbeat_task: asyncio.Task[None] | None = None
    live: browser_skill.LiveConnection | None = None
    try:
        try:
            first = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            frame, version = browser_skill.prepare_handshake(first, lease_key)
        except (asyncio.TimeoutError, WebSocketDisconnect, browser_skill.BrowserSkillError):
            await websocket.close(code=1008)
            return
        await upstream.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
        try:
            reply = await asyncio.wait_for(upstream.recv(), timeout=5)
            if not isinstance(reply, str):
                raise browser_skill.BrowserSkillError("BrowserSkill handshake failed")
            browser_skill.validate_handshake_reply(reply, str(frame["id"]))
        except (asyncio.TimeoutError, browser_skill.BrowserSkillError):
            await websocket.close(code=1008)
            return
        browser_skill.set_extension_version(str(device["id"]), lease_key, version)
        await websocket.send_text(reply)
        live = browser_skill.LiveConnection(scope, str(device["id"]), lease_key, upstream)
        browser_skill.register_live(live, reserved=True)
        reserved = False

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive_text()
                if browser_skill.observe_event(scope, message):
                    continue
                await live.send(message)

        async def upstream_to_client() -> None:
            while True:
                message = await upstream.recv()
                if isinstance(message, str):
                    if browser_skill.receive_ui(scope, message):
                        continue
                    await websocket.send_text(message)

        heartbeat_task = asyncio.create_task(
            browser_skill.heartbeat_until_lost(str(device["id"]), lease_key)
        )
        pumps = {
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
            heartbeat_task,
        }
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pumps:
            task.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)
    finally:
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
        if browser_skill.release(str(device["id"]), lease_key):
            browser_skill.clear_tasks(scope)
        if live is not None:
            browser_skill.unregister_live(live)
        else:
            try:
                await upstream.close()
            except Exception:  # noqa: BLE001 - best-effort transport cleanup
                pass
        if reserved:
            browser_skill.release_connection_reservation()
        await upstream.close()


def _extract_knowledge_ids(
    payload: dict[str, Any],
    session: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> list[str]:
    values = payload.get("knowledge_ids") or []
    if not values:
        config = config if config is not None else loads(session.get("agent_config"), {})
        values = config.get("knowledge_ids") or []
    return [str(value.get("id") if isinstance(value, dict) else value) for value in values if value]


def _claim_steer_follow_up(
    *,
    session_id: str,
    tenant_id: int,
    assistant_message_id: str,
    default_channel: str,
) -> dict[str, Any] | None:
    """Persist the next turn and rebind any remaining steer messages to it."""
    rows = db.fetch_all(
        """
        SELECT * FROM session_steer_messages
        WHERE tenant_id=? AND session_id=? AND assistant_message_id=? AND status='queued'
        ORDER BY created_at, rowid
        """,
        (tenant_id, session_id, assistant_message_id),
    )
    if not rows:
        return None
    first = rows[0]
    query = str(first.get("content") or "").strip()
    if not query:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE session_steer_messages SET status='canceled', updated_at=? WHERE id=?",
                (db.utcnow(), first["id"]),
            )
        return _claim_steer_follow_up(
            session_id=session_id,
            tenant_id=tenant_id,
            assistant_message_id=assistant_message_id,
            default_channel=default_channel,
        )

    request_id = db.new_id()
    channel = str(first.get("channel") or default_channel or "web")
    user_message_id = _create_message(
        session_id=session_id,
        role="user",
        content=query,
        request_id=request_id,
        tenant_id=tenant_id,
        channel=channel,
    )
    next_assistant_id = db.new_id()
    _create_message(
        session_id=session_id,
        role="assistant",
        content="",
        request_id=request_id,
        tenant_id=tenant_id,
        channel=channel,
        is_completed=False,
        message_id=next_assistant_id,
    )
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE session_steer_messages SET status='injected', user_message_id=?, updated_at=? WHERE id=?",
            (user_message_id, now, first["id"]),
        )
        conn.execute(
            "UPDATE session_steer_messages SET assistant_message_id=?, updated_at=? "
            "WHERE tenant_id=? AND session_id=? AND assistant_message_id=? AND status='queued'",
            (next_assistant_id, now, tenant_id, session_id, assistant_message_id),
        )
    return {
        "request_id": request_id,
        "user_message_id": user_message_id,
        "assistant_message_id": next_assistant_id,
        "query": query,
        "channel": channel,
        "mentioned_items": loads(first.get("mentioned_items"), []),
    }


def _launch_steer_follow_up(
    follow_up: dict[str, Any],
    *,
    session: dict[str, Any],
    session_id: str,
    tenant_id: int,
    user_id: str | None,
    preferences: Any,
    memory_allowed: bool,
    auth_ctx: AuthContext | None = None,
) -> None:
    async def run() -> None:
        try:
            current = db.fetch_one(
                "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (session_id, tenant_id),
            )
            if not current:
                return
            async for _event in stream_chat_events(
                session=current,
                session_id=session_id,
                tenant_id=tenant_id,
                payload={
                    "query": follow_up["query"],
                    "channel": follow_up["channel"],
                    "mentioned_items": follow_up.get("mentioned_items") or [],
                    "agent_enabled": True,
                },
                agent_mode=True,
                channel=follow_up["channel"],
                user_id=user_id,
                preferences=preferences,
                memory_allowed=memory_allowed,
                precreated_user_message_id=follow_up["user_message_id"],
                precreated_assistant_message_id=follow_up["assistant_message_id"],
                precreated_request_id=follow_up["request_id"],
                auth_ctx=auth_ctx,
            ):
                pass
        except BaseException:  # noqa: BLE001 - background task must not die silently
            logger.exception("steer follow-up run failed for session %s", session_id)

    task = asyncio.create_task(run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stream_chat_events(
    *,
    session: dict[str, Any],
    session_id: str,
    tenant_id: int,
    payload: dict[str, Any],
    agent_mode: bool,
    channel: str,
    user_id: str | None = None,
    preferences: Any = None,
    memory_allowed: bool = True,
    auth_ctx: AuthContext | None = None,
    precreated_user_message_id: str | None = None,
    precreated_assistant_message_id: str | None = None,
    precreated_request_id: str | None = None,
    locale: str = "zh-CN",
    noninteractive: bool = False,
) -> AsyncIterator[bytes]:
    agent_config = resolve_config(session, tenant_id)
    chat_model = model_runtime.chat_runtime(tenant_id, agent_config, payload)
    if isinstance(agent_config, dict) and agent_config.get("memory_enabled") is False:
        memory_allowed = False
    user_input = str(payload.get("query") or "").strip()
    image_records = _request_images(payload)
    inline_attachments = _inline_attachments(payload)
    attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    attachments = [*attachments, *inline_attachments]
    images = [image["data"] for image in image_records]
    query = user_input or (_upload_only_question(locale) if images or inline_attachments else "")
    if not query:
        raise validation("query is required")
    reset_turn_stats()
    started = time.monotonic()
    request_id = precreated_request_id or db.new_id()
    assistant_id = precreated_assistant_message_id or db.new_id()
    user_message_id = precreated_user_message_id or _create_message(
        session_id=session_id,
        role="user",
        content=user_input,
        request_id=request_id,
        tenant_id=tenant_id,
        channel=channel,
        images=image_records,
        attachments=attachments,
    )
    if not precreated_assistant_message_id:
        _create_message(
            session_id=session_id,
            role="assistant",
            content="",
            request_id=request_id,
            tenant_id=tenant_id,
            channel=channel,
            is_completed=False,
            message_id=assistant_id,
        )
    memory = (
        recall_memory(tenant_id, user_id, query, preferences=preferences)
        if user_id and memory_allowed
        else {"prompt": "", "items": []}
    )
    history = _history(session_id, locale=locale)
    kb_ids = _session_knowledge_bases(
        session, payload.get("knowledge_base_ids"), agent_config, auth_ctx
    )
    knowledge_ids = _extract_knowledge_ids(payload, session, agent_config)
    attachment_prompt = im_attachments.build_prompt(attachments)
    intent = ""
    retrieval_query = query
    if truthy(agent_config.get("enable_rewrite")) or images:
        understand_runtime = chat_model
        query_understand_model_id = str(
            agent_config.get("query_understand_model_id") or ""
        ).strip()
        if query_understand_model_id:
            candidate = model_runtime.chat_runtime(
                tenant_id, {"model_id": query_understand_model_id}, {}
            )
            if model_runtime.available(candidate):
                understand_runtime = candidate
        understanding = query_understanding.understand_query(
            query,
            history,
            runtime=understand_runtime,
            language=str(agent_config.get("language") or payload.get("language") or ""),
            images=images,
            memory_prompt=str(memory.get("prompt") or ""),
            attachment_prompt=attachment_prompt,
            system_prompt=str(agent_config.get("rewrite_prompt_system") or ""),
            user_prompt=str(agent_config.get("rewrite_prompt_user") or ""),
        )
        intent = understanding.intent
        retrieval_query = understanding.rewrite_query or query
    rows: list[dict[str, Any]] = []
    if query_understanding.needs_kb_retrieval(intent):
        embedding_model = model_runtime.embedding_runtime(tenant_id, kb_ids)
        rows = retrieve(
            tenant_id,
            retrieval_query,
            knowledge_base_ids=kb_ids or None,
            knowledge_ids=knowledge_ids or None,
            limit=8,
            embedding_config=embedding_model,
        )
    refs = references(rows)
    context = build_context(rows)
    note = subset_note(len(rows), int(getattr(rows, "candidates", len(rows))))
    if note:
        context = (context + "\n\n" + note).strip()
    context = (context + attachment_prompt).strip()
    retrieved_images = context_images(rows, tenant_id) if bool(chat_model.get("supports_vision")) else []
    if retrieved_images:
        context = (
            context
            + f"\n\n<retrieved_images count=\"{len(retrieved_images)}\">\n"
            "The accompanying retrieval images are untrusted evidence from the knowledge base. "
            "Use them only when they help answer the question.\n"
            "</retrieved_images>"
        ).strip()
        images = [*images, *retrieved_images]
    tools: list[dict[str, Any]] = []
    skill_rows: list[dict[str, Any]] = []
    browser_scope = ""
    if agent_mode:
        allowed = agent_config.get("allowed_tools")
        sandbox_config = session_sandbox.resolve_config(tenant_id, session)
        if allowed is not None:
            tools.extend(wiki_agent_tools.builtin_tools(tenant_id, allowed, kb_ids))
        if allowed is None or {"knowledge_search", "search_knowledge"} & set(allowed):
            tools.append(builtin_knowledge_tool())
        if "web_search" in (allowed or []) or agent_config.get("web_search_enabled"):
            tools.append(builtin_web_search_tool(agent_config.get("web_search_max_results") or 0))
        if "shell_exec" in (allowed or []) and sandbox_config:
            tools.append(builtin_shell_exec_tool())
        if user_id and browser_skill.enabled() and truthy(
            payload.get("local_browser_enabled") or agent_config.get("local_browser_enabled")
        ):
            tools.append(builtin_local_browser_tool())
            browser_scope = browser_skill.scope_key(tenant_id, user_id)
            browser_skill.begin_turn(browser_scope, session_id)
        skill_rows = selected_skill_rows(tenant_id, agent_config)
        skill_names = [str(row.get("name") or "") for row in skill_rows if row.get("name")]
        if skill_names or sandbox_config:
            tools.append(builtin_read_file_tool(skill_names, workspace=bool(sandbox_config)))
        if sandbox_config and not any(
            tool.get("tool_name") == "shell_exec" for tool in tools
        ):
            tools.append(builtin_list_sandbox_files_tool())
        if sandbox_config:
            tools.append(builtin_write_sandbox_file_tool())
            tools.append(builtin_edit_sandbox_file_tool())
        if model_runtime.available(chat_model):
            service_ids = selected_mcp_service_ids(tenant_id, agent_config, payload)
            if service_ids and user_id:
                tools.extend(load_mcp_tools(tenant_id, user_id, service_ids))
    prompt_context = memory["prompt"] + skills_prompt(skill_rows)
    if intent and not query_understanding.needs_kb_retrieval(intent):
        override = query_understanding.intent_system_prompt(intent)
        if override:
            prompt_context = override + "\n\n" + prompt_context

    title = (session.get("title") or "").strip()
    if not title or title in {"新会话", "New chat"}:
        title = re.sub(r"\s+", " ", query).strip()[:40]

    def frame(value: dict[str, Any]) -> bytes:
        return ("data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n").encode("utf-8")

    agent_queue: asyncio.Queue[tuple[str, Any]] | None = None
    agent_task: asyncio.Task[None] | None = None
    answer = ""
    tool_steps: list[dict[str, Any]] = []
    approval_user_id = user_id or ""
    allowed_skills = [str(row.get("name") or "") for row in skill_rows if row.get("name")]
    steer_persist_failures: dict[str, int] = {}

    async def emit(step: dict[str, Any]) -> None:
        assert agent_queue is not None
        await agent_queue.put(("event", step))

    def service_name(service_id: str) -> str:
        row = db.fetch_one(
            "SELECT name FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (service_id, tenant_id),
        ) or {}
        return str(row.get("name") or service_id)

    async def run_tool(tool: dict[str, Any], arguments: dict[str, Any], tool_call_id: str) -> dict[str, Any]:
        description = str(tool.get("function", {}).get("description") or "")

        def call(tool_arguments: dict[str, Any], skip_approval: bool) -> dict[str, Any]:
            tool_for_call = (
                {**tool, "_session_id": session_id}
                if tool.get("tool_name") in {
                    "list_sandbox_files",
                    "local_browser",
                    "read_file",
                    "write_sandbox_file",
                    "edit_sandbox_file",
                    "shell_exec",
                }
                else tool
            )
            return execute_tool(
                tool_for_call,
                tool_arguments,
                tenant_id=tenant_id,
                user_id=approval_user_id,
                knowledge_base_ids=kb_ids or None,
                allowed_skills=allowed_skills,
                skip_approval=skip_approval,
            )

        try:
            try:
                return call(arguments, False)
            except ToolApprovalRequired as exc:
                if noninteractive:
                    return {
                        "success": False,
                        "output": (
                            f"tool {exc.name} requires human approval, but this channel "
                            "cannot ask for it"
                        ),
                        "duration_ms": 0,
                    }
                pending = approvals.request_tool(
                    tenant_id=tenant_id,
                    user_id=approval_user_id,
                    session_id=session_id,
                    assistant_message_id=assistant_id,
                    request_id=request_id,
                    service_id=exc.service_id,
                    service_name=service_name(exc.service_id),
                    mcp_tool_name=exc.tool_name,
                    registered_tool_name=exc.name,
                    description=description,
                    arguments=exc.arguments,
                    tool_call_id=tool_call_id,
                )
                await emit({"type": "tool_approval_required", **pending.public()})
                decision = await approvals.wait(pending)
                await emit(
                    {
                        "type": "tool_approval_resolved",
                        "pending_id": pending.id,
                        "approved": decision.approved,
                        "reason": decision.reason,
                        "timed_out": decision.timed_out,
                        "canceled": decision.canceled,
                    }
                )
                if not decision.approved:
                    return {
                        "success": False,
                        "output": decision.reason or "tool execution rejected by user",
                        "duration_ms": 0,
                    }
                return call(decision.modified_args or exc.arguments, True)
        except MCPOAuthRequired as exc:
            if noninteractive:
                return {
                    "success": False,
                    "output": (
                        f"tool {exc.name} requires MCP OAuth authorization, but this "
                        "channel cannot ask for it"
                    ),
                    "duration_ms": 0,
                }
            pending = approvals.request_oauth(
                tenant_id=tenant_id,
                user_id=approval_user_id,
                session_id=session_id,
                assistant_message_id=assistant_id,
                request_id=request_id,
                service_id=exc.service_id,
                service_name=service_name(exc.service_id),
                mcp_tool_name=exc.tool_name,
                tool_call_id=tool_call_id,
            )
            await emit({"type": "mcp_oauth_required", **pending.public()})
            decision = await approvals.wait(pending)
            await emit(
                {
                    "type": "mcp_oauth_resolved",
                    "pending_id": pending.id,
                    "service_id": exc.service_id,
                    "authorized": decision.approved,
                    "reason": decision.reason,
                    "timed_out": decision.timed_out,
                    "canceled": decision.canceled,
                }
            )
            if not decision.approved:
                return {
                    "success": False,
                    "output": decision.reason or "MCP OAuth authorization canceled",
                    "duration_ms": 0,
                }
            if not mcp_oauth.is_authorized(tenant_id, approval_user_id, exc.service_id):
                return {
                    "success": False,
                    "output": "MCP OAuth authorization is not available for this service.",
                    "duration_ms": 0,
                }
            try:
                return call(exc.arguments, True)
            except MCPOAuthRequired:
                return {
                    "success": False,
                    "output": "MCP service still requires OAuth authorization after the prompt was resolved.",
                    "duration_ms": 0,
                }

    async def poll_steer() -> list[dict[str, Any]]:
        rows = db.fetch_all(
            """
            SELECT * FROM session_steer_messages
            WHERE tenant_id=? AND session_id=? AND assistant_message_id=?
              AND status='queued' AND delivery='inject'
            ORDER BY created_at, rowid LIMIT 20
            """,
            (tenant_id, session_id, assistant_id),
        )
        injected: list[dict[str, Any]] = []
        for row in rows:
            mentioned = loads(row.get("mentioned_items"), [])
            user_message_id = db.new_id()
            try:
                with db.transaction() as conn:
                    cursor = conn.execute(
                        "UPDATE session_steer_messages SET status='injected', user_message_id=?, "
                        "updated_at=? WHERE id=? AND status='queued'",
                        (user_message_id, db.utcnow(), row["id"]),
                    )
                    if int(cursor.rowcount or 0) == 0:
                        continue
                    _insert_message(
                        conn,
                        session_id=session_id,
                        role="user",
                        content=str(row.get("content") or ""),
                        request_id=request_id,
                        tenant_id=tenant_id,
                        channel=str(row.get("channel") or channel),
                        mentioned_items=mentioned if isinstance(mentioned, list) else [],
                        message_id=user_message_id,
                    )
            except Exception:  # noqa: BLE001 - keep the queue ordered on any persistence failure
                attempts = steer_persist_failures.get(str(row["id"]), 0) + 1
                steer_persist_failures[str(row["id"])] = attempts
                logger.warning(
                    "steer persist failed for session %s steer %s (attempt %d)",
                    session_id,
                    row["id"],
                    attempts,
                    exc_info=True,
                )
                if attempts >= MAX_STEER_PERSIST_ATTEMPTS:
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE session_steer_messages SET status='dropped', updated_at=? "
                            "WHERE id=? AND status='queued'",
                            (db.utcnow(), row["id"]),
                        )
                    steer_persist_failures.pop(str(row["id"]), None)
                    continue
                break
            event = {
                "type": "user_message_injected",
                "steer_id": row["id"],
                "user_message_id": user_message_id,
                "content": str(row.get("content") or ""),
                "delivery": "inject",
                "channel": str(row.get("channel") or channel),
                "mentioned_items": mentioned if isinstance(mentioned, list) else [],
                "created_at": row.get("created_at") or db.utcnow(),
            }
            await emit(event)
            injected.append({"content": event["content"]})
        return injected

    if tools and model_runtime.available(chat_model):
        agent_queue = asyncio.Queue()

        async def generate_with_tools() -> None:
            assert agent_queue is not None
            completed = False
            try:
                result = await answer_with_tools_async(
                    query,
                    context,
                    rows,
                    history,
                    prompt_context,
                    tools,
                    run_tool,
                    emit=emit,
                    before_round=poll_steer,
                    runtime=chat_model,
                    images=images,
                )
                completed = True
                await agent_queue.put(("done", result))
            except BaseException as exc:  # noqa: BLE001
                await agent_queue.put(("error", exc))
            finally:
                if browser_scope:
                    try:
                        await asyncio.shield(
                            anyio.to_thread.run_sync(
                                lambda: browser_skill.finish_turn(
                                    browser_scope, session_id, keep_open=not completed
                                )
                            )
                        )
                    except BaseException:  # noqa: BLE001
                        pass

        agent_task = asyncio.create_task(generate_with_tools())
    else:
        answer = generate_answer(
            query,
            context,
            rows,
            history,
            prompt_context,
            runtime=chat_model,
            images=images,
        )

    if agent_mode:
        yield frame(
            {
                "id": request_id,
                "response_type": "agent_query",
                "content": "",
                "done": False,
                "session_id": session_id,
                "assistant_message_id": assistant_id,
                "data": {"query": query, "request_id": request_id, "session_id": session_id, "user_message_id": user_message_id},
            }
        )
    if refs:
        yield frame({"id": request_id, "response_type": "references", "content": "", "done": False, "session_id": session_id, "assistant_message_id": assistant_id, "knowledge_references": refs, "data": {"references": refs, "knowledge_references": refs}})
    if memory["items"]:
        yield frame({"id": request_id, "response_type": "memory_recalled", "content": "", "done": False, "session_id": session_id, "assistant_message_id": assistant_id, "data": {"memories": memory["items"]}})
    if agent_queue is not None:
        try:
            while True:
                kind, value = await agent_queue.get()
                if kind == "event":
                    step = value
                    step_type = str(step.get("type") or "tool_event")
                    content = str(step.get("content") or "")
                    if step_type == "tool_call":
                        content = f"Calling tool: {step.get('tool_name') or ''}"
                    elif step_type == "tool_result":
                        content = ""
                        if not bool(step.get("success", True)):
                            content = str(step.get("error") or step.get("output") or "")
                    elif step_type == "tool_approval_required":
                        content = "MCP tool requires human approval"
                    elif step_type == "tool_approval_resolved":
                        content = "MCP tool approval resolved"
                    elif step_type == "mcp_oauth_required":
                        content = "MCP service requires OAuth authorization"
                    elif step_type == "mcp_oauth_resolved":
                        content = "MCP OAuth authorization resolved"
                    elif step_type == "user_message_injected":
                        content = ""
                    yield frame(
                        {
                            "id": request_id,
                            "response_type": step_type,
                            "content": content,
                            "done": step_type
                            in {
                                "tool_approval_required",
                                "tool_approval_resolved",
                                "mcp_oauth_required",
                                "mcp_oauth_resolved",
                                "user_message_injected",
                            },
                            "session_id": session_id,
                            "assistant_message_id": assistant_id,
                            "data": step,
                        }
                    )
                elif kind == "done":
                    answer, tool_steps = value
                    break
                else:
                    error = str(value) or "agent execution failed"
                    yield frame(
                        {
                            "id": request_id,
                            "response_type": "error",
                            "content": error,
                            "done": True,
                            "session_id": session_id,
                            "assistant_message_id": assistant_id,
                            "data": {"stage": "agent_execution", "error": error},
                        }
                    )
                    return
        finally:
            if agent_task is not None:
                if not agent_task.done():
                    agent_task.cancel()
                await asyncio.gather(agent_task, return_exceptions=True)
    thinking_id = "thinking-1"
    yield frame({"id": request_id, "response_type": "thinking", "content": f"检索到 {len(refs)} 条相关资料", "done": False, "session_id": session_id, "assistant_message_id": assistant_id, "data": {"event_id": thinking_id}})
    yield frame({"id": request_id, "response_type": "thinking", "content": "", "done": True, "session_id": session_id, "assistant_message_id": assistant_id, "data": {"event_id": thinking_id, "duration_ms": 0, "completed_at": int(time.time())}})
    cursor = 0
    while cursor < len(answer):
        piece = answer[cursor:cursor + 16]
        cursor += len(piece)
        yield frame({"id": request_id, "response_type": "answer", "content": piece, "done": False, "session_id": session_id, "assistant_message_id": assistant_id, "data": {"event_id": "answer-1"}})
        await asyncio.sleep(0.008)
    yield frame({"id": request_id, "response_type": "answer", "content": "", "done": True, "session_id": session_id, "assistant_message_id": assistant_id, "data": {"event_id": "answer-1", "done": True}})
    follow_up = _claim_steer_follow_up(
        session_id=session_id,
        tenant_id=tenant_id,
        assistant_message_id=assistant_id,
        default_channel=channel,
    )
    _update_message(
        assistant_id,
        content=answer,
        knowledge_references=refs,
        used_memories=memory["items"],
    )
    if user_id and memory_allowed:
        statement = detect_explicit_memory(query)
        if statement:
            remember_explicit(
                tenant_id,
                user_id,
                statement,
                session_id=session_id,
                message_id=user_message_id,
                preferences=preferences,
            )
        else:
            auto_remember(
                tenant_id,
                user_id,
                query,
                session_id=session_id,
                message_id=user_message_id,
                preferences=preferences,
            )
    if title and title != session.get("title"):
        with db.transaction() as conn:
            conn.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title, db.utcnow(), session_id))
        yield frame({"id": request_id, "response_type": "session_title", "content": title, "done": True, "data": {"session_id": session_id, "title": title}})
    if follow_up:
        _launch_steer_follow_up(
            follow_up,
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            preferences=preferences,
            memory_allowed=memory_allowed,
            auth_ctx=auth_ctx,
        )
    stats = current_turn_stats()
    usage = (
        {
            "prompt_tokens": stats["prompt_tokens"],
            "completion_tokens": stats["completion_tokens"],
            "total_tokens": stats["total_tokens"],
        }
        if stats and stats["total_tokens"] > 0
        else None
    )
    complete_data: dict[str, Any] = {
        "final_content": answer,
        "total_steps": stats["rounds"] if stats else int(bool(answer)),
        "total_duration_ms": int((time.monotonic() - started) * 1000),
    }
    if usage:
        complete_data["usage"] = usage
    complete_frame: dict[str, Any] = {
        "id": request_id,
        "response_type": "complete",
        "content": "",
        "done": True,
        "session_id": session_id,
        "assistant_message_id": assistant_id,
        "data": complete_data,
    }
    if usage:
        complete_frame["usage"] = usage
    yield frame(complete_frame)


@router.post("/knowledge-chat/{session_id}")
@router.post("/agent-chat/{session_id}")
async def chat(session_id: str, request: Request) -> StreamingResponse:
    ctx = parse_auth(request)
    assert ctx is not None
    session = _session(ctx, session_id)
    payload = await body(request)
    query = str(payload.get("query") or "").strip()
    if not query and not _payload_has_upload(payload):
        raise validation("query is required")
    if not _valid_suggestion_attribution(
        ctx.tenant_id, session_id, query, payload.get("suggestion_attribution")
    ):
        raise validation("invalid suggestion attribution")
    agent_mode = request.url.path.endswith("/agent-chat/" + session_id) or bool(payload.get("agent_enabled", True))
    locale = resolve_locale(request.headers.get("accept-language", ""))
    events = stream_chat_events(
        session=session,
        session_id=session_id,
        tenant_id=ctx.tenant_id,
        payload=payload,
        agent_mode=agent_mode,
        channel=str(payload.get("channel") or "web"),
        user_id=ctx.user_id,
        preferences=ctx.user.get("preferences"),
        auth_ctx=ctx,
        locale=locale,
    )
    return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/sessions/{session_id}/artifacts")
def list_session_artifacts(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    rows = db.fetch_all("SELECT * FROM message_artifacts WHERE session_id=? AND deleted_at IS NULL ORDER BY created_at", (session_id,))
    return ok(rows)


@router.get("/sessions/{session_id}/messages/{message_id}/artifacts")
def list_message_artifacts(session_id: str, message_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    rows = db.fetch_all("SELECT * FROM message_artifacts WHERE session_id=? AND message_id=? AND deleted_at IS NULL ORDER BY position", (session_id, message_id))
    return ok(rows)


@router.get("/sessions/{session_id}/messages/{message_id}/artifacts/{index}/download")
def download_artifact(session_id: str, message_id: str, index: int, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    row = db.fetch_one("SELECT * FROM message_artifacts WHERE session_id=? AND message_id=? AND position=? AND deleted_at IS NULL", (session_id, message_id, index))
    if not row:
        raise not_found("Artifact not found")
    from pathlib import Path

    if row.get("source_path") and Path(row["source_path"]).exists():
        return FileResponse(row["source_path"], filename=row.get("file_name") or Path(row["source_path"]).name)
    raise not_found("Artifact file is no longer available")


@router.delete("/sessions/{session_id}/messages/{message_id}/artifacts/{index}")
def delete_artifact(session_id: str, message_id: str, index: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    with db.transaction() as conn:
        conn.execute("UPDATE message_artifacts SET deleted_at=? WHERE session_id=? AND message_id=? AND position=?", (db.utcnow(), session_id, message_id, index))
    return ok(message="Artifact deleted")


@router.get("/artifacts")
def list_artifact_library(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    page_no, page_size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 100)
    clauses = [
        "s.tenant_id=?",
        "s.deleted_at IS NULL",
        "ma.deleted_at IS NULL",
        "m.deleted_at IS NULL",
        "(s.user_id=? OR s.user_id IS NULL OR s.user_id='')",
    ]
    args: list[Any] = [ctx.tenant_id, ctx.user_id]
    keyword = (request.query_params.get("keyword") or "").strip()
    if keyword:
        escaped = keyword.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("LOWER(ma.file_name) LIKE ? ESCAPE '\\'")
        args.append(f"%{escaped}%")
    file_types = [
        value.strip().lower()
        for value in (request.query_params.get("file_types") or "").split(",")
        if value.strip()
    ]
    if file_types:
        clauses.append("LOWER(ma.file_type) IN (" + ",".join("?" for _ in file_types) + ")")
        args.extend(file_types)
    where = " AND ".join(clauses)
    grouped = f"""
        SELECT ma.id, ma.session_id, ma.message_id, ma.position, ma.url,
               ma.file_name, ma.file_type, ma.file_size, ma.source_path,
               ma.created_at, s.title AS session_title,
               CASE WHEN ma.source_path='' THEN ma.id ELSE ma.source_path END AS version_key,
               ROW_NUMBER() OVER (
                 PARTITION BY ma.session_id,
                   CASE WHEN ma.source_path='' THEN ma.id ELSE ma.source_path END
                 ORDER BY ma.created_at DESC, m.created_at DESC, ma.position DESC
               ) AS version_rank,
               DENSE_RANK() OVER (
                 PARTITION BY ma.session_id,
                   CASE WHEN ma.source_path='' THEN ma.id ELSE ma.source_path END
                 ORDER BY ma.url
               ) AS url_rank
        FROM message_artifacts ma
        JOIN messages m ON m.id=ma.message_id AND m.deleted_at IS NULL
        JOIN sessions s ON s.id=ma.session_id
        WHERE {where}
    """
    ranked = f"""
        SELECT g.*, MAX(g.url_rank) OVER (PARTITION BY g.session_id, g.version_key) AS version_count
        FROM ({grouped}) g
    """
    total = int(db.fetch_one(f"SELECT COUNT(*) AS n FROM ({ranked}) latest WHERE version_rank=1", args)["n"])
    rows = db.fetch_all(
        f"""
        SELECT * FROM ({ranked}) latest
        WHERE version_rank=1
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*args, page_size, offset],
    )
    data = []
    for row in rows:
        data.append(
            {
                "session_id": row["session_id"],
                "session_title": row.get("session_title") or "",
                "message_id": row["message_id"],
                "index": int(row.get("position") or 0),
                "file_name": row.get("file_name") or "",
                "file_type": row.get("file_type") or "",
                "file_size": int(row.get("file_size") or 0),
                "source_path": row.get("source_path") or "",
                "created_at": row.get("created_at"),
                "version_count": int(row.get("version_count") or 0),
            }
        )
    return {"success": True, "data": data, "total": total, "page": page_no, "page_size": page_size}


def _artifact_all_versions(raw: str | None, default: bool) -> bool:
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "t", "yes", "y"}:
        return True
    if value in {"0", "false", "f", "no", "n"}:
        return False
    return default


@router.delete("/artifacts")
def delete_artifact_library_item(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    session_id = (request.query_params.get("session_id") or "").strip()
    message_id = (request.query_params.get("message_id") or "").strip()
    try:
        index = int(request.query_params.get("index") or "")
    except ValueError as exc:
        raise validation("invalid artifact index") from exc
    if index < 0:
        raise validation("invalid artifact index")
    if not session_id or not message_id:
        raise validation("session_id and message_id are required")
    _session(ctx, session_id)
    target = db.fetch_one(
        """
        SELECT ma.* FROM message_artifacts ma
        JOIN messages m ON m.id=ma.message_id AND m.deleted_at IS NULL
        WHERE ma.session_id=? AND ma.message_id=? AND ma.position=?
          AND ma.deleted_at IS NULL
        """,
        (session_id, message_id, index),
    )
    if not target:
        raise not_found("Artifact not found")
    all_versions = _artifact_all_versions(request.query_params.get("all_versions"), True)
    source_path = target.get("source_path") or ""
    if all_versions and source_path:
        versions = db.fetch_all(
            """
            SELECT ma.* FROM message_artifacts ma
            JOIN messages m ON m.id=ma.message_id AND m.deleted_at IS NULL
            WHERE ma.session_id=? AND ma.source_path=? AND ma.deleted_at IS NULL
            """,
            (session_id, source_path),
        )
    else:
        versions = [target]
    deleted = 0
    now = db.utcnow()
    with db.transaction() as conn:
        for version in versions:
            cursor = conn.execute(
                "UPDATE message_artifacts SET deleted_at=? WHERE session_id=? AND message_id=? AND position=? AND deleted_at IS NULL",
                (now, session_id, version["message_id"], version["position"]),
            )
            deleted += int(cursor.rowcount or 0)
    return {"success": True, "data": {"file_name": target.get("file_name") or "", "deleted": deleted}}


@router.get("/sessions/{session_id}/messages/{message_id}/suggestions")
def get_suggestions(session_id: str, message_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    row = db.fetch_one("SELECT * FROM message_suggestion_sets WHERE session_id=? AND assistant_message_id=?", (session_id, message_id))
    if not row:
        return ok({"id": "", "session_id": session_id, "assistant_message_id": message_id, "status": "failed", "allow_regenerate": True, "questions": []})
    row["questions"] = loads(row.get("questions"), [])
    return ok(row)


@router.post("/sessions/{session_id}/messages/{message_id}/suggestions")
async def ensure_suggestions(session_id: str, message_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    message = db.fetch_one("SELECT content FROM messages WHERE id=? AND session_id=?", (message_id, session_id))
    base = (message.get("content") if message else "")[:80]
    questions = [
        {"id": "q1", "text": "能否总结一下关键结论？", "category": "deepen", "source": "document"},
        {"id": "q2", "text": "有哪些可执行的下一步？", "category": "action", "source": "document"},
        {"id": "q3", "text": "相关资料还可以从哪里查看？", "category": "clarify", "source": "wiki"},
    ]
    set_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("DELETE FROM message_suggestion_sets WHERE session_id=? AND assistant_message_id=?", (session_id, message_id))
        conn.execute(
            """
            INSERT INTO message_suggestion_sets(id, tenant_id, session_id, assistant_message_id,
              agent_id, agent_tenant_id, placement, config_hash, locale, status,
              allow_regenerate, questions, model_id, created_at, updated_at, generated_at)
            VALUES (?, ?, ?, ?, '', ?, 'inline', '', 'zh-CN', 'ready', 1, ?, '', ?, ?, ?)
            """,
            (set_id, ctx.tenant_id, session_id, message_id, ctx.tenant_id, dumps(questions), now, now, now),
        )
    return ok({"id": set_id, "session_id": session_id, "assistant_message_id": message_id, "status": "ready", "allow_regenerate": True, "questions": questions, "generated_at": now})


@router.post("/sessions/{session_id}/suggestion-events")
async def suggestion_event(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    data = await body(request)
    suggestion_set_id = str(data.get("suggestion_set_id") or "").strip()
    if not suggestion_set_id:
        raise validation("suggestion_set_id is required")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO message_suggestion_events(tenant_id, session_id, suggestion_set_id, question_id, event_type, actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ctx.tenant_id, session_id, suggestion_set_id, data.get("question_id") or "", data.get("event_type") or "impression", ctx.user_id, db.utcnow()),
        )
    return ok(message="Recorded")


@router.post("/sessions/{session_id}/steer")
async def steer(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    data = await body(request)
    query = str(data.get("query") or data.get("content") or "").replace("\x00", "").strip()
    if not query:
        raise validation("query must not be empty")
    if len(query) > 10000:
        raise validation("query too long")
    delivery = str(data.get("delivery") or "after").strip().lower()
    if delivery not in {"inject", "after"}:
        raise validation("delivery must be inject or after")
    steer_id = str(data.get("steer_id") or "").strip() or db.new_id()
    try:
        uuid.UUID(steer_id)
    except ValueError as exc:
        raise validation("invalid steer_id") from exc
    assistant_id = _live_assistant_id(session_id)
    if not assistant_id:
        return ok({"status": "new_run"})
    expected = str(data.get("expected_assistant_message_id") or "").strip()
    if expected and expected != assistant_id:
        raise conflict("The active turn changed; retry the message")
    existing = db.fetch_one(
        "SELECT * FROM session_steer_messages WHERE id=? AND tenant_id=? AND session_id=?",
        (steer_id, ctx.tenant_id, session_id),
    )
    if existing:
        if str(existing.get("content") or "") != query:
            raise conflict("steer_id already belongs to another message")
        return ok(
            {
                "status": "already_injected" if existing.get("status") in STEER_CONSUMED_STATUSES else "queued",
                "steer_id": steer_id,
                "delivery": existing.get("delivery") or delivery,
                "assistant_message_id": existing.get("assistant_message_id") or assistant_id,
            }
        )
    pending = db.fetch_one(
        "SELECT COUNT(*) AS n FROM session_steer_messages WHERE tenant_id=? AND session_id=? AND assistant_message_id=? AND status='queued'",
        (ctx.tenant_id, session_id, assistant_id),
    )["n"]
    if int(pending) >= 10:
        raise validation("too many queued messages for the running turn")
    now = db.utcnow()
    mentioned = data.get("mentioned_items")
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO session_steer_messages(id, tenant_id, session_id, assistant_message_id,
              content, delivery, mentioned_items, channel, status, user_message_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', '', ?, ?)
            """,
            (
                steer_id, ctx.tenant_id, session_id, assistant_id, query, delivery,
                dumps(mentioned if isinstance(mentioned, list) else []),
                str(data.get("channel") or "web"), now, now,
            ),
        )
    return ok(
        {
            "status": "queued",
            "steer_id": steer_id,
            "delivery": delivery,
            "assistant_message_id": assistant_id,
        }
    )


@router.get("/sessions/{session_id}/steer")
def list_steer(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    assistant_id = _live_assistant_id(session_id)
    if not assistant_id:
        return ok({"assistant_message_id": "", "items": []})
    rows = db.fetch_all(
        """
        SELECT * FROM session_steer_messages
        WHERE tenant_id=? AND session_id=? AND assistant_message_id=? AND status='queued'
        ORDER BY created_at, rowid
        """,
        (ctx.tenant_id, session_id, assistant_id),
    )
    return ok(
        {
            "assistant_message_id": assistant_id,
            "items": [
                {
                    "steer_id": row["id"],
                    "content": row.get("content") or "",
                    "delivery": row.get("delivery") or "after",
                    "mentioned_items": loads(row.get("mentioned_items"), []),
                }
                for row in rows
            ],
        }
    )


@router.delete("/sessions/{session_id}/steer/{steer_id}")
def delete_steer(session_id: str, steer_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    assistant_id = _live_assistant_id(session_id)
    if not assistant_id:
        return ok({"status": "gone", "removed": False, "steer_id": steer_id})
    row = db.fetch_one(
        "SELECT * FROM session_steer_messages WHERE id=? AND tenant_id=? AND session_id=? AND assistant_message_id=?",
        (steer_id, ctx.tenant_id, session_id, assistant_id),
    )
    if not row:
        return ok({"status": "gone", "removed": False, "steer_id": steer_id})
    if row.get("status") in STEER_CONSUMED_STATUSES:
        return ok({"status": "already_injected", "removed": False, "steer_id": steer_id})
    with db.transaction() as conn:
        conn.execute(
            "UPDATE session_steer_messages SET status='deleted', updated_at=? WHERE id=?",
            (db.utcnow(), steer_id),
        )
    return ok({"status": "deleted", "removed": True, "steer_id": steer_id})


@router.post("/sessions/{session_id}/steer/{steer_id}/inject")
def inject_steer(session_id: str, steer_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    assistant_id = _live_assistant_id(session_id)
    if not assistant_id:
        return ok({"status": "new_run"})
    row = db.fetch_one(
        "SELECT * FROM session_steer_messages WHERE id=? AND tenant_id=? AND session_id=? AND assistant_message_id=?",
        (steer_id, ctx.tenant_id, session_id, assistant_id),
    )
    if not row:
        raise not_found("Queued message not found")
    if row.get("status") in STEER_CONSUMED_STATUSES:
        return ok({"status": "already_injected", "steer_id": steer_id})
    with db.transaction() as conn:
        conn.execute(
            "UPDATE session_steer_messages SET delivery='inject', status='queued', updated_at=? WHERE id=?",
            (db.utcnow(), steer_id),
        )
    return ok(
        {
            "status": "queued",
            "steer_id": steer_id,
            "delivery": "inject",
            "assistant_message_id": assistant_id,
        }
    )


@router.get("/sessions/{session_id}/local-browser")
def local_browser(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    return ok(_browser_status(ctx, session_id))


@router.post("/sessions/{session_id}/local-browser")
async def connect_local_browser(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    if not os.getenv("BROWSERSKILL_BINARY"):
        raise AppError("local browser is unavailable", status=503, code=1006)
    data = await body(request)
    action = str(data.get("action") or "").strip().lower()
    if action not in {"preview", "focus", "select", "start", "resume", "pause", "stop"}:
        raise validation("invalid browser action")
    status = _browser_status(ctx, session_id)
    if action in {"preview", "focus"} and not status["connected"]:
        raise conflict("local browser is not connected")
    scope_key = _browser_scope_key(ctx)
    if action == "preview":
        try:
            data = await browser_skill.ui_call(scope_key, session_id, "ui.task_preview")
        except browser_skill.BrowserSkillError as exc:
            raise conflict(str(exc)) from exc
        return ok(data)
    if action == "focus":
        try:
            data = await browser_skill.ui_call(scope_key, session_id, "ui.task_focus")
        except browser_skill.BrowserSkillError as exc:
            raise conflict(str(exc)) from exc
        if not data.get("focused"):
            raise conflict("browser task tab unavailable")
        return ok(_browser_status(ctx, session_id))
    if action in {"select", "start", "resume"}:
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO browser_task_interruptions(scope_key, session) VALUES (?, ?)",
                (scope_key, session_id),
            )
        if action in {"start", "resume"}:
            try:
                await anyio.to_thread.run_sync(browser_skill.control, scope_key, session_id, action)
            except browser_skill.BrowserSkillError as exc:
                raise conflict(str(exc)) from exc
    elif action == "pause":
        await anyio.to_thread.run_sync(browser_skill.control, scope_key, session_id, "pause")
    elif action == "stop":
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM browser_task_interruptions WHERE scope_key=? AND session=?",
                (scope_key, session_id),
            )
        await anyio.to_thread.run_sync(browser_skill.stop_task, scope_key, session_id)
    return ok(_browser_status(ctx, session_id))


async def _sandbox_ws_identity(websocket: WebSocket, token_type: str):
    query = parse_qs(bytes(websocket.scope.get("query_string") or b"").decode("utf-8", errors="replace"))
    ticket = str((query.get("ticket") or [""])[0])
    try:
        claims = decode_token(ticket, token_type)
    except ValueError:
        await websocket.close(code=1008, reason="AUTH_REVOKED")
        return None
    try:
        session_id = str(claims.get("session_id") or "")
        tenant_id = int(claims.get("tenant_id") or 0)
    except (TypeError, ValueError):
        await websocket.close(code=1008, reason="AUTH_REVOKED")
        return None
    user_id = str(claims.get("user_id") or "")
    path_session_id = str((websocket.scope.get("path_params") or {}).get("session_id") or "")
    if not session_id or session_id != path_session_id or tenant_id <= 0 or not user_id:
        await websocket.close(code=1008, reason="AUTH_REVOKED")
        return None
    user = db.fetch_one(
        "SELECT 1 FROM users WHERE id=? AND is_active=1 AND deleted_at IS NULL", (user_id,)
    )
    session = db.fetch_one(
        "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (session_id, tenant_id),
    )
    if not user or not session or (session.get("user_id") and str(session["user_id"]) != user_id):
        await websocket.close(code=1008, reason="AUTH_REVOKED")
        return None
    ticket_id = str(claims.get("jti") or "")
    token_id = str(claims.get("token_id") or "")
    if not ticket_id or not _sandbox_access_token_active(token_id, user_id):
        await websocket.close(code=1008, reason="AUTH_REVOKED")
        return None
    with db.transaction() as conn:
        consumed = conn.execute(
            "UPDATE py_sandbox_tickets SET consumed_at=? WHERE jti=? AND token_type=? "
            "AND tenant_id=? AND user_id=? AND session_id=? AND consumed_at IS NULL AND expires_at>?",
            (db.utcnow(), ticket_id, token_type, tenant_id, user_id, session_id, int(time.time())),
        )
    if int(consumed.rowcount or 0) != 1:
        await websocket.close(code=1008, reason="AUTH_REVOKED")
        return None
    return session_id, tenant_id, user_id, session, query, token_id


def _sandbox_access_token_active(token_id: str, user_id: str) -> bool:
    row = db.fetch_one(
        "SELECT user_id, token_type, is_revoked FROM auth_tokens WHERE id=?",
        (token_id,),
    )
    return bool(
        row
        and str(row.get("user_id") or "") == user_id
        and row.get("token_type") == "access_token"
        and not bool(row.get("is_revoked"))
    )


def _issue_sandbox_ticket(ctx: AuthContext, session_id: str, token_type: str) -> dict[str, Any]:
    if ctx.is_api_key or not ctx.access_token:
        raise unauthorized("Unauthorized: sandbox ticket requires an access token")
    token_row = db.fetch_one(
        "SELECT id FROM auth_tokens WHERE token=? AND user_id=? AND token_type='access_token' AND is_revoked=0",
        (ctx.access_token, ctx.user_id),
    )
    if not token_row:
        raise unauthorized("Unauthorized: access token is no longer active")
    ticket, expires = issue_token(
        session_id,
        ctx.tenant_id,
        token_type,
        120,
        extra={
            "user_id": ctx.user_id,
            "session_id": session_id,
            "token_id": str(token_row["id"]),
        },
    )
    claims = decode_token(ticket, token_type)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_sandbox_tickets(jti, token_type, tenant_id, user_id, session_id, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(claims["jti"]), token_type, ctx.tenant_id, ctx.user_id, session_id, expires),
        )
    return ok({"ticket": ticket, "expires_in": 120, "expires_in_seconds": 120})


@router.post("/sessions/{session_id}/sandbox/terminal-ticket")
def terminal_ticket(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    return _issue_sandbox_ticket(ctx, session_id, "sandbox_terminal")


@router.post("/sessions/{session_id}/sandbox/desktop-ticket")
def desktop_ticket(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    return _issue_sandbox_ticket(ctx, session_id, "sandbox_desktop")


@router.post("/sessions/{session_id}/sandbox/desktop/activity")
def desktop_activity(session_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _session(ctx, session_id)
    _desktop_touch(session_id)
    return ok(message="Activity recorded")


@router.websocket("/sessions/{session_id}/sandbox/terminal")
async def sandbox_terminal_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    identity = await _sandbox_ws_identity(websocket, "sandbox_terminal")
    if identity is None:
        return
    session_id, tenant_id, user_id, session, query, token_id = identity
    agent_id = str((query.get("agent_id") or [""])[0]).strip()
    session_data = dict(session)
    if agent_id:
        session_data["agent_id"] = agent_id
    allow_provision = str((query.get("provision") or [""])[0]).strip().lower() in {"1", "true", "yes"}
    if not _claim_terminal(session_id):
        await websocket.close(code=1008, reason="TOO_MANY_TERMINALS")
        return
    try:
        await _serve_sandbox_terminal(
            websocket,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_data=session_data,
            allow_provision=allow_provision,
            cols=_terminal_dimension(query, "cols", 80),
            rows=_terminal_dimension(query, "rows", 24),
            attach_pid=_terminal_pid(query),
            token_id=token_id,
        )
    finally:
        _release_terminal(session_id)


def _claim_terminal(session_id: str) -> bool:
    with _terminal_counts_lock:
        count = _terminal_counts.get(session_id, 0)
        if count >= TERMINAL_MAX_PER_SESSION:
            return False
        _terminal_counts[session_id] = count + 1
        return True


def _release_terminal(session_id: str) -> None:
    with _terminal_counts_lock:
        count = _terminal_counts.get(session_id, 0) - 1
        if count > 0:
            _terminal_counts[session_id] = count
        else:
            _terminal_counts.pop(session_id, None)


async def _serve_sandbox_terminal(
    websocket: WebSocket,
    *,
    session_id: str,
    tenant_id: int,
    user_id: str,
    session_data: dict[str, Any],
    allow_provision: bool,
    cols: int,
    rows: int,
    attach_pid: int,
    token_id: str,
) -> None:
    try:
        binding, terminal = await anyio.to_thread.run_sync(
            lambda: session_sandbox.open_terminal(
                tenant_id,
                session_data,
                provision=allow_provision,
                cols=cols,
                rows=rows,
                pty_id=attach_pid,
            )
        )
    except session_sandbox.SessionSandboxError as exc:
        reason = {
            "sandbox_not_bound": "SANDBOX_NOT_BOUND",
            "terminal_unsupported": "TERMINAL_UNSUPPORTED",
            "sandbox_start_failed": "INTERNAL",
        }.get(exc.code, "INTERNAL")
        await websocket.close(code=1008, reason=reason)
        return

    ready: dict[str, Any] = {
        "type": "ready",
        "backend": str(binding.get("provider") or getattr(terminal, "backend", "") or ""),
        "reattachable": bool(getattr(terminal, "reattachable", False)),
    }
    pid = int(getattr(terminal, "pid", 0) or 0)
    if pid > 0:
        ready["pty_id"] = pid
    await websocket.send_text(json.dumps(ready, separators=(",", ":")))

    stop = asyncio.Event()
    send_lock = asyncio.Lock()
    last_activity = time.monotonic()

    async def send_text(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(payload, separators=(",", ":")))

    async def send_bytes(payload: bytes) -> None:
        async with send_lock:
            await websocket.send_bytes(payload)

    async def pump_output() -> None:
        nonlocal last_activity
        try:
            while not stop.is_set():
                data = await anyio.to_thread.run_sync(terminal.read, 32768)
                if data:
                    if stop.is_set():
                        return
                    last_activity = time.monotonic()
                    await send_bytes(data)
                    await asyncio.sleep(0)
        except SandboxTerminalClosed as exc:
            if not stop.is_set():
                await send_text({"type": "exited", "exit_code": int(exc.exit_code)})
                await websocket.close(code=1000)
        except Exception as exc:  # noqa: BLE001 - normalize provider stream failures
            logger.warning("sandbox terminal stream failed session=%s: %s", session_id, exc)
            if not stop.is_set():
                await send_text({
                    "type": "error",
                    "code": "INTERNAL",
                    "message": "terminal stream failed",
                })
                await websocket.close(code=1011)
        finally:
            stop.set()

    async def pump_input() -> None:
        nonlocal last_activity
        try:
            while not stop.is_set():
                remaining = TERMINAL_IDLE_SECONDS - (time.monotonic() - last_activity)
                if remaining <= 0:
                    await send_text({
                        "type": "error",
                        "code": "IDLE_DISCONNECTED",
                        "message": "terminal idle disconnect",
                    })
                    await websocket.close(code=1008, reason="IDLE_DISCONNECTED")
                    stop.set()
                    return
                try:
                    event = await asyncio.wait_for(
                        websocket.receive(), timeout=min(remaining, TERMINAL_WATCH_INTERVAL)
                    )
                except asyncio.TimeoutError:
                    continue
                if event.get("type") == "websocket.disconnect":
                    return
                text = event.get("text")
                if isinstance(text, str):
                    try:
                        frame = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    kind = str(frame.get("type") or "")
                    if kind == "ping":
                        await send_text({"type": "pong"})
                    elif kind == "resize":
                        resize_cols = _terminal_frame_dimension(frame.get("cols"))
                        resize_rows = _terminal_frame_dimension(frame.get("rows"))
                        if resize_cols and resize_rows:
                            last_activity = time.monotonic()
                            await anyio.to_thread.run_sync(terminal.resize, resize_cols, resize_rows)
                    elif kind == "input":
                        data = str(frame.get("data") or "").encode("utf-8")
                        if data:
                            last_activity = time.monotonic()
                            await anyio.to_thread.run_sync(terminal.write, data)
                    continue
                data = event.get("bytes")
                if isinstance(data, (bytes, bytearray)) and data:
                    last_activity = time.monotonic()
                    await anyio.to_thread.run_sync(terminal.write, bytes(data))
        finally:
            stop.set()

    async def watch_auth() -> None:
        while not stop.is_set():
            await asyncio.sleep(TERMINAL_AUTH_RECHECK_SECONDS)
            if stop.is_set():
                return
            user = db.fetch_one(
                "SELECT 1 FROM users WHERE id=? AND tenant_id=? AND is_active=1 AND deleted_at IS NULL",
                (user_id, tenant_id),
            )
            session = db.fetch_one(
                "SELECT 1 FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (session_id, tenant_id),
            )
            if not user or not session or not _sandbox_access_token_active(token_id, user_id):
                await send_text({
                    "type": "error",
                    "code": "AUTH_REVOKED",
                    "message": "terminal authorization revoked",
                })
                await websocket.close(code=1008, reason="AUTH_REVOKED")
                stop.set()
                return

    tasks = [
        asyncio.create_task(pump_output()),
        asyncio.create_task(pump_input()),
        asyncio.create_task(watch_auth()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.set()
        try:
            await anyio.to_thread.run_sync(terminal.close)
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _terminal_dimension(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = str((query.get(key) or [""])[0]).strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if 0 < value <= TERMINAL_MAX_GEOMETRY else default


def _terminal_frame_dimension(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if 0 < parsed <= TERMINAL_MAX_GEOMETRY else 0


def _terminal_pid(query: dict[str, list[str]]) -> int:
    raw = str((query.get("pty_id") or [""])[0]).strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if 0 < value <= 0xFFFFFFFF else 0


class _DesktopInputTooLarge(Exception):
    pass


class _DesktopIdleDisconnect(Exception):
    pass


class _DesktopAuthRevoked(Exception):
    pass


class _DesktopLeaseLost(Exception):
    pass


def _desktop_lease_acquire(session_id: str, tenant_id: int, lease_key: str, node_id: str | None = None) -> bool:
    now = time.time()
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO py_desktop_leases(session_id, tenant_id, node_id, lease_key, lease_until, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "tenant_id=excluded.tenant_id, node_id=excluded.node_id, lease_key=excluded.lease_key, "
            "lease_until=excluded.lease_until, updated_at=excluded.updated_at "
            "WHERE py_desktop_leases.lease_until <= ? OR py_desktop_leases.lease_key = excluded.lease_key",
            (
                session_id,
                tenant_id,
                node_id or _desktop_node_id,
                lease_key,
                now + DESKTOP_LEASE_SECONDS,
                db.utcnow(),
                now,
            ),
        )
        return int(cursor.rowcount or 0) == 1


def _desktop_lease_renew(session_id: str, tenant_id: int, lease_key: str) -> bool:
    now = time.time()
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE py_desktop_leases SET lease_until=?, updated_at=? "
            "WHERE session_id=? AND tenant_id=? AND lease_key=?",
            (now + DESKTOP_LEASE_SECONDS, db.utcnow(), session_id, tenant_id, lease_key),
        )
        return int(cursor.rowcount or 0) == 1


def _desktop_lease_release(session_id: str, tenant_id: int, lease_key: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM py_desktop_leases WHERE session_id=? AND tenant_id=? AND lease_key=?",
            (session_id, tenant_id, lease_key),
        )


def _desktop_sandbox_rebuilt(session_id: str, tenant_id: int, sandbox_id: str) -> bool:
    session_id = session_id.strip()
    sandbox_id = sandbox_id.strip()
    if not session_id or not sandbox_id:
        return False
    now = time.time()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT sandbox_id, expires_at FROM py_desktop_last_sandboxes "
            "WHERE session_id=? AND tenant_id=?",
            (session_id, tenant_id),
        ).fetchone()
        previous = str(row["sandbox_id"]) if row and float(row["expires_at"]) > now else ""
        conn.execute(
            "INSERT INTO py_desktop_last_sandboxes(session_id, tenant_id, sandbox_id, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
            "tenant_id=excluded.tenant_id, sandbox_id=excluded.sandbox_id, "
            "expires_at=excluded.expires_at, updated_at=excluded.updated_at",
            (session_id, tenant_id, sandbox_id, now + DESKTOP_LAST_SANDBOX_SECONDS, db.utcnow()),
        )
    return bool(previous and previous != sandbox_id)


def _claim_desktop(session_id: str, tenant_id: int) -> bool:
    with _desktop_sessions_lock:
        if session_id in _desktop_sessions:
            return False
        lease_key = secrets.token_hex(16)
        if not _desktop_lease_acquire(session_id, tenant_id, lease_key):
            return False
        _desktop_sessions.add(session_id)
        _desktop_last_activity[session_id] = time.monotonic()
        _desktop_lease_keys[session_id] = lease_key
        return True


def _release_desktop(session_id: str, tenant_id: int) -> None:
    with _desktop_sessions_lock:
        _desktop_sessions.discard(session_id)
        _desktop_last_activity.pop(session_id, None)
        lease_key = _desktop_lease_keys.pop(session_id, "")
    if lease_key:
        _desktop_lease_release(session_id, tenant_id, lease_key)


def _desktop_touch(session_id: str) -> None:
    with _desktop_sessions_lock:
        if session_id in _desktop_sessions:
            _desktop_last_activity[session_id] = time.monotonic()


def _renew_desktop(session_id: str, tenant_id: int) -> bool:
    with _desktop_sessions_lock:
        lease_key = _desktop_lease_keys.get(session_id, "")
    return bool(lease_key) and _desktop_lease_renew(session_id, tenant_id, lease_key)


async def _relay_desktop(
    websocket: WebSocket,
    upstream,
    *,
    session_id: str,
    tenant_id: int,
    user_id: str,
    token_id: str,
) -> None:
    async def browser_to_sandbox() -> None:
        while True:
            event = await websocket.receive()
            if event.get("type") == "websocket.disconnect":
                return
            data = event.get("bytes")
            if data is None:
                data = event.get("text")
            if data is None:
                continue
            if len(data) > 64 * 1024:
                raise _DesktopInputTooLarge()
            _desktop_touch(session_id)
            await upstream.send(data)

    async def sandbox_to_browser() -> None:
        async for message in upstream:
            _desktop_touch(session_id)
            if isinstance(message, bytes):
                await websocket.send_bytes(message)
            else:
                await websocket.send_text(str(message))

    async def watch_idle() -> None:
        while True:
            await asyncio.sleep(DESKTOP_WATCH_INTERVAL)
            with _desktop_sessions_lock:
                last = _desktop_last_activity.get(session_id, time.monotonic())
            if time.monotonic() - last >= DESKTOP_IDLE_SECONDS:
                raise _DesktopIdleDisconnect()

    async def watch_auth() -> None:
        while True:
            await asyncio.sleep(DESKTOP_AUTH_RECHECK_SECONDS)
            user = db.fetch_one(
                "SELECT 1 FROM users WHERE id=? AND tenant_id=? AND is_active=1 AND deleted_at IS NULL",
                (user_id, tenant_id),
            )
            session = db.fetch_one(
                "SELECT 1 FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (session_id, tenant_id),
            )
            if not user or not session or not _sandbox_access_token_active(token_id, user_id):
                raise _DesktopAuthRevoked()

    async def watch_lease() -> None:
        while True:
            await asyncio.sleep(DESKTOP_LEASE_RENEW_SECONDS)
            if not _renew_desktop(session_id, tenant_id):
                raise _DesktopLeaseLost()

    tasks = [
        asyncio.create_task(browser_to_sandbox()),
        asyncio.create_task(sandbox_to_browser()),
        asyncio.create_task(watch_idle()),
        asyncio.create_task(watch_auth()),
        asyncio.create_task(watch_lease()),
    ]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                raise error
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/sessions/{session_id}/sandbox/desktop")
async def sandbox_desktop_socket(websocket: WebSocket) -> None:
    protocols = [
        value.decode(errors="replace") if isinstance(value, bytes) else str(value)
        for value in (websocket.scope.get("subprotocols") or [])
    ]
    await websocket.accept(subprotocol="binary" if "binary" in protocols else None)
    identity = await _sandbox_ws_identity(websocket, "sandbox_desktop")
    if identity is None:
        return
    session_id, tenant_id, user_id, session, query, token_id = identity
    if not _claim_desktop(session_id, tenant_id):
        await websocket.close(code=1008, reason="DESKTOP_BUSY")
        return
    try:
        agent_id = str((query.get("agent_id") or [""])[0]).strip()
        session_data = dict(session)
        if agent_id:
            session_data["agent_id"] = agent_id
        allow_provision = str((query.get("provision") or [""])[0]).strip().lower() in {"1", "true", "yes"}
        try:
            binding, upstream = await session_sandbox.open_desktop(
                tenant_id, session_data, provision=allow_provision
            )
        except session_sandbox.SessionSandboxError as exc:
            reason = {
                "sandbox_not_bound": "SANDBOX_NOT_BOUND",
                "desktop_unsupported": "DESKTOP_UNSUPPORTED",
                "terminal_unsupported": "DESKTOP_UNSUPPORTED",
                "desktop_start_failed": "DESKTOP_START_FAILED",
                "sandbox_start_failed": "DESKTOP_START_FAILED",
                "desktop_upstream": "DESKTOP_UPSTREAM",
            }.get(exc.code, "INTERNAL")
            await websocket.close(code=1008, reason=reason)
            return
        try:
            try:
                prelude = await asyncio.wait_for(
                    upstream.recv(), timeout=DESKTOP_RFB_HANDSHAKE_SECONDS
                )
            except Exception as exc:  # noqa: BLE001 - normalize upstream handshake failures
                logger.warning("sandbox desktop handshake failed for session %s: %s", session_id, exc)
                await websocket.close(code=1011, reason="DESKTOP_UPSTREAM")
                return
            if not isinstance(prelude, bytes) or not prelude.startswith(DESKTOP_RFB_VERSION):
                await websocket.close(code=1011, reason="DESKTOP_UPSTREAM")
                return
            await websocket.send_bytes(prelude)
            if _desktop_sandbox_rebuilt(
                session_id, tenant_id, str(binding.get("sandbox_id") or "")
            ):
                await websocket.close(code=1008, reason="SANDBOX_REBUILT")
                return
            await _relay_desktop(
                websocket,
                upstream,
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                token_id=token_id,
            )
        except _DesktopInputTooLarge:
            await websocket.close(code=1009, reason="INPUT_TOO_LARGE")
        except _DesktopIdleDisconnect:
            await websocket.close(code=1008, reason="IDLE_DISCONNECTED")
        except _DesktopAuthRevoked:
            await websocket.close(code=1008, reason="AUTH_REVOKED")
        except _DesktopLeaseLost:
            await websocket.close(code=1008, reason="DESKTOP_BUSY")
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001 - upstream relay failures must become a stable close code
            logger.exception("sandbox desktop relay failed for session %s", session_id)
            await websocket.close(code=1011, reason="DESKTOP_UPSTREAM")
        finally:
            try:
                await upstream.close()
            except Exception:  # noqa: BLE001 - the browser close code is already decided
                pass
    finally:
        _release_desktop(session_id, tenant_id)

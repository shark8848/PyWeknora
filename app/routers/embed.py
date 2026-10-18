"""Embed channel management and anonymous embed chat endpoints."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from .. import config, db
from ..deps import parse_auth, require_role
from ..errors import AppError, conflict, forbidden, not_found, unauthorized, validation
from ..security import decode_token, issue_token
from ..services import approvals, file_proxy, mcp_oauth
from ..services.agents import (
    agent_config,
    agent_suggested_questions,
    builtin_agent,
)
from ..utils import dumps, loads, ok, resolve_locale
from .chat import (
    _payload_has_upload,
    _serialize_message,
    _valid_suggestion_attribution,
    stream_chat_events,
)


router = APIRouter(tags=["embed"])

SESSION_MARKER = "embed_channel:"
SESSION_TOKEN_PREFIX = "ems_"
SESSION_TOKEN_TTL = 3600


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _extra_attrs(item: dict[str, Any]) -> dict[str, Any]:
    return {"data": item}


def _serialize_channel(row: dict[str, Any], *, include_token: bool = False) -> dict[str, Any]:
    result = dict(row)
    result["allowed_origins"] = loads(result.get("allowed_origins"), [])
    for key in (
        "enabled", "show_suggested_questions", "allow_web_search",
        "allow_file_upload", "allow_memory",
    ):
        if key in result:
            result[key] = bool(result[key])
    result["has_webhook_secret"] = bool(result.pop("webhook_secret", ""))
    token = result.pop("publish_token", "")
    if include_token and token:
        result["publish_token"] = token
    return result


def _management_channel(ctx: Any, channel_id: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM embed_channels WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (channel_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Embed channel not found")
    return row


def _origin_allowed(origin: str, allowed: list[Any]) -> bool:
    if not origin or not allowed:
        return True
    for raw in allowed:
        pattern = str(raw or "").strip()
        if not pattern:
            continue
        if pattern == "*":
            return True
        if hmac.compare_digest(pattern, origin):
            return True
        if "*" in pattern:
            regex = "^" + re.escape(pattern).replace(r"\*", ".*") + "$"
            if re.match(regex, origin):
                return True
    return False


def _extract_embed_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("embed "):
        return header.split(" ", 1)[1].strip()
    return ""


def _session_token(channel_id: str, tenant_id: int) -> tuple[str, int]:
    token, _ = issue_token(channel_id, tenant_id, "embed_session", SESSION_TOKEN_TTL)
    return SESSION_TOKEN_PREFIX + token, SESSION_TOKEN_TTL


def _embed_channel(request: Request, channel_id: str) -> dict[str, Any]:
    token = _extract_embed_token(request)
    if not token:
        raise unauthorized("embed publish token is required")
    row = db.fetch_one(
        "SELECT * FROM embed_channels WHERE id=? AND enabled=1 AND deleted_at IS NULL",
        (channel_id,),
    )
    if not row:
        raise not_found("Embed channel not found")
    if token.startswith(SESSION_TOKEN_PREFIX):
        try:
            payload = decode_token(token[len(SESSION_TOKEN_PREFIX):], "embed_session")
        except ValueError as exc:
            raise unauthorized("invalid embed session token") from exc
        if payload.get("sub") != channel_id or int(payload.get("tenant_id") or 0) != int(row["tenant_id"]):
            raise unauthorized("invalid embed session token")
    else:
        publish = str(row.get("publish_token") or "")
        if not publish or not hmac.compare_digest(publish, token):
            raise unauthorized("invalid embed channel or token")
    origin = request.headers.get("origin") or ""
    if not origin:
        referer = request.headers.get("referer") or ""
        if referer:
            match = re.match(r"^(https?://[^/]+)", referer)
            origin = match.group(1) if match else ""
    if not _origin_allowed(origin, loads(row.get("allowed_origins"), [])):
        raise forbidden("origin not allowed")
    return row


def _session_signature(channel: dict[str, Any], session_id: str) -> str:
    message = "embed-session:v2|{}|{}|{}".format(
        channel.get("publish_token") or "", channel["id"], session_id
    )
    digest = hmac.new(config.jwt_secret().encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _embed_session(channel: dict[str, Any], session_id: str, request: Request) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (session_id, channel["tenant_id"]),
    )
    if not row or row.get("description") != SESSION_MARKER + channel["id"]:
        raise not_found("Session not found")
    supplied = request.headers.get("x-embed-session", "").strip()
    expected = _session_signature(channel, session_id)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise forbidden("session signature invalid")
    return row


def _agent_config(agent_id: str, tenant_id: int) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (agent_id, tenant_id),
    )
    return loads(row.get("config"), {}) if row else {}


def _public_config(channel: dict[str, Any]) -> dict[str, Any]:
    config_data = _agent_config(str(channel.get("agent_id") or ""), int(channel["tenant_id"]))
    kb_ids = config_data.get("knowledge_bases") or config_data.get("knowledge_base_ids") or []
    normalized_kbs: list[str] = []
    for value in kb_ids:
        if isinstance(value, dict):
            value = value.get("id") or value.get("knowledge_base_id")
        if value:
            normalized_kbs.append(str(value))
    agent = db.fetch_one(
        "SELECT name, avatar FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (str(channel.get("agent_id") or ""), int(channel["tenant_id"])),
    ) or {}
    builtin = builtin_agent(str(channel.get("agent_id") or ""), int(channel["tenant_id"])) or {}
    builtin_name = str(builtin.get("name") or "")
    builtin_avatar = str(builtin.get("avatar") or "")
    agent_name = agent.get("name") or builtin_name
    agent_avatar = agent.get("avatar") or builtin_avatar
    return {
        "channel_id": channel["id"],
        "name": channel.get("name") or "",
        "display_title": channel.get("page_title") or channel.get("name") or "",
        "knowledge_base_ids": normalized_kbs,
        "agent_id": channel.get("agent_id") or "",
        "agent_name": agent_name,
        "agent_avatar": agent_avatar,
        "welcome_message": channel.get("welcome_message") or "",
        "primary_color": channel.get("primary_color") or "#07C05F",
        "page_title": channel.get("page_title") or "WeKnora",
        "header_title_mode": channel.get("header_title_mode") or "channel",
        "show_suggested_questions": bool(channel.get("show_suggested_questions", 1)),
        "widget_position": channel.get("widget_position") or "bottom-right",
        "allow_web_search": bool(channel.get("allow_web_search")),
        "allow_file_upload": bool(channel.get("allow_file_upload")),
        "agent_web_search_enabled": False,
        "agent_image_upload_enabled": False,
        "default_locale": channel.get("default_locale") or "zh-CN",
    }


@router.get("/agents/{agent_id}/embed-channels")
def list_agent_embed_channels(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT * FROM embed_channels WHERE tenant_id=? AND agent_id=? AND deleted_at IS NULL ORDER BY created_at DESC",
        (ctx.tenant_id, agent_id),
    )
    return ok([_serialize_channel(row) for row in rows])


@router.post("/agents/{agent_id}/embed-channels", status_code=201)
async def create_agent_embed_channel(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    channel_id = db.new_id()
    token = "emb_" + secrets.token_urlsafe(32)
    now = db.utcnow()
    origins = data.get("allowed_origins") or ["*"]
    if not isinstance(origins, list) or not origins:
        raise validation("at least one allowed origin is required")
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO embed_channels(id, tenant_id, agent_id, name, enabled, publish_token,
              allowed_origins, welcome_message, rate_limit_per_minute, rate_limit_per_day,
              primary_color, page_title, header_title_mode, show_suggested_questions,
              widget_position, allow_web_search, allow_file_upload, default_locale,
              webhook_url, webhook_secret, created_at, updated_at, allow_memory)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id, ctx.tenant_id, agent_id, str(data.get("name") or "Web Embed"),
                1 if data.get("enabled", True) else 0, token, dumps(origins),
                str(data.get("welcome_message") or ""),
                int(data.get("rate_limit_per_minute") or 30),
                int(data.get("rate_limit_per_day") or 10000),
                str(data.get("primary_color") or "#07C05F"),
                str(data.get("page_title") or "WeKnora"),
                str(data.get("header_title_mode") or "channel"),
                1 if data.get("show_suggested_questions", True) else 0,
                str(data.get("widget_position") or "bottom-right"),
                1 if data.get("allow_web_search") else 0,
                1 if data.get("allow_file_upload") else 0,
                str(data.get("default_locale") or "zh-CN"),
                str(data.get("webhook_url") or ""),
                str(data.get("webhook_secret") or ""),
                now, now, 1 if data.get("allow_memory") else 0,
            ),
        )
    return ok(_serialize_channel(db.fetch_one("SELECT * FROM embed_channels WHERE id=?", (channel_id,)), include_token=True))


@router.get("/embed-channels")
def list_embed_channels(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT * FROM embed_channels WHERE tenant_id=? AND deleted_at IS NULL ORDER BY created_at DESC",
        (ctx.tenant_id,),
    )
    return ok([_serialize_channel(row) for row in rows])


@router.get("/embed-channels/{channel_id}")
def get_embed_channel(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(_serialize_channel(_management_channel(ctx, channel_id), include_token=True))


@router.put("/embed-channels/{channel_id}")
async def update_embed_channel(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _management_channel(ctx, channel_id)
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    for key in (
        "agent_id", "name", "welcome_message", "primary_color", "page_title",
        "header_title_mode", "widget_position", "default_locale", "webhook_url",
    ):
        if key in data:
            updates[key] = str(data.get(key) or "")
    for key in ("rate_limit_per_minute", "rate_limit_per_day"):
        if key in data:
            updates[key] = int(data.get(key) or 0)
    for key in (
        "enabled", "show_suggested_questions", "allow_web_search",
        "allow_file_upload", "allow_memory",
    ):
        if key in data:
            updates[key] = 1 if data.get(key) else 0
    if "allowed_origins" in data:
        if not isinstance(data["allowed_origins"], list) or not data["allowed_origins"]:
            raise validation("at least one allowed origin is required")
        updates["allowed_origins"] = dumps(data["allowed_origins"])
    if "webhook_secret" in data and data.get("webhook_secret"):
        updates["webhook_secret"] = str(data["webhook_secret"])
    with db.transaction() as conn:
        db.update("embed_channels", updates, "id=? AND tenant_id=?", (channel_id, ctx.tenant_id), conn)
    return ok(_serialize_channel(_management_channel(ctx, channel_id), include_token=True))


@router.delete("/embed-channels/{channel_id}")
def delete_embed_channel(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _management_channel(ctx, channel_id)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE embed_channels SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (now, now, channel_id, ctx.tenant_id))
    return {"success": True}


@router.post("/embed-channels/{channel_id}/rotate-token")
def rotate_embed_token(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _management_channel(ctx, channel_id)
    token = "emb_" + secrets.token_urlsafe(32)
    with db.transaction() as conn:
        conn.execute("UPDATE embed_channels SET publish_token=?, updated_at=? WHERE id=?", (token, db.utcnow(), channel_id))
    return ok(_serialize_channel(_management_channel(ctx, channel_id), include_token=True))


@router.get("/embed-channels/{channel_id}/stats")
def embed_channel_stats(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _management_channel(ctx, channel_id)
    row = db.fetch_one(
        "SELECT COUNT(*) AS n FROM sessions WHERE tenant_id=? AND description=? AND deleted_at IS NULL",
        (ctx.tenant_id, SESSION_MARKER + channel_id),
    )
    return ok({"session_count": int(row["n"] if row else 0)})


@router.post("/embed-channels/{channel_id}/preview-session")
def issue_preview_session(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    channel = _management_channel(ctx, channel_id)
    if not channel.get("enabled"):
        raise forbidden("embed channel is disabled")
    token, expires_in = _session_token(channel_id, ctx.tenant_id)
    return ok({"session_token": token, "expires_in": expires_in})


@router.post("/embed/{channel_id}/exchange")
def exchange_embed_session(channel_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    token = _extract_embed_token(request)
    if token.startswith(SESSION_TOKEN_PREFIX):
        raise forbidden("publish token required")
    session_token, expires_in = _session_token(channel_id, int(channel["tenant_id"]))
    return ok({"session_token": session_token, "expires_in": expires_in})


@router.get("/embed/{channel_id}/config")
def embed_public_config(channel_id: str, request: Request) -> dict[str, Any]:
    return ok(_public_config(_embed_channel(request, channel_id)))


@router.get("/embed/{channel_id}/suggested-questions")
def embed_suggested_questions(channel_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    if not channel.get("show_suggested_questions"):
        return ok({"questions": []})
    limit = request.query_params.get("limit")
    try:
        count = max(0, int(limit)) if limit else 0
    except ValueError:
        count = 0
    count = min(12, count)
    tenant_id = int(channel["tenant_id"])
    agent_id = str(channel.get("agent_id") or "")
    config = agent_config(tenant_id, agent_id)
    kb_ids = []
    if str(config.get("kb_selection_mode") or "").strip().lower() == "selected":
        for value in config.get("knowledge_bases") or []:
            if isinstance(value, dict):
                value = value.get("id") or value.get("knowledge_base_id")
            kb_id = str(value or "").strip()
            if kb_id:
                kb_ids.append(kb_id)
    questions = agent_suggested_questions(
        tenant_id,
        config,
        kb_ids,
        [],
        count,
        resolve_locale(request.headers.get("accept-language", "")),
    )
    return ok({"questions": questions})


@router.get("/embed/{channel_id}/chunks/{chunk_id}")
def embed_chunk(channel_id: str, chunk_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    row = db.fetch_one(
        "SELECT * FROM chunks WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (chunk_id, channel["tenant_id"]),
    )
    if not row:
        raise not_found("chunk not found")
    return ok(
        {
            "id": row["id"],
            "content": row.get("content") or "",
            "knowledge_id": row.get("knowledge_id"),
            "knowledge_base_id": row.get("knowledge_base_id"),
            "chunk_index": row.get("chunk_index"),
            "chunk_type": row.get("chunk_type") or "text",
        }
    )


@router.post("/embed/{channel_id}/sessions", status_code=201)
def create_embed_session(channel_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    session_id = db.new_id()
    now = db.utcnow()
    agent_id = str(channel.get("agent_id") or "builtin-quick-answer")
    agent_config = _agent_config(agent_id, int(channel["tenant_id"]))
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sessions(id, tenant_id, title, description, max_rounds,
              enable_rewrite, fallback_strategy, fallback_response, keyword_threshold,
              vector_threshold, embedding_top_k, rerank_top_k, rerank_threshold,
              summary_parameters, agent_config, context_config, agent_id, user_id,
              is_pinned, created_at, updated_at, sandbox_config_tenant_id)
            VALUES (?, ?, '', ?, 5, 1, 'fixed', '很抱歉，我暂时无法回答这个问题。', 0.5,
              0.5, 10, 10, 0.65, '{}', ?, '{}', ?, ?, 0, ?, ?, 0)
            """,
            (
                session_id, int(channel["tenant_id"]), SESSION_MARKER + channel_id,
                dumps(agent_config), agent_id,
                f"embed_session:{int(channel['tenant_id'])}:{channel_id}:{session_id}", now, now,
            ),
        )
    return ok({"id": session_id, "sig": _session_signature(channel, session_id)})


@router.get("/embed/{channel_id}/messages/{session_id}/load")
def embed_load_messages(channel_id: str, session_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    _embed_session(channel, session_id, request)
    try:
        limit = min(200, max(1, int(request.query_params.get("limit") or 20)))
    except ValueError:
        limit = 20
    before = request.query_params.get("before_time")
    clauses = ["session_id=?", "deleted_at IS NULL"]
    params: list[Any] = [session_id]
    if before:
        clauses.append("created_at<?")
        params.append(before)
    rows = db.fetch_all(
        f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        [*params, limit],
    )
    rows.reverse()
    return {"success": True, "data": [_serialize_message(row) for row in rows], "total": len(rows)}


@router.post("/embed/{channel_id}/knowledge-chat/{session_id}")
@router.post("/embed/{channel_id}/agent-chat/{session_id}")
async def embed_chat(channel_id: str, session_id: str, request: Request) -> StreamingResponse:
    channel = _embed_channel(request, channel_id)
    session = _embed_session(channel, session_id, request)
    payload = await body(request)
    payload["agent_id"] = channel.get("agent_id") or ""
    payload["knowledge_base_ids"] = []
    payload["knowledge_ids"] = []
    payload["tag_ids"] = []
    payload["mentioned_items"] = []
    payload["skill_names"] = []
    for key in ("summary_model_id", "question_origin", "agent_source_tenant_id"):
        payload.pop(key, None)
    payload["web_search_enabled"] = bool(channel.get("allow_web_search")) and bool(payload.get("web_search_enabled"))
    if not channel.get("allow_file_upload"):
        for key in ("images", "attachment_uploads", "attachment_ids"):
            payload.pop(key, None)
    if not str(payload.get("query") or "").strip() and not _payload_has_upload(payload):
        raise validation("query is required")
    if not _valid_suggestion_attribution(
        int(channel["tenant_id"]),
        session_id,
        str(payload.get("query") or ""),
        payload.get("suggestion_attribution"),
    ):
        raise validation("invalid suggestion attribution")
    payload["mcp_service_ids"] = []
    agent_mode = request.url.path.endswith(f"/agent-chat/{session_id}")
    payload["agent_enabled"] = agent_mode
    memory_user_id = str(session.get("user_id") or f"embed_session:{int(channel['tenant_id'])}:{channel_id}:{session_id}")
    events = stream_chat_events(
        session=session,
        session_id=session_id,
        tenant_id=int(channel["tenant_id"]),
        payload=payload,
        agent_mode=agent_mode,
        channel="embed",
        user_id=memory_user_id,
        memory_allowed=bool(channel.get("allow_memory")),
        locale=resolve_locale(request.headers.get("accept-language", "")),
    )
    return StreamingResponse(events, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/embed/{channel_id}/sessions/{session_id}/stop")
def stop_embed_session(channel_id: str, session_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    _embed_session(channel, session_id, request)
    return ok({"stopped": True})


def _suggestion_set(session_id: str, message_id: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        "SELECT * FROM message_suggestion_sets WHERE session_id=? AND assistant_message_id=? ORDER BY created_at DESC LIMIT 1",
        (session_id, message_id),
    )
    if row:
        row["questions"] = loads(row.get("questions"), [])
        row["allow_regenerate"] = bool(row.get("allow_regenerate"))
    return row


@router.get("/embed/{channel_id}/sessions/{session_id}/messages/{message_id}/suggestions")
def embed_get_suggestions(channel_id: str, session_id: str, message_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    _embed_session(channel, session_id, request)
    if not channel.get("show_suggested_questions"):
        return ok({"id": "", "status": "suppressed", "allow_regenerate": False, "questions": []})
    row = _suggestion_set(session_id, message_id)
    if not row:
        return ok({"id": "", "status": "failed", "allow_regenerate": True, "questions": []})
    return ok(row)


@router.post("/embed/{channel_id}/sessions/{session_id}/messages/{message_id}/suggestions")
def embed_ensure_suggestions(channel_id: str, session_id: str, message_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    _embed_session(channel, session_id, request)
    if not channel.get("show_suggested_questions"):
        return ok({"id": "", "status": "suppressed", "allow_regenerate": False, "questions": []})
    message = db.fetch_one(
        "SELECT content FROM messages WHERE id=? AND session_id=? AND deleted_at IS NULL",
        (message_id, session_id),
    )
    if not message:
        raise not_found("Message not found")
    questions = [
        {"id": db.new_id(), "text": "能否总结一下关键结论？", "category": "deepen", "source": "document"},
        {"id": db.new_id(), "text": "有哪些可执行的下一步？", "category": "action", "source": "document"},
        {"id": db.new_id(), "text": "相关资料还可以从哪里查看？", "category": "clarify", "source": "wiki"},
    ]
    set_id, now = db.new_id(), db.utcnow()
    with db.transaction() as conn:
        conn.execute("DELETE FROM message_suggestion_sets WHERE session_id=? AND assistant_message_id=?", (session_id, message_id))
        conn.execute(
            """
            INSERT INTO message_suggestion_sets(id, tenant_id, session_id, assistant_message_id,
              agent_id, agent_tenant_id, placement, config_hash, locale, status,
              allow_regenerate, questions, model_id, created_at, updated_at, generated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'inline', '', 'zh-CN', 'ready', 1, ?, '', ?, ?, ?)
            """,
            (
                set_id, int(channel["tenant_id"]), session_id, message_id,
                channel.get("agent_id") or "", int(channel["tenant_id"]),
                dumps(questions), now, now, now,
            ),
        )
    return ok({"id": set_id, "status": "ready", "allow_regenerate": True, "questions": questions, "generated_at": now})


@router.post("/embed/{channel_id}/sessions/{session_id}/suggestion-events")
async def embed_record_suggestion_event(channel_id: str, session_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    _embed_session(channel, session_id, request)
    data = await body(request)
    set_id = str(data.get("suggestion_set_id") or "").strip()
    if not set_id:
        raise validation("suggestion_set_id is required")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO message_suggestion_events(tenant_id, session_id, suggestion_set_id, question_id, event_type, actor_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(channel["tenant_id"]), session_id, set_id, str(data.get("question_id") or ""), str(data.get("event_type") or "impression"), request.headers.get("x-embed-visitor", ""), db.utcnow()),
        )
    return ok(message="Recorded")


@router.post("/embed/{channel_id}/sessions/{session_id}/events")
async def embed_relay_event(channel_id: str, session_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    _embed_session(channel, session_id, request)
    data = await body(request)
    return ok({"received": True, "event": data.get("event") or data.get("type") or "message"})


@router.post("/embed/{channel_id}/sessions/{session_id}/tool-approvals/{pending_id}")
async def embed_tool_approval(channel_id: str, session_id: str, pending_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    session = _embed_session(channel, session_id, request)
    data = await body(request)
    decision = str(data.get("decision") or "").strip().lower()
    if decision not in {"approve", "reject"}:
        raise validation("decision must be approve or reject")
    modified_args = data.get("modified_args")
    if modified_args is not None and not isinstance(modified_args, dict):
        raise validation("modified_args must be a non-null JSON object")
    try:
        approvals.resolve(
            pending_id,
            approvals.Decision(
                approved=decision == "approve",
                modified_args=modified_args if decision == "approve" else None,
                reason=str(data.get("reason") or ""),
            ),
            kind="tool",
            tenant_id=int(channel["tenant_id"]),
            user_id=str(session.get("user_id") or ""),
            session_id=session_id,
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


@router.post("/embed/{channel_id}/sessions/{session_id}/mcp-oauth-resolutions/{pending_id}")
async def embed_mcp_oauth_resolution(channel_id: str, session_id: str, pending_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    session = _embed_session(channel, session_id, request)
    data = await body(request)
    service_id = str(data.get("service_id") or "").strip()
    if not service_id:
        raise validation("service_id is required")
    service = db.fetch_one(
        "SELECT id FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (service_id, channel["tenant_id"]),
    )
    if not service:
        raise not_found("MCP service not found")
    decision = str(data.get("decision") or "authorize").strip().lower()
    user_id = str(session.get("user_id") or "")
    if decision in {"cancel", "reject", "skip"}:
        try:
            approvals.resolve(
                pending_id,
                approvals.Decision(approved=False, reason="user canceled", canceled=True),
                kind="oauth",
                tenant_id=int(channel["tenant_id"]),
                user_id=user_id,
                session_id=session_id,
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
    if not mcp_oauth.is_authorized(int(channel["tenant_id"]), user_id, service_id):
        raise conflict("authorization not completed yet for this MCP service")
    try:
        approvals.resolve(
            pending_id,
            approvals.Decision(approved=True),
            kind="oauth",
            tenant_id=int(channel["tenant_id"]),
            user_id=user_id,
            session_id=session_id,
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


@router.post("/embed/{channel_id}/sessions/{session_id}/mcp-oauth-resolutions/{pending_id}/cancel")
def embed_mcp_oauth_cancel(channel_id: str, session_id: str, pending_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    session = _embed_session(channel, session_id, request)
    try:
        approvals.resolve(
            pending_id,
            approvals.Decision(approved=False, reason="user canceled", canceled=True),
            kind="oauth",
            tenant_id=int(channel["tenant_id"]),
            user_id=str(session.get("user_id") or ""),
            session_id=session_id,
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


@router.post("/embed/{channel_id}/sessions/{session_id}/mcp-services/{service_id}/oauth/authorize-url")
async def embed_mcp_authorize_url(channel_id: str, session_id: str, service_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    session = _embed_session(channel, session_id, request)
    data = await body(request)
    service = db.fetch_one(
        "SELECT * FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (service_id, channel["tenant_id"]),
    )
    if not service:
        raise not_found("MCP service not found")
    redirect_uri = str(data.get("redirect_uri") or "").strip()
    if not redirect_uri:
        raise validation("redirect_uri is required")
    try:
        authorization_url, attempt = mcp_oauth.start_authorization(
            service,
            int(channel["tenant_id"]),
            str(session.get("user_id") or ""),
            redirect_uri,
            str(data.get("frontend_redirect") or "/"),
        )
    except mcp_oauth.OAuthError as exc:
        raise validation(str(exc)) from exc
    return ok({"authorization_url": authorization_url, "authorization_attempt": attempt})


@router.get("/embed/{channel_id}/sessions/{session_id}/mcp-services/{service_id}/oauth/status")
def embed_mcp_oauth_status(channel_id: str, session_id: str, service_id: str, request: Request) -> dict[str, Any]:
    channel = _embed_channel(request, channel_id)
    session = _embed_session(channel, session_id, request)
    user_id = str(session.get("user_id") or "")
    attempt_id = str(request.query_params.get("authorization_attempt") or "").strip()
    if attempt_id:
        authorized = mcp_oauth.attempt_complete(
            int(channel["tenant_id"]), user_id, service_id, attempt_id
        )
        return ok({"authorized": authorized, "state": "authorized" if authorized else "pending"})
    return ok(mcp_oauth.token_status(mcp_oauth.get_token(int(channel["tenant_id"]), user_id, service_id)))


@router.get("/embed/{channel_id}/files")
def embed_file(channel_id: str, request: Request):
    channel = _embed_channel(request, channel_id)
    reference = str(
        request.query_params.get("file_path")
        or request.query_params.get("path")
        or request.query_params.get("url")
        or ""
    ).strip()
    if not reference:
        raise validation("missing required parameter: file_path")
    return file_proxy.file_response(reference, int(channel["tenant_id"]))


@router.get("/embed-frame-policy")
def embed_frame_policy(request: Request) -> Response:
    channel_id = request.query_params.get("channel_id") or ""
    channel = db.fetch_one(
        "SELECT allowed_origins FROM embed_channels WHERE id=? AND enabled=1 AND deleted_at IS NULL",
        (channel_id,),
    )
    origins = loads(channel.get("allowed_origins"), []) if channel else []
    frame_ancestors = "'self'" if not origins or "*" in origins else " ".join(str(value) for value in origins)
    return Response(
        headers={
            "Content-Security-Policy": f"frame-ancestors {frame_ancestors}",
            "Cache-Control": "no-store",
        }
    )

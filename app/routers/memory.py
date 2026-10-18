"""Personal long-term memory storage and management."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import APIRouter, Request

from .. import db
from ..deps import AuthContext, parse_auth, require_role
from ..errors import not_found, validation
from ..utils import dumps, loads, ok, pagination


router = APIRouter(tags=["memory"])

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "write_mode": "explicit_only",
    "extract_model_id": "",
    "max_items": 200,
    "extract_delay_seconds": 30,
    "extract_min_interval_seconds": 3600,
    "extract_instructions": "",
    "interest_threshold": 3,
    "retrieval_conditioning": True,
    "embedding_model_id": "",
    "vector_recall": False,
}


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _workspace_config(ctx: AuthContext) -> dict[str, Any]:
    tenant = db.fetch_one("SELECT memory_config FROM tenants WHERE id=?", (ctx.tenant_id,)) or {}
    return {**DEFAULT_CONFIG, **loads(tenant.get("memory_config"), {})}


def _user_enabled(ctx: AuthContext) -> bool:
    prefs = loads(ctx.user.get("preferences"), {})
    return bool(prefs.get("memory_enabled", True))


def _settings(ctx: AuthContext) -> dict[str, Any]:
    config = _workspace_config(ctx)
    enabled = _user_enabled(ctx)
    count = int(db.fetch_one(
        "SELECT COUNT(*) AS n FROM memory_items WHERE tenant_id=? AND subject_id=? AND status='active'",
        (ctx.tenant_id, ctx.user_id),
    )["n"])
    return {
        "workspace_enabled": bool(config.get("enabled", True)),
        "user_enabled": enabled,
        "effective": bool(config.get("enabled", True)) and enabled,
        "write_mode": config.get("write_mode") or "explicit_only",
        "item_count": count,
        "max_items": int(config.get("max_items") or 200),
    }


def _subject(ctx: AuthContext) -> None:
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO memory_subjects(id, tenant_id, subject_id, enabled, created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?) ON CONFLICT(tenant_id, subject_id) DO NOTHING",
            (db.new_id(), ctx.tenant_id, ctx.user_id, now, now),
        )


def _item_json(row: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in row.items() if key not in {"normalized_key", "expires_at"}}
    result["importance"] = int(result.get("importance") or 3)
    result["use_count"] = int(result.get("use_count") or 0)
    return result


def _item(item_id: str, ctx: AuthContext) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM memory_items WHERE id=? AND tenant_id=? AND subject_id=?",
        (item_id, ctx.tenant_id, ctx.user_id),
    )
    if not row:
        raise not_found("Memory item not found")
    return row


def _normalized(content: str) -> str:
    return re.sub(r"\s+", " ", content.strip().lower())


@router.get("/memory/settings")
def get_settings(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(_settings(ctx))


@router.put("/memory/settings")
async def update_settings(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    if "enabled" in data:
        preferences = loads(ctx.user.get("preferences"), {})
        preferences["memory_enabled"] = bool(data["enabled"])
        with db.transaction() as conn:
            conn.execute("UPDATE users SET preferences=?, updated_at=? WHERE id=?", (dumps(preferences), db.utcnow(), ctx.user_id))
        ctx.user["preferences"] = dumps(preferences)
    return ok(_settings(ctx))


@router.get("/memory/items")
def list_items(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    page_no, size, offset = pagination(request.query_params.get("limit"), request.query_params.get("offset"), 500)
    clauses = ["tenant_id=?", "subject_id=?"]
    params: list[Any] = [ctx.tenant_id, ctx.user_id]
    status = request.query_params.get("status")
    if status:
        clauses.append("status=?")
        params.append(status)
    where = " AND ".join(clauses)
    total = int(db.fetch_one(f"SELECT COUNT(*) AS n FROM memory_items WHERE {where}", params)["n"])
    rows = db.fetch_all(
        f"SELECT * FROM memory_items WHERE {where} ORDER BY importance DESC, updated_at DESC LIMIT ? OFFSET ?",
        [*params, size, offset],
    )
    return {**ok([_item_json(row) for row in rows]), "total": total}


@router.post("/memory/items", status_code=201)
async def create_item(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "viewer")
    data = await body(request)
    content = str(data.get("content") or "").strip()
    if not content:
        raise validation("Memory content is required")
    kind = str(data.get("kind") or "fact")
    if kind not in {"profile", "preference", "fact", "task", "interest"}:
        raise validation("Invalid memory kind")
    _subject(ctx)
    item_id, now = db.new_id(), db.utcnow()
    importance = min(5, max(1, int(data.get("importance") or 3)))
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO memory_items(id, tenant_id, subject_id, kind, content, topic,
              normalized_key, importance, origin, status, valid_from, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual', 'active', ?, ?, ?)
            """,
            (item_id, ctx.tenant_id, ctx.user_id, kind, content, str(data.get("topic") or ""),
             _normalized(content)[:255], importance, now, now, now),
        )
    return ok(_item_json(_item(item_id, ctx)))


@router.put("/memory/items/{item_id}")
async def update_item(item_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _item(item_id, ctx)
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    if "content" in data:
        content = str(data.get("content") or "").strip()
        if not content:
            raise validation("Memory content is required")
        updates["content"] = content
        updates["normalized_key"] = _normalized(content)[:255]
    if "importance" in data:
        updates["importance"] = min(5, max(1, int(data["importance"] or 3)))
    with db.transaction() as conn:
        db.update("memory_items", updates, "id=? AND tenant_id=? AND subject_id=?", (item_id, ctx.tenant_id, ctx.user_id), conn)
    return ok(_item_json(_item(item_id, ctx)))


@router.delete("/memory/items/{item_id}")
def delete_item(item_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _item(item_id, ctx)
    with db.transaction() as conn:
        conn.execute("DELETE FROM memory_items WHERE id=? AND tenant_id=? AND subject_id=?", (item_id, ctx.tenant_id, ctx.user_id))
    return ok(message="Memory item deleted")


@router.delete("/memory/items")
def clear_items(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    with db.transaction() as conn:
        cursor = conn.execute("DELETE FROM memory_items WHERE tenant_id=? AND subject_id=?", (ctx.tenant_id, ctx.user_id))
        removed = cursor.rowcount
    return {**ok(message="Memory cleared"), "removed": removed}


@router.post("/memory/items/{item_id}/confirm")
def confirm_item(item_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _item(item_id, ctx)
    with db.transaction() as conn:
        conn.execute("UPDATE memory_items SET status='active', updated_at=? WHERE id=?", (db.utcnow(), item_id))
    return ok(_item_json(_item(item_id, ctx)))


@router.post("/memory/items/{item_id}/reject")
def reject_item(item_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _item(item_id, ctx)
    fingerprint = hashlib.sha256(_normalized(row["content"]).encode()).hexdigest()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO memory_tombstones(id, tenant_id, subject_id, topic, fingerprint, source_message_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (db.new_id(), ctx.tenant_id, ctx.user_id, row.get("topic") or "", fingerprint, row.get("source_message_id"), db.utcnow()),
        )
        conn.execute("DELETE FROM memory_items WHERE id=?", (item_id,))
    return ok(message="Memory item rejected")


@router.get("/memory/export")
def export_items(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT * FROM memory_items WHERE tenant_id=? AND subject_id=? ORDER BY created_at",
        (ctx.tenant_id, ctx.user_id),
    )
    return {**ok([_item_json(row) for row in rows]), "total": len(rows)}


@router.post("/memory/consolidate")
def consolidate(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT * FROM memory_items WHERE tenant_id=? AND subject_id=? AND status='active' ORDER BY importance DESC, created_at",
        (ctx.tenant_id, ctx.user_id),
    )
    if len(rows) < 2:
        return ok({"merged": 0, "demoted": 0, "expired": 0, "reviewed": len(rows), "candidates": 0, "skipped": "too_few_items"})
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get("normalized_key") or _normalized(row["content"]), []).append(row)
    merged = 0
    with db.transaction() as conn:
        for items in groups.values():
            if len(items) < 2:
                continue
            keep = items[0]
            for duplicate in items[1:]:
                conn.execute(
                    "UPDATE memory_items SET status='superseded', superseded_by=?, invalid_at=?, updated_at=? WHERE id=?",
                    (keep["id"], db.utcnow(), db.utcnow(), duplicate["id"]),
                )
                merged += 1
    return ok({"merged": merged, "demoted": 0, "expired": 0, "reviewed": len(rows), "candidates": len(rows) - merged})


@router.get("/memory/topics")
def list_topics(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    page_no, size, offset = pagination(request.query_params.get("limit"), request.query_params.get("offset"), 500)
    total = int(db.fetch_one("SELECT COUNT(*) AS n FROM memory_topic_stats WHERE tenant_id=? AND subject_id=?", (ctx.tenant_id, ctx.user_id))["n"])
    rows = db.fetch_all(
        "SELECT * FROM memory_topic_stats WHERE tenant_id=? AND subject_id=? ORDER BY hits DESC, last_seen_at DESC LIMIT ? OFFSET ?",
        (ctx.tenant_id, ctx.user_id, size, offset),
    )
    config = _workspace_config(ctx)
    items = [{
        "id": row["id"], "topic": row["topic"], "aliases": loads(row.get("aliases"), []),
        "hits": int(row.get("hits") or 0), "threshold": int(config.get("interest_threshold") or 3),
        "last_seen_at": row.get("last_seen_at"),
    } for row in rows]
    return {**ok(items), "total": total}


@router.post("/memory/topics/{topic_id}/promote")
def promote_topic(topic_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    topic = db.fetch_one("SELECT * FROM memory_topic_stats WHERE id=? AND tenant_id=? AND subject_id=?", (topic_id, ctx.tenant_id, ctx.user_id))
    if not topic:
        raise not_found("Memory topic not found")
    _subject(ctx)
    item_id, now = db.new_id(), db.utcnow()
    content = f"用户持续关注：{topic['topic']}"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO memory_items(id, tenant_id, subject_id, kind, content, topic, normalized_key, importance, origin, status, valid_from, created_at, updated_at) VALUES (?, ?, ?, 'interest', ?, ?, ?, 3, 'extracted', 'active', ?, ?, ?)",
            (item_id, ctx.tenant_id, ctx.user_id, content, topic["topic"], _normalized(content)[:255], now, now, now),
        )
        conn.execute("UPDATE memory_topic_stats SET promoted_at=?, updated_at=? WHERE id=?", (now, now, topic_id))
    return ok(_item_json(_item(item_id, ctx)))


@router.delete("/memory/topics/{topic_id}")
def delete_topic(topic_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    topic = db.fetch_one("SELECT * FROM memory_topic_stats WHERE id=? AND tenant_id=? AND subject_id=?", (topic_id, ctx.tenant_id, ctx.user_id))
    if not topic:
        raise not_found("Memory topic not found")
    fingerprint = hashlib.sha256(_normalized(topic["topic"]).encode()).hexdigest()
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO memory_tombstones(id, tenant_id, subject_id, topic, fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (db.new_id(), ctx.tenant_id, ctx.user_id, topic["topic"], fingerprint, db.utcnow()),
        )
        conn.execute("DELETE FROM memory_topic_stats WHERE id=?", (topic_id,))
    return ok(message="Memory topic removed")


@router.get("/memory/documents")
def list_documents(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    page_no, size, offset = pagination(request.query_params.get("limit"), request.query_params.get("offset"), 500)
    total = int(db.fetch_one("SELECT COUNT(*) AS n FROM memory_doc_affinity WHERE tenant_id=? AND subject_id=?", (ctx.tenant_id, ctx.user_id))["n"])
    rows = db.fetch_all(
        "SELECT * FROM memory_doc_affinity WHERE tenant_id=? AND subject_id=? ORDER BY hits DESC, last_used_at DESC LIMIT ? OFFSET ?",
        (ctx.tenant_id, ctx.user_id, size, offset),
    )
    return {**ok(rows), "total": total}


@router.delete("/memory/documents/{document_id}")
def delete_document(document_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    with db.transaction() as conn:
        conn.execute("DELETE FROM memory_doc_affinity WHERE id=? AND tenant_id=? AND subject_id=?", (document_id, ctx.tenant_id, ctx.user_id))
    return ok(message="Memory document removed")

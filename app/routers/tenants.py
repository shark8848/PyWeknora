"""Workspaces, members, invitations and tenant-scoped settings."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

from .. import config, db
from ..api_keys import hash_token, normalize_capabilities, normalize_knowledge_base_ids
from ..deps import parse_auth, require_role, tenant_json, user_json
from ..errors import conflict, forbidden, not_found, validation
from ..utils import dumps, loads, ok, pagination, truthy


router = APIRouter(tags=["tenants"])


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _tenant(ctx: Any, tenant_id: int) -> dict[str, Any]:
    if tenant_id != ctx.tenant_id and not (
        config.ENABLE_CROSS_TENANT_ACCESS and ctx.user.get("can_access_all_tenants")
    ) and not (ctx.api_key is not None and ctx.api_key.is_platform):
        member = db.fetch_one(
            """
            SELECT 1 AS ok FROM tenant_members
            WHERE user_id=? AND tenant_id=? AND status='active' AND deleted_at IS NULL
            """,
            (ctx.user_id, tenant_id),
        )
        if not member:
            raise forbidden("No access to this workspace")
    row = db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (tenant_id,))
    if not row:
        raise not_found("Workspace not found")
    return row


def _require_cross_tenant(ctx: Any) -> None:
    if ctx.api_key is not None and ctx.api_key.is_platform:
        return
    if not config.ENABLE_CROSS_TENANT_ACCESS:
        raise forbidden("Cross-workspace access is disabled")
    if not ctx.user.get("can_access_all_tenants"):
        raise forbidden("Insufficient permissions for cross-workspace operation")


def _member_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": row["user_id"],
        "email": row.get("email") or "",
        "username": row.get("username") or "",
        "avatar": row.get("avatar") or "",
        "role": row["role"],
        "status": row["status"],
        "invited_by": row.get("invited_by"),
        "joined_at": row.get("joined_at"),
    }


def _members(tenant_id: int, keyword: str = "", limit: int | None = None, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    clauses = ["tm.tenant_id=?", "tm.deleted_at IS NULL"]
    params: list[Any] = [tenant_id]
    if keyword:
        clauses.append("(lower(u.username) LIKE ? OR lower(u.email) LIKE ?)")
        needle = f"%{keyword.lower()}%"
        params.extend([needle, needle])
    where = " AND ".join(clauses)
    total = db.fetch_one(
        f"SELECT COUNT(*) AS n FROM tenant_members tm JOIN users u ON u.id=tm.user_id WHERE {where}",
        params,
    )["n"]
    sql = (
        f"SELECT tm.*, u.username, u.email, u.avatar FROM tenant_members tm "
        f"JOIN users u ON u.id=tm.user_id WHERE {where} ORDER BY tm.role DESC, tm.created_at"
    )
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = db.fetch_all(sql, params)
    return [_member_json(row) for row in rows], int(total)


@router.get("/tenants")
def list_tenants(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok({"items": [tenant_json(ctx.tenant)] if ctx.tenant else []})


@router.get("/tenants/all")
def all_tenants(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _require_cross_tenant(ctx)
    rows = db.fetch_all("SELECT * FROM tenants WHERE deleted_at IS NULL ORDER BY id")
    return ok({"items": [tenant_json(row) for row in rows]})


@router.get("/tenants/search")
def search_tenants(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _require_cross_tenant(ctx)
    p, size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 100)
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    keyword = (request.query_params.get("keyword") or "").strip()
    if keyword:
        clauses.append("(lower(name) LIKE ? OR lower(COALESCE(description,'')) LIKE ?)")
        needle = f"%{keyword.lower()}%"
        params.extend([needle, needle])
    if request.query_params.get("tenant_id") and request.query_params["tenant_id"].isdigit():
        clauses.append("id=?")
        params.append(int(request.query_params["tenant_id"]))
    where = " AND ".join(clauses)
    total = db.fetch_one(f"SELECT COUNT(*) AS n FROM tenants WHERE {where}", params)["n"]
    rows = db.fetch_all(f"SELECT * FROM tenants WHERE {where} ORDER BY id LIMIT ? OFFSET ?", [*params, size, offset])
    return ok(
        {
            "items": [tenant_json(row) for row in rows],
            "total": int(total),
            "page": p,
            "page_size": size,
        }
    )


@router.post("/tenants", status_code=201)
async def create_tenant(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("Workspace name is required")
    now = db.utcnow()
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO tenants(name, description, retriever_engines, status, business,
              storage_quota, storage_used, created_at, updated_at)
            VALUES (?, ?, ?, 'active', 'knowledge_management', ?, 0, ?, ?)
            """,
            (name, str(data.get("description") or ""), dumps({"engines": []}), 10 * 1024**3, now, now),
        )
        tenant_id = int(cur.lastrowid)
        conn.execute(
            """
            INSERT INTO tenant_members(user_id, tenant_id, role, status, joined_at, created_at, updated_at)
            VALUES (?, ?, 'owner', 'active', ?, ?, ?)
            """,
            (ctx.user_id, tenant_id, now, now, now),
        )
    return ok(tenant_json(_tenant(ctx, tenant_id), "owner"))


@router.get("/tenants/{tenant_id}")
def get_tenant(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(tenant_json(_tenant(ctx, tenant_id)))


@router.put("/tenants/{tenant_id}")
async def update_tenant(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise validation("Workspace name is required")
        updates["name"] = name
    if "description" in data:
        updates["description"] = str(data.get("description") or "")
    with db.transaction() as conn:
        db.update("tenants", updates, "id=?", (tenant_id,), conn)
    return ok(tenant_json(db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))))


@router.delete("/tenants/{tenant_id}")
def delete_tenant(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    if tenant_id == int(ctx.user.get("tenant_id") or 0):
        raise validation("The home workspace cannot be deleted")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE tenants SET deleted_at=?, updated_at=? WHERE id=?", (now, now, tenant_id))
        conn.execute("UPDATE tenant_members SET deleted_at=?, updated_at=? WHERE tenant_id=?", (now, now, tenant_id))
    return ok(message="Workspace deleted")


@router.get("/tenants/{tenant_id}/members")
def list_members(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    p, size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 100)
    members, total = _members(tenant_id, (request.query_params.get("q") or "").strip(), size, offset)
    return ok({"members": members, "items": members, "total": total, "page": p, "page_size": size})


@router.post("/tenants/{tenant_id}/members", status_code=201)
async def add_member(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    email = str(data.get("email") or "").strip().lower()
    role = str(data.get("role") or "viewer")
    user = db.fetch_one("SELECT * FROM users WHERE lower(email)=? AND deleted_at IS NULL", (email,))
    if not user:
        raise not_found("No registered user with this email")
    existing = db.fetch_one("SELECT * FROM tenant_members WHERE user_id=? AND tenant_id=? AND deleted_at IS NULL", (user["id"], tenant_id))
    if existing:
        raise conflict("User is already a member")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tenant_members(user_id, tenant_id, role, status, invited_by, joined_at, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
            (user["id"], tenant_id, role, ctx.user_id, now, now, now),
        )
    members, _ = _members(tenant_id, email)
    return ok(members[0] if members else None)


@router.put("/tenants/{tenant_id}/members/{user_id}")
async def update_member(tenant_id: int, user_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    role = str(data.get("role") or "viewer")
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_members SET role=?, updated_at=? WHERE tenant_id=? AND user_id=? AND deleted_at IS NULL", (role, db.utcnow(), tenant_id, user_id))
    return ok(message="Member updated")


@router.delete("/tenants/{tenant_id}/members/{user_id}")
def remove_member(tenant_id: int, user_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_members SET deleted_at=?, updated_at=? WHERE tenant_id=? AND user_id=?", (now, now, tenant_id, user_id))
    return ok(message="Member removed")


@router.post("/tenants/{tenant_id}/leave")
def leave_tenant(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    if tenant_id == int(ctx.user.get("tenant_id") or 0):
        raise validation("The home workspace cannot be left")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_members SET deleted_at=?, updated_at=? WHERE tenant_id=? AND user_id=?", (now, now, tenant_id, ctx.user_id))
    return ok(message="Left workspace")


def _invitation_json(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result.pop("token", None)
    if result.get("invite_url"):
        pass
    return result


@router.get("/tenants/{tenant_id}/invitations")
def list_invitations(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    p, size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 100)
    clauses = ["i.tenant_id=?", "i.deleted_at IS NULL"]
    params: list[Any] = [tenant_id]
    if not truthy(request.query_params.get("include_terminal")):
        clauses.append("i.status='pending'")
    where = " AND ".join(clauses)
    total = db.fetch_one(f"SELECT COUNT(*) AS n FROM tenant_invitations i WHERE {where}", params)["n"]
    rows = db.fetch_all(
        f"""
        SELECT i.*, u.email AS invitee_email, u.username AS invitee_username, t.name AS tenant_name
        FROM tenant_invitations i
        LEFT JOIN users u ON u.id=i.invitee_user_id
        JOIN tenants t ON t.id=i.tenant_id
        WHERE {where} ORDER BY i.id DESC LIMIT ? OFFSET ?
        """,
        [*params, size, offset],
    )
    return ok({"items": [_invitation_json(row) for row in rows], "invitations": [_invitation_json(row) for row in rows], "total": total, "page": p, "page_size": size})


@router.post("/tenants/{tenant_id}/invitations", status_code=201)
async def create_invitation(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    email = str(data.get("email") or "").strip().lower()
    user = db.fetch_one("SELECT * FROM users WHERE lower(email)=? AND deleted_at IS NULL", (email,))
    if not user:
        raise not_found("No registered user with this email")
    role = str(data.get("role") or "viewer")
    token = secrets.token_urlsafe(32)
    now = db.utcnow()
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO tenant_invitations(tenant_id, invitee_user_id, invited_by, role,
              status, message, expires_at, created_at, updated_at, token, accepted_count)
            VALUES (?, ?, ?, ?, 'pending', ?, datetime('now', '+7 days'), ?, ?, ?, 0)
            """,
            (tenant_id, user["id"], ctx.user_id, role, str(data.get("message") or ""), now, now, token),
        )
        invitation_id = int(cur.lastrowid)
    row = db.fetch_one(
        "SELECT i.*, u.email AS invitee_email, u.username AS invitee_username, t.name AS tenant_name FROM tenant_invitations i LEFT JOIN users u ON u.id=i.invitee_user_id JOIN tenants t ON t.id=i.tenant_id WHERE i.id=?",
        (invitation_id,),
    )
    return ok(_invitation_json(row))


@router.post("/tenants/{tenant_id}/invite-links", status_code=201)
async def create_invite_link(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    role = str(data.get("role") or "viewer")
    token = secrets.token_urlsafe(32)
    now = db.utcnow()
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO tenant_invitations(tenant_id, invitee_user_id, invited_by, role,
              status, message, expires_at, created_at, updated_at, token, accepted_count)
            VALUES (?, '', ?, ?, 'pending', ?, datetime('now', '+30 days'), ?, ?, ?, 0)
            """,
            (tenant_id, ctx.user_id, role, str(data.get("message") or ""), now, now, token),
        )
        invitation_id = int(cur.lastrowid)
    row = db.fetch_one("SELECT i.*, t.name AS tenant_name FROM tenant_invitations i JOIN tenants t ON t.id=i.tenant_id WHERE i.id=?", (invitation_id,))
    row["token"] = token
    row["invite_url"] = f"/register?token={token}"
    return ok(row)


@router.delete("/tenants/{tenant_id}/invitations/{invitation_id}")
def revoke_invitation(tenant_id: int, invitation_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_invitations SET status='revoked', deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), invitation_id, tenant_id))
    return ok(message="Invitation revoked")


@router.get("/me/invitations")
def my_invitations(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    clauses = ["i.invitee_user_id=?", "i.deleted_at IS NULL"]
    if not truthy(request.query_params.get("include_terminal")):
        clauses.append("i.status='pending'")
    rows = db.fetch_all(
        f"SELECT i.*, t.name AS tenant_name FROM tenant_invitations i JOIN tenants t ON t.id=i.tenant_id WHERE {' AND '.join(clauses)} ORDER BY i.id DESC",
        (ctx.user_id,),
    )
    return ok({"invitations": [_invitation_json(row) for row in rows], "total": len(rows)})


@router.get("/me/invitations/pending-count")
def pending_invitations(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    count = db.fetch_one("SELECT COUNT(*) AS n FROM tenant_invitations WHERE invitee_user_id=? AND status='pending' AND deleted_at IS NULL", (ctx.user_id,))["n"]
    return ok({"pending_count": int(count)})


def _accept_invitation(ctx: Any, invitation: dict[str, Any]) -> None:
    now = db.utcnow()
    with db.transaction() as conn:
        existing = conn.execute("SELECT id FROM tenant_members WHERE user_id=? AND tenant_id=? AND deleted_at IS NULL", (ctx.user_id, invitation["tenant_id"])).fetchone()
        if existing:
            conn.execute("UPDATE tenant_members SET role=?, status='active', updated_at=? WHERE id=?", (invitation["role"], now, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO tenant_members(user_id, tenant_id, role, status, joined_at, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?, ?)",
                (ctx.user_id, invitation["tenant_id"], invitation["role"], now, now, now),
            )
        conn.execute("UPDATE tenant_invitations SET status='accepted', responded_at=?, updated_at=? WHERE id=?", (now, now, invitation["id"]))


@router.post("/me/invitations/{invitation_id}/accept")
def accept_invitation(invitation_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    invitation = db.fetch_one("SELECT * FROM tenant_invitations WHERE id=? AND invitee_user_id=? AND status='pending' AND deleted_at IS NULL", (invitation_id, ctx.user_id))
    if not invitation:
        raise not_found("Invitation not found")
    _accept_invitation(ctx, invitation)
    return ok({"tenant_id": invitation["tenant_id"], "role": invitation["role"], "status": "active"})


@router.post("/me/invitations/accept-by-token")
async def accept_by_token(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    invitation = db.fetch_one("SELECT * FROM tenant_invitations WHERE token=? AND status='pending' AND deleted_at IS NULL", (str(data.get("token") or ""),))
    if not invitation:
        raise not_found("Invitation not found")
    _accept_invitation(ctx, invitation)
    return ok({"tenant_id": invitation["tenant_id"], "role": invitation["role"]})


@router.post("/me/invitations/{invitation_id}/decline")
def decline_invitation(invitation_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_invitations SET status='declined', responded_at=?, updated_at=? WHERE id=? AND invitee_user_id=?", (db.utcnow(), db.utcnow(), invitation_id, ctx.user_id))
    return ok(message="Invitation declined")


@router.get("/tenants/kv/prompt-templates")
def get_prompt_templates(request: Request) -> dict[str, Any]:
    return get_tenant_kv("prompt-templates", request)


@router.get("/tenants/kv/web-search-config")
def get_web_search_config(request: Request) -> dict[str, Any]:
    return get_tenant_kv("web-search-config", request)


@router.get("/tenants/kv/{key}")
def get_tenant_kv(key: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    columns = {
        "prompt-templates": "api_principal_config",
        "web-search-config": "web_search_config",
        "retrieval-config": "retrieval_config",
        "storage-engine-config": "storage_engine_config",
        "parser-engine-config": "parser_engine_config",
        "chat-history-config": "chat_history_config",
        "memory-config": "memory_config",
    }
    column = columns.get(key)
    if not column:
        raise not_found("Unknown configuration key")
    value = loads(ctx.tenant.get(column), {})
    if key == "prompt-templates" and not value:
        value = {}
    return ok(value)


@router.put("/tenants/kv/{key}")
async def update_tenant_kv(key: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    columns = {
        "prompt-templates": "api_principal_config",
        "web-search-config": "web_search_config",
        "retrieval-config": "retrieval_config",
        "storage-engine-config": "storage_engine_config",
        "parser-engine-config": "parser_engine_config",
        "chat-history-config": "chat_history_config",
        "memory-config": "memory_config",
    }
    column = columns.get(key)
    if not column:
        raise not_found("Unknown configuration key")
    data = await body(request)
    with db.transaction() as conn:
        db.update("tenants", {column: dumps(data), "updated_at": db.utcnow()}, "id=?", (ctx.tenant_id,), conn)
    return ok(data)


@router.get("/tenants/{tenant_id}/api-principal-config")
def api_principal_config(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    value = loads(row.get("api_principal_config"), {})
    value.setdefault("mode", "tenant")
    value.setdefault("direct_header_name", "X-External-User-ID")
    value.setdefault("signed_token_header_name", "X-API-Principal")
    value.setdefault("require_direct_header", False)
    value.setdefault("has_hmac_secret", bool(value.get("hmac_secret")))
    value.pop("hmac_secret", None)
    return ok(value)


@router.put("/tenants/{tenant_id}/api-principal-config")
async def update_api_principal_config(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    with db.transaction() as conn:
        db.update("tenants", {"api_principal_config": dumps(data), "updated_at": db.utcnow()}, "id=?", (tenant_id,), conn)
    return ok({key: value for key, value in data.items() if key != "hmac_secret"})


@router.post("/tenants/{tenant_id}/api-principal-test-token")
async def api_principal_test_token(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    return ok({"token": secrets.token_urlsafe(32), "header_name": "X-API-Principal", "expires_in_seconds": int(data.get("expires_in_seconds") or 3600), "expires_at_unix": 0, "external_user_id": data.get("external_user_id")})


def _api_key_json(row: dict[str, Any], reveal: bool = False) -> dict[str, Any]:
    result = dict(row)
    result["knowledge_base_ids"] = loads(result.get("knowledge_base_ids"), [])
    result["capabilities"] = loads(result.get("capabilities"), [])
    result["full_access"] = bool(result.get("full_access"))
    result["api_key"] = result.get("api_key") or ("••••" + result.get("key_hash", "")[-4:])
    result.pop("key_hash", None)
    result.pop("revoked_at", None)
    return result


def _api_key_expiry(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise validation("Invalid expires_at") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@router.get("/tenants/{tenant_id}/api-keys")
def list_api_keys(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    rows = db.fetch_all("SELECT * FROM tenant_api_keys WHERE tenant_id=? AND revoked_at IS NULL ORDER BY id", (tenant_id,))
    return ok([_api_key_json(row) for row in rows])


@router.post("/tenants/{tenant_id}/api-keys", status_code=201)
async def create_api_key(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    token = "wk_" + secrets.token_urlsafe(32)
    now = db.utcnow()
    full_access = bool(data.get("full_access"))
    knowledge_base_ids = [] if full_access else normalize_knowledge_base_ids(data.get("knowledge_base_ids"))
    capabilities = [] if full_access else normalize_capabilities(data.get("capabilities"))
    with db.transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO tenant_api_keys(tenant_id, scope_type, name, key_hash, api_key,
              full_access, knowledge_base_ids, capabilities, expires_at, created_at, updated_at)
            VALUES (?, 'tenant', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant_id, str(data.get("name") or "API Key"), hash_token(token), token,
                1 if full_access else 0, dumps(knowledge_base_ids), dumps(capabilities),
                _api_key_expiry(data.get("expires_at")), now, now,
            ),
        )
        key_id = int(cur.lastrowid)
    row = db.fetch_one("SELECT * FROM tenant_api_keys WHERE id=?", (key_id,))
    result = _api_key_json(row)
    result["token"] = token
    return ok(result)


def hashlib_sha256(value: str) -> str:
    return hash_token(value)


@router.put("/tenants/{tenant_id}/api-keys/{key_id}")
async def update_api_key(tenant_id: int, key_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "full_access"):
        if key in data:
            updates[key] = 1 if data[key] else 0
    if "knowledge_base_ids" in data:
        updates["knowledge_base_ids"] = dumps(normalize_knowledge_base_ids(data["knowledge_base_ids"]))
    if "capabilities" in data:
        updates["capabilities"] = dumps(normalize_capabilities(data["capabilities"]))
    if "expires_at" in data:
        updates["expires_at"] = _api_key_expiry(data["expires_at"])
    if data.get("full_access"):
        updates["knowledge_base_ids"] = "[]"
        updates["capabilities"] = "[]"
    with db.transaction() as conn:
        db.update("tenant_api_keys", updates, "id=? AND tenant_id=? AND scope_type='tenant' AND revoked_at IS NULL", (key_id, tenant_id), conn)
    row = db.fetch_one(
        "SELECT * FROM tenant_api_keys WHERE id=? AND tenant_id=? AND scope_type='tenant' AND revoked_at IS NULL",
        (key_id, tenant_id),
    )
    if not row:
        raise not_found("API key not found")
    return ok(_api_key_json(row))


@router.delete("/tenants/{tenant_id}/api-keys/{key_id}")
def delete_api_key(tenant_id: int, key_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "owner")
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_api_keys SET revoked_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), key_id, tenant_id))
    return ok(message="API key revoked")


@router.get("/tenants/{tenant_id}/audit-log")
def tenant_audit(tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _tenant(ctx, tenant_id)
    if tenant_id == ctx.tenant_id:
        require_role(ctx, "admin")
    try:
        after_id = max(0, int(request.query_params.get("after_id") or 0))
    except ValueError:
        after_id = 0
    try:
        limit = min(100, max(1, int(request.query_params.get("limit") or 50)))
    except ValueError:
        limit = 50
    clauses = ["tenant_id=?", "id<?"]
    params: list[Any] = [tenant_id, after_id or 2**63 - 1]
    for column, query_key in (("action", "action"), ("outcome", "outcome"), ("actor_user_id", "actor")):
        value = str(request.query_params.get(query_key) or "").strip()
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    rows = db.fetch_all(
        f"SELECT * FROM audit_logs WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    )
    return {"success": True, "data": rows, "next_cursor": int(rows[-1]["id"]) if rows else 0}

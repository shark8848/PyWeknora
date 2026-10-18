"""Organizations and cross-workspace knowledge-base / agent sharing."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request

from .. import db
from ..deps import AuthContext, parse_auth, require_role, user_json
from ..errors import conflict, forbidden, not_found, validation
from ..utils import dumps, loads, ok


router = APIRouter(tags=["organizations"])


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _org(org_id: str) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM organizations WHERE id=? AND deleted_at IS NULL", (org_id,))
    if not row:
        raise not_found("Organization not found")
    return row


def _membership(org_id: str, tenant_id: int) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT * FROM organization_tenant_members WHERE organization_id=? AND tenant_id=?",
        (org_id, tenant_id),
    )


def _org_role(org: dict[str, Any], ctx: AuthContext) -> str:
    if int(org.get("owner_tenant_id") or 0) == ctx.tenant_id:
        return "admin"
    member = _membership(org["id"], ctx.tenant_id)
    return str(member["role"]) if member else ""


def _org_admin(org: dict[str, Any], ctx: AuthContext) -> None:
    role = _org_role(org, ctx)
    if role not in {"admin", "owner"}:
        raise forbidden("Organization administrator access required")


def _owned_or_admin(ctx: AuthContext, owner_id: Any) -> None:
    if owner_id and str(owner_id) == ctx.user_id:
        return
    require_role(ctx, "admin")


def _member_count(org_id: str) -> int:
    return int(db.fetch_one("SELECT COUNT(*) AS n FROM organization_tenant_members WHERE organization_id=?", (org_id,))["n"])


def _share_count(org_id: str) -> int:
    return int(db.fetch_one("SELECT COUNT(*) AS n FROM kb_shares WHERE organization_id=? AND deleted_at IS NULL", (org_id,))["n"])


def _agent_share_count(org_id: str) -> int:
    return int(db.fetch_one("SELECT COUNT(*) AS n FROM agent_shares WHERE organization_id=? AND deleted_at IS NULL", (org_id,))["n"])


def _pending_count(org_id: str) -> int:
    return int(db.fetch_one("SELECT COUNT(*) AS n FROM organization_join_requests WHERE organization_id=? AND status='pending'", (org_id,))["n"])


def _serialize_org(org: dict[str, Any], ctx: AuthContext) -> dict[str, Any]:
    result = dict(org)
    result["member_count"] = _member_count(org["id"])
    result["share_count"] = _share_count(org["id"])
    result["agent_share_count"] = _agent_share_count(org["id"])
    result["pending_join_request_count"] = _pending_count(org["id"])
    result["require_approval"] = bool(result.get("require_approval"))
    result["searchable"] = bool(result.get("searchable"))
    role = _org_role(org, ctx)
    result["is_owner"] = int(org.get("owner_tenant_id") or 0) == ctx.tenant_id
    result["my_role"] = role
    result["has_pending_upgrade"] = bool(db.fetch_one(
        "SELECT 1 FROM organization_join_requests WHERE organization_id=? AND tenant_id=? AND request_type='upgrade' AND status='pending'",
        (org["id"], ctx.tenant_id),
    ))
    result.pop("deleted_at", None)
    return result


def _tenant_name(tenant_id: int) -> str:
    row = db.fetch_one("SELECT name FROM tenants WHERE id=? AND deleted_at IS NULL", (tenant_id,))
    return row["name"] if row else ""


def _tenant_rep(tenant_id: int) -> dict[str, Any] | None:
    return db.fetch_one(
        "SELECT u.* FROM users u JOIN tenant_members tm ON tm.user_id=u.id "
        "WHERE tm.tenant_id=? AND tm.status='active' AND tm.deleted_at IS NULL "
        "ORDER BY CASE tm.role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END, u.created_at LIMIT 1",
        (tenant_id,),
    )


def _member_json(row: dict[str, Any]) -> dict[str, Any]:
    user = db.fetch_one("SELECT id, username, email, avatar FROM users WHERE id=?", (row.get("representative_user_id"),))
    return {
        "id": row["id"],
        "user_id": row.get("representative_user_id") or "",
        "representative_user_id": row.get("representative_user_id") or "",
        "username": user.get("username") or "" if user else "",
        "email": user.get("email") or "" if user else "",
        "avatar": (user.get("avatar") or "") if user else "",
        "role": row["role"],
        "tenant_id": int(row["tenant_id"]),
        "tenant_name": _tenant_name(int(row["tenant_id"])),
        "joined_at": row.get("joined_at") or row.get("created_at"),
    }


def _share_json(row: dict[str, Any], ctx: AuthContext) -> dict[str, Any]:
    kb = db.fetch_one("SELECT * FROM knowledge_bases WHERE id=? AND deleted_at IS NULL", (row["knowledge_base_id"],))
    org = db.fetch_one("SELECT name FROM organizations WHERE id=?", (row["organization_id"],))
    sharer = db.fetch_one("SELECT username, email FROM users WHERE id=?", (row["shared_by_user_id"],))
    return {
        "id": row["id"], "knowledge_base_id": row["knowledge_base_id"],
        "knowledge_base_name": kb.get("name") if kb else "", "knowledge_base_type": kb.get("type") if kb else "",
        "knowledge_count": int(db.fetch_one("SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL", (row["knowledge_base_id"],))["n"]),
        "chunk_count": int(db.fetch_one("SELECT COUNT(*) AS n FROM chunks WHERE knowledge_base_id=? AND deleted_at IS NULL", (row["knowledge_base_id"],))["n"]),
        "organization_id": row["organization_id"], "organization_name": org["name"] if org else "",
        "shared_by_user_id": row["shared_by_user_id"],
        "shared_by_username": (sharer.get("username") or sharer.get("email") or "") if sharer else "",
        "source_tenant_id": int(row["source_tenant_id"]), "permission": row["permission"],
        "my_role_in_org": _org_role(_org(row["organization_id"]), ctx), "my_permission": row["permission"],
        "created_at": row.get("created_at"), "is_mine": int(row["source_tenant_id"]) == ctx.tenant_id,
    }


def _agent_share_json(row: dict[str, Any], ctx: AuthContext) -> dict[str, Any]:
    agent = db.fetch_one(
        "SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (row["agent_id"], row["source_tenant_id"]),
    )
    org = db.fetch_one("SELECT name FROM organizations WHERE id=?", (row["organization_id"],))
    sharer = db.fetch_one("SELECT username, email FROM users WHERE id=?", (row["shared_by_user_id"],))
    return {
        "id": row["id"], "agent_id": row["agent_id"], "agent_name": agent.get("name") if agent else "",
        "agent_avatar": agent.get("avatar") if agent else "", "organization_id": row["organization_id"],
        "organization_name": org["name"] if org else "", "shared_by_user_id": row["shared_by_user_id"],
        "shared_by_username": (sharer.get("username") or sharer.get("email") or "") if sharer else "",
        "source_tenant_id": int(row["source_tenant_id"]), "permission": row["permission"],
        "my_role_in_org": _org_role(_org(row["organization_id"]), ctx), "my_permission": row["permission"],
        "created_at": row.get("created_at"), "is_mine": int(row["source_tenant_id"]) == ctx.tenant_id,
    }


@router.get("/organizations")
def list_organizations(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT o.* FROM organizations o JOIN organization_tenant_members m ON m.organization_id=o.id "
        "WHERE m.tenant_id=? AND o.deleted_at IS NULL ORDER BY o.name",
        (ctx.tenant_id,),
    )
    return ok({
        "organizations": [_serialize_org(row, ctx) for row in rows],
        "total": len(rows),
        "resource_counts": {
            "knowledge_bases": {"by_organization": {row["id"]: _share_count(row["id"]) for row in rows}},
            "agents": {"by_organization": {row["id"]: _agent_share_count(row["id"]) for row in rows}},
        },
    })


@router.post("/organizations", status_code=201)
async def create_organization(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("Organization name is required")
    org_id, now = db.new_id(), db.utcnow()
    invite_code = secrets.token_urlsafe(12)
    days = int(data.get("invite_code_validity_days") or 7)
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z") if days else None
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO organizations(id, name, description, owner_id, owner_tenant_id,
              invite_code, require_approval, invite_code_expires_at,
              invite_code_validity_days, avatar, searchable, member_limit,
              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?, ?)
            """,
            (org_id, name, str(data.get("description") or ""), ctx.user_id, ctx.tenant_id,
             invite_code, expires, days, str(data.get("avatar") or ""),
             int(data.get("member_limit") or 50), now, now),
        )
        conn.execute(
            "INSERT INTO organization_tenant_members(id, organization_id, tenant_id, role, representative_user_id, joined_at, created_at, updated_at) VALUES (?, ?, ?, 'admin', ?, ?, ?, ?)",
            (db.new_id(), org_id, ctx.tenant_id, ctx.user_id, now, now, now),
        )
    return ok(_serialize_org(_org(org_id), ctx))


@router.get("/organizations/search")
def search_organizations(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    q = (request.query_params.get("q") or "").strip()
    limit = min(100, max(1, int(request.query_params.get("limit") or 20)))
    rows = db.fetch_all(
        "SELECT * FROM organizations WHERE deleted_at IS NULL AND searchable=1 AND (name LIKE ? OR description LIKE ?) ORDER BY name LIMIT ?",
        (f"%{q}%", f"%{q}%", limit),
    )
    items = []
    for row in rows:
        item = _serialize_org(row, ctx)
        item["is_already_member"] = bool(_membership(row["id"], ctx.tenant_id))
        items.append(item)
    return ok({"data": items, "total": len(items)})


@router.get("/organizations/preview/{code}")
def preview_organization(code: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request, required=False)
    row = db.fetch_one("SELECT * FROM organizations WHERE invite_code=? AND deleted_at IS NULL", (code,)) or _org(code)
    assert_ctx = ctx or AuthContext(user={}, tenant_id=0, tenant={}, role="", token_payload={})
    result = _serialize_org(row, assert_ctx)
    result["is_already_member"] = bool(ctx and _membership(row["id"], ctx.tenant_id))
    return ok(result)


@router.post("/organizations/join")
async def join_organization(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    org = db.fetch_one("SELECT * FROM organizations WHERE invite_code=? AND deleted_at IS NULL", (str(data.get("invite_code") or ""),))
    if not org:
        raise not_found("Invalid invitation code")
    if data.get("message") or data.get("role") or org.get("require_approval"):
        return await _submit_join(org, ctx, data)
    return _add_member(org, ctx, "viewer")


@router.post("/organizations/join-request")
async def join_organization_request(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    org = db.fetch_one("SELECT * FROM organizations WHERE invite_code=? AND deleted_at IS NULL", (str(data.get("invite_code") or ""),))
    if not org:
        raise not_found("Invalid invitation code")
    return await _submit_join(org, ctx, data)


@router.post("/organizations/join-by-id")
async def join_organization_by_id(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    org = _org(str(data.get("organization_id") or ""))
    if not org.get("searchable") and not org.get("invite_code"):
        raise forbidden("Organization is not discoverable")
    if org.get("require_approval") or data.get("message") or data.get("role"):
        return await _submit_join(org, ctx, data)
    return _add_member(org, ctx, "viewer")


async def _submit_join(org: dict[str, Any], ctx: AuthContext, data: dict[str, Any]) -> dict[str, Any]:
    if _membership(org["id"], ctx.tenant_id):
        return ok(_serialize_org(org, ctx))
    role = str(data.get("role") or "viewer")
    if role not in {"admin", "editor", "viewer"}:
        role = "viewer"
    existing = db.fetch_one(
        "SELECT * FROM organization_join_requests WHERE organization_id=? AND tenant_id=? AND request_type='join' AND status='pending'",
        (org["id"], ctx.tenant_id),
    )
    if existing:
        return ok({"request_id": existing["id"], "status": "pending"})
    request_id, now = db.new_id(), db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO organization_join_requests(id, organization_id, user_id, tenant_id, status, requested_role, request_type, message, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, 'join', ?, ?, ?)",
            (request_id, org["id"], ctx.user_id, ctx.tenant_id, role, str(data.get("message") or ""), now, now),
        )
    return ok({"request_id": request_id, "status": "pending"})


def _add_member(org: dict[str, Any], ctx: AuthContext, role: str) -> dict[str, Any]:
    if _membership(org["id"], ctx.tenant_id):
        return ok(_serialize_org(org, ctx))
    limit = int(org.get("member_limit") or 0)
    if limit and _member_count(org["id"]) >= limit:
        raise conflict("Organization member limit reached")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO organization_tenant_members(id, organization_id, tenant_id, role, representative_user_id, joined_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (db.new_id(), org["id"], ctx.tenant_id, role, ctx.user_id, now, now, now),
        )
    return ok(_serialize_org(org, ctx))


@router.get("/organizations/{org_id}")
def get_organization(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    org = _org(org_id)
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    return ok(_serialize_org(org, ctx))


@router.put("/organizations/{org_id}")
async def update_organization(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    for key in ("name", "description", "avatar"):
        if key in data:
            updates[key] = str(data.get(key) or "")
    for key in ("require_approval", "searchable"):
        if key in data:
            updates[key] = 1 if data[key] else 0
    for key in ("invite_code_validity_days", "member_limit"):
        if key in data:
            updates[key] = int(data[key] or 0)
    with db.transaction() as conn:
        db.update("organizations", updates, "id=?", (org_id,), conn)
    return ok(_serialize_org(_org(org_id), ctx))


@router.delete("/organizations/{org_id}")
def delete_organization(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    if int(org.get("owner_tenant_id") or 0) != ctx.tenant_id:
        raise forbidden("Only the owning workspace can delete this organization")
    with db.transaction() as conn:
        conn.execute("UPDATE organizations SET deleted_at=?, updated_at=? WHERE id=?", (db.utcnow(), db.utcnow(), org_id))
    return ok(message="Organization deleted")


@router.post("/organizations/{org_id}/leave")
def leave_organization(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    if int(org.get("owner_tenant_id") or 0) == ctx.tenant_id:
        raise validation("The owning workspace cannot leave the organization")
    with db.transaction() as conn:
        conn.execute("DELETE FROM organization_tenant_members WHERE organization_id=? AND tenant_id=?", (org_id, ctx.tenant_id))
    return ok(message="Left organization")


@router.post("/organizations/{org_id}/invite-code")
def generate_invite_code(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    code = secrets.token_urlsafe(12)
    days = int(org.get("invite_code_validity_days") or 7)
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z") if days else None
    with db.transaction() as conn:
        conn.execute("UPDATE organizations SET invite_code=?, invite_code_expires_at=?, updated_at=? WHERE id=?", (code, expires, db.utcnow(), org_id))
    return ok({"invite_code": code, "invite_code_expires_at": expires})


@router.get("/organizations/{org_id}/members")
def list_members(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    org = _org(org_id)
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    rows = db.fetch_all("SELECT * FROM organization_tenant_members WHERE organization_id=? ORDER BY role, joined_at", (org_id,))
    return ok({"members": [_member_json(row) for row in rows], "total": len(rows)})


@router.post("/organizations/{org_id}/invite")
async def invite_member(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    data = await body(request)
    tenant_id = int(data.get("tenant_id") or 0)
    representative = str(data.get("representative_user_id") or data.get("user_id") or "")
    if not tenant_id and representative:
        user = db.fetch_one("SELECT tenant_id FROM users WHERE id=?", (representative,))
        tenant_id = int(user["tenant_id"]) if user else 0
    if not tenant_id:
        raise validation("tenant_id is required")
    if _membership(org_id, tenant_id):
        raise conflict("Workspace is already a member")
    role = str(data.get("role") or "viewer")
    if role not in {"admin", "editor", "viewer"}:
        role = "viewer"
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO organization_tenant_members(id, organization_id, tenant_id, role, representative_user_id, joined_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (db.new_id(), org_id, tenant_id, role, representative or (_tenant_rep(tenant_id) or {}).get("id", ""), now, now, now),
        )
    return ok(message="Member invited")


@router.put("/organizations/{org_id}/members/{tenant_id}")
async def update_member_role(org_id: str, tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    data = await body(request)
    role = str(data.get("role") or "viewer")
    if role not in {"admin", "editor", "viewer"}:
        raise validation("Invalid role")
    with db.transaction() as conn:
        conn.execute("UPDATE organization_tenant_members SET role=?, updated_at=? WHERE organization_id=? AND tenant_id=?", (role, db.utcnow(), org_id, tenant_id))
    return ok(message="Member role updated")


@router.delete("/organizations/{org_id}/members/{tenant_id}")
def remove_member(org_id: str, tenant_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    if tenant_id == int(org.get("owner_tenant_id") or 0):
        raise validation("The owning workspace cannot be removed")
    with db.transaction() as conn:
        conn.execute("DELETE FROM organization_tenant_members WHERE organization_id=? AND tenant_id=?", (org_id, tenant_id))
    return ok(message="Member removed")


@router.get("/organizations/{org_id}/join-requests")
def list_join_requests(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    rows = db.fetch_all("SELECT * FROM organization_join_requests WHERE organization_id=? AND status='pending' ORDER BY created_at", (org_id,))
    requests = []
    for row in rows:
        user = db.fetch_one("SELECT username, email FROM users WHERE id=?", (row["user_id"],))
        requests.append({**row, "username": user.get("username") if user else "", "email": user.get("email") if user else ""})
    return ok({"requests": requests, "total": len(requests)})


@router.put("/organizations/{org_id}/join-requests/{request_id}/review")
async def review_join_request(org_id: str, request_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    data = await body(request)
    row = db.fetch_one("SELECT * FROM organization_join_requests WHERE id=? AND organization_id=?", (request_id, org_id))
    if not row:
        raise not_found("Join request not found")
    approved = bool(data.get("approved"))
    role = str(data.get("role") or row.get("requested_role") or "viewer")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE organization_join_requests SET status=?, reviewed_by=?, reviewed_at=?, review_message=?, updated_at=? WHERE id=?",
            ("approved" if approved else "rejected", ctx.user_id, now, str(data.get("message") or ""), now, request_id),
        )
        if approved and row.get("request_type") == "join":
            existing = conn.execute("SELECT 1 FROM organization_tenant_members WHERE organization_id=? AND tenant_id=?", (org_id, row["tenant_id"])).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO organization_tenant_members(id, organization_id, tenant_id, role, representative_user_id, joined_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (db.new_id(), org_id, row["tenant_id"], role, row["user_id"], now, now, now),
                )
        elif approved and row.get("request_type") == "upgrade":
            conn.execute("UPDATE organization_tenant_members SET role=?, updated_at=? WHERE organization_id=? AND tenant_id=?", (role, now, org_id, row["tenant_id"]))
    return ok(message="Join request reviewed")


@router.post("/organizations/{org_id}/request-upgrade")
async def request_upgrade(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    member = _membership(org_id, ctx.tenant_id)
    if not member:
        raise forbidden("Not a member of this organization")
    data = await body(request)
    role = str(data.get("requested_role") or "editor")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO organization_join_requests(id, organization_id, user_id, tenant_id, status, requested_role, request_type, prev_role, message, created_at, updated_at) VALUES (?, ?, ?, ?, 'pending', ?, 'upgrade', ?, ?, ?, ?)",
            (db.new_id(), org_id, ctx.user_id, ctx.tenant_id, role, member["role"], str(data.get("message") or ""), now, now),
        )
    return ok(message="Upgrade request submitted")


@router.get("/organizations/{org_id}/search-tenants")
def search_tenants(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    org = _org(org_id)
    _org_admin(org, ctx)
    q = (request.query_params.get("q") or "").strip()
    limit = min(50, max(1, int(request.query_params.get("limit") or 10)))
    rows = db.fetch_all(
        "SELECT * FROM tenants WHERE deleted_at IS NULL AND (CAST(id AS TEXT)=? OR name LIKE ?) ORDER BY name LIMIT ?",
        (q, f"%{q}%", limit),
    )
    result = []
    for row in rows:
        if _membership(org_id, int(row["id"])):
            continue
        rep = _tenant_rep(int(row["id"])) or {}
        result.append({
            "tenant_id": int(row["id"]), "tenant_name": row["name"],
            "representative_user_id": rep.get("id") or "", "representative_username": rep.get("username") or "",
            "representative_email": rep.get("email") or "", "representative_avatar": rep.get("avatar") or "",
        })
    return ok(result)


@router.get("/organizations/{org_id}/search-users")
def search_users(org_id: str, request: Request) -> dict[str, Any]:
    return search_tenants(org_id, request)


def _kb_row(kb_id: str, ctx: AuthContext) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (kb_id, ctx.tenant_id))
    if not row:
        raise not_found("Knowledge base not found")
    return row


@router.post("/knowledge-bases/{kb_id}/shares", status_code=201)
async def share_knowledge_base(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb_row(kb_id, ctx)
    _owned_or_admin(ctx, kb.get("creator_id"))
    data = await body(request)
    org = _org(str(data.get("organization_id") or ""))
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    permission = str(data.get("permission") or "viewer")
    if permission not in {"admin", "editor", "viewer"}:
        permission = "viewer"
    existing = db.fetch_one("SELECT * FROM kb_shares WHERE knowledge_base_id=? AND organization_id=? AND deleted_at IS NULL", (kb_id, org["id"]))
    now = db.utcnow()
    with db.transaction() as conn:
        if existing:
            conn.execute("UPDATE kb_shares SET permission=?, updated_at=? WHERE id=?", (permission, now, existing["id"]))
            share_id = existing["id"]
        else:
            share_id = db.new_id()
            conn.execute(
                "INSERT INTO kb_shares(id, knowledge_base_id, organization_id, shared_by_user_id, source_tenant_id, permission, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (share_id, kb_id, org["id"], ctx.user_id, ctx.tenant_id, permission, now, now),
            )
    return ok(_share_json(db.fetch_one("SELECT * FROM kb_shares WHERE id=?", (share_id,)), ctx))


@router.get("/knowledge-bases/{kb_id}/shares")
def list_kb_shares(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb_row(kb_id, ctx)
    rows = db.fetch_all("SELECT * FROM kb_shares WHERE knowledge_base_id=? AND deleted_at IS NULL", (kb_id,))
    return ok({"shares": [_share_json(row, ctx) for row in rows], "total": len(rows)})


@router.put("/knowledge-bases/{kb_id}/shares/{share_id}")
async def update_kb_share(kb_id: str, share_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb_row(kb_id, ctx)
    _owned_or_admin(ctx, kb.get("creator_id"))
    data = await body(request)
    with db.transaction() as conn:
        conn.execute("UPDATE kb_shares SET permission=?, updated_at=? WHERE id=? AND knowledge_base_id=?", (str(data.get("permission") or "viewer"), db.utcnow(), share_id, kb_id))
    return ok(message="Share updated")


@router.delete("/knowledge-bases/{kb_id}/shares/{share_id}")
def delete_kb_share(kb_id: str, share_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb_row(kb_id, ctx)
    _owned_or_admin(ctx, kb.get("creator_id"))
    with db.transaction() as conn:
        conn.execute("UPDATE kb_shares SET deleted_at=?, updated_at=? WHERE id=? AND knowledge_base_id=?", (db.utcnow(), db.utcnow(), share_id, kb_id))
    return ok(message="Share removed")


@router.get("/shared-knowledge-bases")
def list_shared_kbs(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT s.*, o.name AS org_name FROM kb_shares s JOIN organizations o ON o.id=s.organization_id "
        "JOIN organization_tenant_members m ON m.organization_id=s.organization_id "
        "WHERE m.tenant_id=? AND s.source_tenant_id<>? AND s.deleted_at IS NULL AND o.deleted_at IS NULL",
        (ctx.tenant_id, ctx.tenant_id),
    )
    items = []
    for row in rows:
        if ctx.api_key is not None and not ctx.api_key.allows_kb(str(row["knowledge_base_id"])):
            continue
        kb = db.fetch_one("SELECT * FROM knowledge_bases WHERE id=? AND deleted_at IS NULL", (row["knowledge_base_id"],))
        if not kb:
            continue
        items.append({
            "knowledge_base": {key: kb.get(key) for key in ("id", "name", "description", "type", "created_at", "updated_at")},
            "share_id": row["id"], "organization_id": row["organization_id"], "org_name": row["org_name"],
            "permission": row["permission"], "source_tenant_id": int(row["source_tenant_id"]), "shared_at": row.get("created_at"),
        })
    return ok(items)


@router.get("/organizations/{org_id}/shared-knowledge-bases")
def list_org_shared_kbs(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    org = _org(org_id)
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    rows = db.fetch_all("SELECT * FROM kb_shares WHERE organization_id=? AND deleted_at IS NULL", (org_id,))
    if ctx.api_key is not None:
        rows = [row for row in rows if ctx.api_key.allows_kb(str(row["knowledge_base_id"]))]
    return ok([{**_share_json(row, ctx), "is_mine": int(row["source_tenant_id"]) == ctx.tenant_id} for row in rows])


@router.get("/organizations/{org_id}/shares")
def list_org_shares(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    org = _org(org_id)
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    rows = db.fetch_all("SELECT * FROM kb_shares WHERE organization_id=? AND deleted_at IS NULL", (org_id,))
    if ctx.api_key is not None:
        rows = [row for row in rows if ctx.api_key.allows_kb(str(row["knowledge_base_id"]))]
    return ok({"shares": [_share_json(row, ctx) for row in rows], "total": len(rows)})


@router.post("/agents/{agent_id}/shares", status_code=201)
async def share_agent(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    agent = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
    if not agent:
        raise not_found("Agent not found")
    _owned_or_admin(ctx, agent.get("created_by"))
    data = await body(request)
    org = _org(str(data.get("organization_id") or ""))
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    permission = str(data.get("permission") or "viewer")
    existing = db.fetch_one("SELECT * FROM agent_shares WHERE agent_id=? AND organization_id=? AND deleted_at IS NULL", (agent_id, org["id"]))
    now = db.utcnow()
    with db.transaction() as conn:
        if existing:
            conn.execute("UPDATE agent_shares SET permission=?, updated_at=? WHERE id=?", (permission, now, existing["id"]))
            share_id = existing["id"]
        else:
            share_id = db.new_id()
            conn.execute(
                "INSERT INTO agent_shares(id, agent_id, organization_id, shared_by_user_id, source_tenant_id, permission, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (share_id, agent_id, org["id"], ctx.user_id, ctx.tenant_id, permission, now, now),
            )
    return ok(_agent_share_json(db.fetch_one("SELECT * FROM agent_shares WHERE id=?", (share_id,)), ctx))


@router.get("/agents/{agent_id}/shares")
def list_agent_shares(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    agent = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
    if not agent:
        raise not_found("Agent not found")
    _owned_or_admin(ctx, agent.get("created_by"))
    rows = db.fetch_all("SELECT * FROM agent_shares WHERE agent_id=? AND source_tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
    return ok({"shares": [_agent_share_json(row, ctx) for row in rows], "total": len(rows)})


@router.put("/agents/{agent_id}/shares/{share_id}")
async def update_agent_share(agent_id: str, share_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    agent = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
    if not agent:
        raise not_found("Agent not found")
    _owned_or_admin(ctx, agent.get("created_by"))
    data = await body(request)
    with db.transaction() as conn:
        conn.execute("UPDATE agent_shares SET permission=?, updated_at=? WHERE id=? AND agent_id=? AND source_tenant_id=?", (str(data.get("permission") or "viewer"), db.utcnow(), share_id, agent_id, ctx.tenant_id))
    return ok(message="Agent share updated")


@router.delete("/agents/{agent_id}/shares/{share_id}")
def delete_agent_share(agent_id: str, share_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    agent = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
    if not agent:
        raise not_found("Agent not found")
    _owned_or_admin(ctx, agent.get("created_by"))
    with db.transaction() as conn:
        conn.execute("UPDATE agent_shares SET deleted_at=?, updated_at=? WHERE id=? AND agent_id=? AND source_tenant_id=?", (db.utcnow(), db.utcnow(), share_id, agent_id, ctx.tenant_id))
    return ok(message="Agent share removed")


def _shared_agent_item(row: dict[str, Any], ctx: AuthContext) -> dict[str, Any] | None:
    agent = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (row["agent_id"], row["source_tenant_id"]))
    if not agent:
        return None
    org = db.fetch_one("SELECT name FROM organizations WHERE id=?", (row["organization_id"],))
    disabled = db.fetch_one(
        "SELECT 1 FROM tenant_disabled_shared_agents WHERE tenant_id=? AND agent_id=? AND source_tenant_id=?",
        (ctx.tenant_id, row["agent_id"], row["source_tenant_id"]),
    )
    return {
        "agent": {**agent, "config": loads(agent.get("config"), {})}, "share_id": row["id"],
        "organization_id": row["organization_id"], "org_name": org["name"] if org else "",
        "permission": row["permission"], "source_tenant_id": int(row["source_tenant_id"]),
        "shared_at": row.get("created_at"), "shared_by_user_id": row.get("shared_by_user_id"),
        "web_search_ready": False, "disabled_by_me": bool(disabled), "is_mine": int(row["source_tenant_id"]) == ctx.tenant_id,
    }


@router.get("/shared-agents")
def list_shared_agents(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT s.* FROM agent_shares s JOIN organization_tenant_members m ON m.organization_id=s.organization_id "
        "WHERE m.tenant_id=? AND s.source_tenant_id<>? AND s.deleted_at IS NULL",
        (ctx.tenant_id, ctx.tenant_id),
    )
    return ok([item for row in rows if (item := _shared_agent_item(row, ctx))])


@router.get("/organizations/{org_id}/shared-agents")
def list_org_shared_agents(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    org = _org(org_id)
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    rows = db.fetch_all("SELECT * FROM agent_shares WHERE organization_id=? AND deleted_at IS NULL", (org_id,))
    return ok([item for row in rows if (item := _shared_agent_item(row, ctx))])


@router.get("/organizations/{org_id}/agent-shares")
def list_org_agent_shares(org_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    org = _org(org_id)
    if not _org_role(org, ctx):
        raise forbidden("Not a member of this organization")
    rows = db.fetch_all("SELECT * FROM agent_shares WHERE organization_id=? AND deleted_at IS NULL", (org_id,))
    return ok({"shares": [_agent_share_json(row, ctx) for row in rows], "total": len(rows)})


@router.post("/shared-agents/disabled")
async def disable_shared_agent(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    agent_id = str(data.get("agent_id") or "")
    share = db.fetch_one("SELECT * FROM agent_shares WHERE agent_id=? AND deleted_at IS NULL LIMIT 1", (agent_id,))
    if not share:
        raise not_found("Shared agent not found")
    with db.transaction() as conn:
        if data.get("disabled", True):
            conn.execute(
                "INSERT OR REPLACE INTO tenant_disabled_shared_agents(tenant_id, agent_id, source_tenant_id, created_at) VALUES (?, ?, ?, ?)",
                (ctx.tenant_id, agent_id, share["source_tenant_id"], db.utcnow()),
            )
        else:
            conn.execute(
                "DELETE FROM tenant_disabled_shared_agents WHERE tenant_id=? AND agent_id=? AND source_tenant_id=?",
                (ctx.tenant_id, agent_id, share["source_tenant_id"]),
            )
    return ok(message="Preference updated")

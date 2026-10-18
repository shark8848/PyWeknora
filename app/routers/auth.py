"""Authentication, users, profiles, invitations and workspace switching."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .. import config, db
from ..deps import AuthContext, parse_auth, tenant_json, user_json
from ..errors import AppError, forbidden, unauthorized, validation
from ..security import hash_password, issue_token, verify_password
from ..utils import ok


router = APIRouter(tags=["auth"])

REGISTRATION_MODES = {"self_serve", "invite_register", "invite_only"}


BROWSER_SEARCH_DEFAULT = (
    "Default search engine: Bing.\n"
    "Search URL: https://www.bing.com/search?q={query}"
)


async def body(request: Request) -> dict[str, Any]:
    if not request.headers.get("content-type", "").startswith(("application/json", "application/x-www-form-urlencoded")):
        return {}
    try:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _memberships(user_id: str, active_tenant: int | None = None) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT tm.tenant_id, tm.role, t.name AS tenant_name
        FROM tenant_members tm JOIN tenants t ON t.id = tm.tenant_id
        WHERE tm.user_id=? AND tm.status='active' AND tm.deleted_at IS NULL
          AND t.deleted_at IS NULL
        ORDER BY tm.tenant_id
        """,
        (user_id,),
    )
    if active_tenant and not any(int(row["tenant_id"]) == active_tenant for row in rows):
        tenant = db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (active_tenant,))
        if tenant:
            rows.append({"tenant_id": active_tenant, "role": "admin", "tenant_name": tenant["name"]})
    return rows


def _login_payload(user: dict[str, Any], tenant_id: int | None = None, message: str = "Login successful") -> dict[str, Any]:
    tenant_id = int(tenant_id or user.get("tenant_id") or 0)
    tenant = db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (tenant_id,))
    access, _ = issue_token(str(user["id"]), tenant_id, "access")
    refresh, _ = issue_token(str(user["id"]), None, "refresh")
    now = db.utcnow()
    with db.transaction() as conn:
        for token, token_type in ((access, "access_token"), (refresh, "refresh_token")):
            conn.execute(
                """
                INSERT INTO auth_tokens
                (id, user_id, token, token_type, expires_at, is_revoked, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    db.new_id(),
                    user["id"],
                    token,
                    token_type,
                    (datetime.now(timezone.utc) + timedelta(seconds=config.ACCESS_TOKEN_TTL)).isoformat(),
                    now,
                    now,
                ),
            )
        conn.execute(
            "UPDATE users SET updated_at=? WHERE id=?", (now, user["id"])
        )
    role = next((m["role"] for m in _memberships(str(user["id"]), tenant_id) if int(m["tenant_id"]) == tenant_id), "owner")
    return {
        "success": True,
        "message": message,
        "user": user_json(user),
        "active_tenant": tenant_json(tenant, role) if tenant else None,
        "memberships": _memberships(str(user["id"]), tenant_id),
        "token": access,
        "refresh_token": refresh,
    }


def _validate_password(value: str) -> None:
    if not isinstance(value, str) or len(value) < 6 or len(value) > 128:
        raise validation("Password must contain 6 to 128 characters")
    if config.COMPLEX_PASSWORD and not re.search(r"[a-z]", value):
        raise validation("Password must include a lowercase letter")
    if config.COMPLEX_PASSWORD and not re.search(r"[A-Z]", value):
        raise validation("Password must include an uppercase letter")
    if config.COMPLEX_PASSWORD and not re.search(r"\d", value):
        raise validation("Password must include a digit")


def _registration_mode() -> str:
    row = db.fetch_one(
        "SELECT value FROM system_settings WHERE key='auth.registration_mode'"
    )
    mode = str((row or {}).get("value") or config.REGISTRATION_MODE).strip()
    return mode if mode in REGISTRATION_MODES else "self_serve"


def _create_personal_tenant(conn: Any, user_id: str, username: str) -> int:
    now = db.utcnow()
    cur = conn.execute(
        """
        INSERT INTO tenants
        (name, description, retriever_engines, status, business, storage_quota,
         storage_used, created_at, updated_at)
        VALUES (?, ?, ?, 'active', 'personal', ?, 0, ?, ?)
        """,
        (f"{username} 的空间", "个人知识空间", json.dumps({"engines": []}), 10 * 1024**3, now, now),
    )
    tenant_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO tenant_members
        (user_id, tenant_id, role, status, joined_at, created_at, updated_at)
        VALUES (?, ?, 'owner', 'active', ?, ?, ?)
        """,
        (user_id, tenant_id, now, now, now),
    )
    return tenant_id


@router.get("/auth/config")
def auth_config() -> dict[str, Any]:
    return {
        "success": True,
        "registration_mode": _registration_mode(),
        "complex_password_enabled": config.COMPLEX_PASSWORD,
    }


@router.post("/auth/register", status_code=201)
async def register(request: Request) -> dict[str, Any]:
    if _registration_mode() != "self_serve":
        raise forbidden("Public registration is disabled")
    data = await body(request)
    username = str(data.get("username") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    _validate_password(password)
    if not username or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise validation("Username and a valid email are required")
    if db.fetch_one("SELECT id FROM users WHERE email=? AND deleted_at IS NULL", (email,)):
        raise validation("Email is already registered")
    if db.fetch_one("SELECT id FROM users WHERE username=? AND deleted_at IS NULL", (username,)):
        raise validation("Username is already taken")
    user_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        tenant_id = _create_personal_tenant(conn, user_id, username)
        conn.execute(
            """
            INSERT INTO users
            (id, username, email, password_hash, avatar, tenant_id, is_active,
             can_access_all_tenants, preferences, created_at, updated_at, is_system_admin)
            VALUES (?, ?, ?, ?, '', ?, 1, 0, '{}', ?, ?, 0)
            """,
            (user_id, username, email, hash_password(password), tenant_id, now, now),
        )
    user = db.fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
    tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
    return {
        "success": True,
        "message": "Registration successful",
        "user": user_json(user),
        "tenant": tenant_json(tenant, "owner"),
    }


@router.post("/auth/login")
def login(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = payload or {}
    email = str(data.get("email") or "").strip().lower()
    password = str(data.get("password") or "")
    if not email or not password:
        raise validation("Email and password are required")
    user = db.fetch_one(
        "SELECT * FROM users WHERE lower(email)=? AND deleted_at IS NULL", (email,)
    )
    if not user or not verify_password(password, user["password_hash"]):
        raise unauthorized("Invalid email or password")
    if not user.get("is_active"):
        raise forbidden("Account is disabled")
    home = int(user.get("tenant_id") or 0)
    active = home
    prefs = user.get("preferences") or "{}"
    try:
        preference_data = json.loads(prefs) if isinstance(prefs, str) else prefs
        remembered = int(preference_data.get("last_active_tenant_id") or 0)
        if remembered and any(int(m["tenant_id"]) == remembered for m in _memberships(str(user["id"]))):
            active = remembered
    except (ValueError, TypeError, AttributeError):
        pass
    return _login_payload(user, active)


@router.post("/auth/logout")
def logout(request: Request) -> dict[str, Any]:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if token:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE auth_tokens SET is_revoked=1, updated_at=? WHERE token=?",
                (db.utcnow(), token),
            )
    return ok(message="Logged out")


@router.post("/auth/refresh")
async def refresh(request: Request) -> dict[str, Any]:
    data = await body(request)
    token = str(data.get("refreshToken") or data.get("refresh_token") or "")
    if not token:
        raise validation("refreshToken is required")
    from ..security import decode_token

    try:
        payload = decode_token(token, "refresh")
    except ValueError as exc:
        raise unauthorized(str(exc)) from exc
    record = db.fetch_one(
        "SELECT id FROM auth_tokens WHERE token=? AND token_type='refresh_token' AND is_revoked=0",
        (token,),
    )
    if not record:
        raise unauthorized("Refresh token has been revoked")
    user = db.fetch_one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (payload["sub"],))
    if not user or not user.get("is_active"):
        raise unauthorized("User not found or inactive")
    tenant_id = int(payload.get("tenant_id") or user.get("tenant_id") or 0)
    access, _ = issue_token(str(user["id"]), tenant_id, "access")
    refresh_token, _ = issue_token(str(user["id"]), None, "refresh")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE auth_tokens SET is_revoked=1, updated_at=? WHERE id=?", (now, record["id"]))
        for new_token, kind in ((access, "access_token"), (refresh_token, "refresh_token")):
            conn.execute(
                """
                INSERT INTO auth_tokens
                (id, user_id, token, token_type, expires_at, is_revoked, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (db.new_id(), user["id"], new_token, kind, now, now, now),
            )
    return {
        "success": True,
        "access_token": access,
        "refresh_token": refresh_token,
        "token": access,
        "refreshToken": refresh_token,
    }


@router.get("/auth/me")
def current_user(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(
        {
            "user": user_json(ctx.user),
            "preference_defaults": {
                "browser_search_instructions": BROWSER_SEARCH_DEFAULT,
            },
            "tenant": tenant_json(ctx.tenant, ctx.role),
            "memberships": _memberships(ctx.user_id, ctx.tenant_id),
            "tenant_required": False,
            "capabilities": {
                "can_create_tenant": True,
                "auto_accept_invitation": False,
            },
        }
    )


@router.put("/auth/me/preferences")
async def update_preferences(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    current = user_json(ctx.user).get("preferences") or {}
    for key in ("browser_search_instructions", "last_active_tenant_id", "oidc_only_login", "gallery"):
        if key in data:
            if key == "last_active_tenant_id" and int(data[key] or 0) == 0:
                current.pop(key, None)
            else:
                current[key] = data[key]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE users SET preferences=?, updated_at=? WHERE id=?",
            (json.dumps(current, ensure_ascii=False), db.utcnow(), ctx.user_id),
        )
    return ok(current)


@router.post("/auth/change-password")
async def change_password(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    old = str(data.get("old_password") or "")
    new = str(data.get("new_password") or "")
    if not verify_password(old, ctx.user["password_hash"]):
        raise validation("Current password is incorrect", "invalid_old_password")
    if old == new:
        raise validation("New password must differ from the current password", "same_password")
    _validate_password(new)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (hash_password(new), db.utcnow(), ctx.user_id),
        )
        conn.execute("UPDATE auth_tokens SET is_revoked=1 WHERE user_id=?", (ctx.user_id,))
    return ok(message="Password changed")


@router.post("/auth/switch-tenant")
async def switch_tenant(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    target = int(data.get("tenant_id") or 0)
    if not target:
        raise validation("tenant_id is required")
    if not any(int(m["tenant_id"]) == target for m in _memberships(ctx.user_id)) and not (
        config.ENABLE_CROSS_TENANT_ACCESS and ctx.user.get("can_access_all_tenants")
    ):
        raise forbidden("You are not a member of this workspace")
    prefs = user_json(ctx.user).get("preferences") or {}
    prefs["last_active_tenant_id"] = target
    with db.transaction() as conn:
        conn.execute(
            "UPDATE users SET preferences=?, updated_at=? WHERE id=?",
            (json.dumps(prefs, ensure_ascii=False), db.utcnow(), ctx.user_id),
        )
    return _login_payload(ctx.user, target, "Workspace switched")


@router.post("/auth/auto-setup")
def auto_setup() -> dict[str, Any]:
    if not config.LITE_MODE:
        raise forbidden("auto-setup is only available in lite edition")
    user = db.fetch_one("SELECT * FROM users WHERE email=? AND deleted_at IS NULL", (config.DEFAULT_ADMIN_EMAIL,))
    if not user:
        raise unauthorized("Default user is unavailable")
    return _login_payload(user, int(user["tenant_id"]), "Auto-setup successful")


@router.get("/auth/validate")
def validate(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return {"success": True, "message": "Token is valid", "user": user_json(ctx.user)}


@router.get("/auth/tenant")
def auth_tenant(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(tenant_json(ctx.tenant, ctx.role))


@router.get("/auth/oidc/config")
def oidc_config() -> dict[str, Any]:
    return {"success": True, "enabled": False}


@router.get("/auth/oidc/url")
def oidc_url() -> dict[str, Any]:
    raise forbidden("OIDC is not configured")


@router.get("/auth/oidc/start")
def oidc_start() -> RedirectResponse:
    return RedirectResponse("/login?error=oidc_unavailable", status_code=302)


@router.get("/auth/oidc/callback")
def oidc_callback() -> RedirectResponse:
    return RedirectResponse("/login#oidc_error=oidc_unavailable", status_code=302)


@router.post("/auth/invitations/lookup")
async def invitation_lookup(request: Request) -> dict[str, Any]:
    data = await body(request)
    token = str(data.get("token") or "")
    row = db.fetch_one(
        """
        SELECT i.*, t.name AS tenant_name FROM tenant_invitations i
        JOIN tenants t ON t.id=i.tenant_id
        WHERE i.token=? AND i.deleted_at IS NULL
        """,
        (token,),
    )
    if not row or row.get("status") not in {"pending", "accepted"}:
        raise AppError("Invitation is invalid or expired", status=410, code=1003)
    return ok(
        {
            "tenant_id": int(row["tenant_id"]),
            "tenant_name": row["tenant_name"],
            "role": row["role"],
            "expires_at": row["expires_at"],
        }
    )


@router.post("/auth/register-by-invite", status_code=201)
async def register_by_invite(request: Request) -> dict[str, Any]:
    if _registration_mode() not in {"self_serve", "invite_register"}:
        raise forbidden("Registration is disabled")
    data = await body(request)
    token = str(data.get("token") or "")
    email = str(data.get("email") or "").strip().lower()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    invite = db.fetch_one(
        "SELECT * FROM tenant_invitations WHERE token=? AND deleted_at IS NULL", (token,)
    )
    if not invite:
        raise AppError("Invitation is invalid or expired", status=410, code=1003)
    _validate_password(password)
    if not username or not email:
        raise validation("Username and email are required")
    existing = db.fetch_one("SELECT * FROM users WHERE lower(email)=? AND deleted_at IS NULL", (email,))
    now = db.utcnow()
    if existing:
        user_id = existing["id"]
    else:
        user_id = db.new_id()
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO users
                (id, username, email, password_hash, avatar, tenant_id, is_active,
                 can_access_all_tenants, preferences, created_at, updated_at, is_system_admin)
                VALUES (?, ?, ?, ?, '', ?, 1, 0, '{}', ?, ?, 0)
                """,
                (user_id, username, email, hash_password(password), invite["tenant_id"], now, now),
            )
    member = db.fetch_one(
        "SELECT id FROM tenant_members WHERE user_id=? AND tenant_id=? AND deleted_at IS NULL",
        (user_id, invite["tenant_id"]),
    )
    with db.transaction() as conn:
        if member:
            conn.execute(
                "UPDATE tenant_members SET role=?, status='active', updated_at=? WHERE id=?",
                (invite["role"], now, member["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO tenant_members
                (user_id, tenant_id, role, status, joined_at, created_at, updated_at)
                VALUES (?, ?, ?, 'active', ?, ?, ?)
                """,
                (user_id, invite["tenant_id"], invite["role"], now, now, now),
            )
        conn.execute(
            "UPDATE tenant_invitations SET status='accepted', accepted_count=accepted_count+1, updated_at=? WHERE id=?",
            (now, invite["id"]),
        )
    user = db.fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
    return _login_payload(user, int(invite["tenant_id"]), "Registration successful")

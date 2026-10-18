"""System information, capabilities, admin users, settings and audit APIs."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, db
from ..api_keys import hash_token, normalize_capabilities
from ..deps import parse_auth, require_role, user_json
from ..errors import conflict, forbidden, not_found, validation
from ..security import hash_password
from ..services import model_catalog
from ..services import model_limiter
from ..services import url_guard
from ..utils import dumps, loads, ok, page, pagination, truthy


router = APIRouter(tags=["system"])
STARTED_AT = datetime.now(timezone.utc)


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _system_admin(request: Request) -> Any:
    ctx = parse_auth(request)
    assert ctx is not None
    if ctx.api_key is not None and ctx.api_key.is_platform:
        return ctx
    if not ctx.user.get("is_system_admin"):
        raise forbidden("System administrator access required")
    return ctx


def _audit(ctx: Any, action: str, target_type: str = "", target_id: str = "", details: Any = None, outcome: str = "success") -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs(tenant_id, actor_user_id, actor_role, action,
              target_type, target_id, target_user_id, request_path, request_method,
              outcome, details, scope_type, scope_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, '', '', '', ?, ?, 'tenant', ?, ?)
            """,
            (ctx.tenant_id, ctx.user_id, ctx.role, action, target_type, target_id, outcome, dumps(details or {}), str(ctx.tenant_id), db.utcnow()),
        )


@router.get("/system/info")
def system_info(request: Request) -> dict[str, Any]:
    parse_auth(request)
    tables = db.fetch_one("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'")["n"]
    return {
        "code": 0,
        "msg": "success",
        "data": {
            "version": config.APP_VERSION,
            "edition": "python",
            "commit_id": "pyweknora",
            "build_time": STARTED_AT.isoformat(),
            "go_version": "",
            "keyword_index_engine": "sqlite-fts",
            "vector_store_engine": "sqlite-local-embedding",
            "graph_database_engine": "sqlite",
            "minio_enabled": False,
            "db_version": f"sqlite:{tables} tables",
            "started_at": STARTED_AT.isoformat(),
            "uptime_seconds": int((datetime.now(timezone.utc) - STARTED_AT).total_seconds()),
        },
    }


@router.get("/system/capabilities")
def capabilities(request: Request) -> dict[str, Any]:
    parse_auth(request)
    supported = {"supported": True}
    docker_enabled = os.getenv("WEKNORA_SANDBOX_DOCKER_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    docker = {"supported": docker_enabled}
    if not docker_enabled:
        docker["reason"] = "docker_backend_disabled"
    return {
        "code": 0,
        "msg": "success",
        "data": {
            "edition": "python",
            "capabilities": {
                "organizations": supported,
                "agents": supported,
                "integrations.im": supported,
                "integrations.embed": supported,
                "integrations.api": supported,
                "integrations.mcpserver": supported,
                "settings.mcp": supported,
                "settings.websearch": supported,
                "settings.vectorstore": supported,
                "settings.storage": supported,
                "settings.sandbox": supported,
                "settings.sandbox.docker": docker,
                "settings.sandbox.host": {"supported": False, "reason": "platform_unsupported"},
                "settings.sandbox.remote": supported,
            },
        },
    }


@router.get("/system/parser-engines")
def parser_engines(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    config_data = loads(ctx.tenant.get("parser_engine_config"), {})
    return _parser_engine_response(config_data if isinstance(config_data, dict) else {})


@router.post("/system/parser-engines/check")
async def parser_engines_check(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    stored = loads(ctx.tenant.get("parser_engine_config"), {})
    overrides = await body(request)
    merged = dict(stored) if isinstance(stored, dict) else {}
    merged.update(overrides)
    return _parser_engine_response(merged)


@router.post("/system/docreader/reconnect")
async def docreader_reconnect(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    addr = str(data.get("addr") or "").strip()
    parsed = urllib.parse.urlparse(addr)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation("addr must be an absolute http(s) URL")
    connected, message = _probe_docreader(addr)
    stored = loads(ctx.tenant.get("parser_engine_config"), {})
    config_data = dict(stored) if isinstance(stored, dict) else {}
    config_data["docreader_addr"] = addr
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE tenants SET parser_engine_config=?, updated_at=? WHERE id=?", (dumps(config_data), now, ctx.tenant_id))
    response = _parser_engine_response(config_data, connected=connected)
    response["msg"] = "连接成功" if connected else message
    return response


@router.get("/system/host-project-dir")
@router.post("/system/host-project-dir")
def host_project_dir(request: Request) -> dict[str, Any]:
    parse_auth(request)
    configured = str(os.getenv("WEKNORA_HOST_PROJECT_DIR") or "").strip()
    if not configured:
        raise not_found("host project picker is not available")
    path = Path(configured).expanduser().resolve()
    if not path.is_dir():
        raise not_found("configured host project directory does not exist")
    return {"code": 0, "msg": "success", "data": {"dir": str(path)}}


def _parser_engine_response(config_data: dict[str, Any], *, connected: bool | None = None) -> dict[str, Any]:
    engines = [
        {
            "Name": "builtin",
            "Description": "Python text, Markdown, JSON, CSV and HTML parser",
            "FileTypes": ["txt", "md", "csv", "json", "html"],
            "Available": True,
            "UnavailableReason": "",
        }
    ]
    dependencies = [
        ("docx", "python-docx", ["docx"], "Word document parser"),
        ("xlsx", "openpyxl", ["xlsx", "xlsm"], "Excel workbook parser"),
        ("pptx", "python-pptx", ["pptx"], "PowerPoint parser"),
        ("pdf", "pypdf or PyMuPDF", ["pdf"], "PDF parser"),
    ]
    for name, package, file_types, description in dependencies:
        module = "docx" if name == "docx" else "openpyxl" if name == "xlsx" else "pptx" if name == "pptx" else "pypdf"
        available = importlib.util.find_spec(module) is not None
        if name == "pdf" and not available:
            available = importlib.util.find_spec("fitz") is not None
        engine: dict[str, Any] = {
            "Name": name,
            "Description": description,
            "FileTypes": file_types,
            "Available": available,
            "UnavailableReason": "",
        }
        if not available:
            engine["UnavailableReason"] = f"Optional dependency {package} is not installed"
        engines.append(engine)
    remote_addr = str(config_data.get("docreader_addr") or "").strip()
    if connected is None:
        connected, _ = _probe_docreader(remote_addr) if remote_addr else (True, "")
    if remote_addr:
        transport = "https" if remote_addr.lower().startswith("https://") else "http"
        remote_engines = _fetch_remote_parser_engines(remote_addr)
        if remote_engines:
            known = {str(item.get("Name") or "").lower() for item in engines}
            engines.extend(item for item in remote_engines if str(item.get("Name") or "").lower() not in known)
    else:
        transport = "python"
    return {
        "code": 0,
        "msg": "success",
        "data": engines,
        "docreader_addr": remote_addr,
        "docreader_transport": transport,
        "connected": connected,
    }


def _probe_docreader(addr: str) -> tuple[bool, str]:
    try:
        parsed = urllib.parse.urlparse(addr)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False, "docreader address is invalid"
        url = addr.rstrip("/") + "/health"
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=8) as response:
            return 200 <= response.status < 300, ""
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return False, str(exc)


def _fetch_remote_parser_engines(addr: str) -> list[dict[str, Any]]:
    try:
        request = urllib.request.Request(addr.rstrip("/") + "/engines", headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return []
    values = payload.get("data") if isinstance(payload, dict) else payload
    return [item for item in values or [] if isinstance(item, dict) and item.get("Name")]


@router.get("/system/admin/list")
def list_system_admins(request: Request) -> dict[str, Any]:
    _system_admin(request)
    rows = db.fetch_all("SELECT * FROM users WHERE is_system_admin=1 AND deleted_at IS NULL ORDER BY created_at", ())
    admins = [user_json(row) for row in rows]
    return {"total": len(admins), "admins": admins}


@router.post("/system/admin/promote")
async def promote_admin(request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    data = await body(request)
    user_id = str(data.get("user_id") or "")
    email = str(data.get("email") or "").strip().lower()
    user = db.fetch_one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)) if user_id else db.fetch_one("SELECT * FROM users WHERE lower(email)=? AND deleted_at IS NULL", (email,))
    if not user:
        raise not_found("User not found")
    with db.transaction() as conn:
        conn.execute("UPDATE users SET is_system_admin=1, updated_at=? WHERE id=?", (db.utcnow(), user["id"]))
    _audit(ctx, "system_admin.promote", "user", user["id"])
    return user_json(db.fetch_one("SELECT * FROM users WHERE id=?", (user["id"],)))


@router.post("/system/admin/revoke")
async def revoke_admin(request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    data = await body(request)
    user_id = str(data.get("user_id") or "")
    if user_id == ctx.user_id:
        raise validation("You cannot revoke your own system administrator role")
    user = db.fetch_one("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (user_id,))
    if not user:
        raise not_found("User not found")
    with db.transaction() as conn:
        conn.execute("UPDATE users SET is_system_admin=0, updated_at=? WHERE id=?", (db.utcnow(), user_id))
    _audit(ctx, "system_admin.revoke", "user", user_id)
    return user_json(db.fetch_one("SELECT * FROM users WHERE id=?", (user_id,)))


@router.get("/system/admin/users")
def list_all_users(request: Request) -> dict[str, Any]:
    _system_admin(request)
    try:
        offset = max(0, int(request.query_params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(request.query_params.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    if limit <= 0:
        limit = 50
    limit = min(200, limit)
    rows = db.fetch_all("SELECT * FROM users WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit + 1, offset))
    has_more = len(rows) > limit
    return {"users": [user_json(row) for row in rows[:limit]], "offset": offset, "limit": limit, "has_more": has_more}


@router.post("/system/admin/users/status")
async def set_user_status(request: Request) -> Any:
    ctx = _system_admin(request)
    data = await body(request)
    raw_user_id = data.get("user_id")
    active = data.get("is_active")
    if not isinstance(raw_user_id, str) or not raw_user_id or type(active) is not bool:
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    user_id = raw_user_id.strip()
    with db.transaction() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (user_id,)
        ).fetchone()
        if user is None:
            return JSONResponse({"error": "User not found"}, status_code=404)
        if bool(user["is_active"]) == active:
            return {"message": "User status updated", "user": user_json(dict(user)), "is_active": active}
        if not active:
            if user_id == ctx.user_id:
                return JSONResponse({"error": "cannot disable your own account"}, status_code=400)
            if user["is_system_admin"]:
                active_admins = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE is_system_admin=1 AND is_active=1 AND deleted_at IS NULL"
                ).fetchone()[0]
                if active_admins <= 1:
                    return JSONResponse(
                        {"error": "cannot disable the last system administrator"}, status_code=400
                    )
        conn.execute("UPDATE users SET is_active=?, updated_at=? WHERE id=?", (1 if active else 0, db.utcnow(), user_id))
        if not active:
            conn.execute("UPDATE auth_tokens SET is_revoked=1 WHERE user_id=?", (user_id,))
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    _audit(ctx, "user.status", "user", user_id, {"is_active": active})
    return {"message": "User status updated", "user": user_json(dict(user)), "is_active": active}


@router.post("/system/admin/users/reset-password")
async def reset_password(request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    data = await body(request)
    email = str(data.get("email") or "").strip().lower()
    new_password = str(data.get("new_password") or "")
    if len(new_password) < 6:
        raise validation("Password must contain at least 6 characters")
    user = db.fetch_one("SELECT * FROM users WHERE lower(email)=? AND deleted_at IS NULL", (email,))
    if not user:
        raise not_found("User not found")
    if user["id"] == ctx.user_id:
        raise validation("Use change-password to reset your own password")
    with db.transaction() as conn:
        conn.execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (hash_password(new_password), db.utcnow(), user["id"]))
        conn.execute("UPDATE auth_tokens SET is_revoked=1 WHERE user_id=?", (user["id"],))
    _audit(ctx, "user.password_reset", "user", user["id"])
    return {"success": True, "message": "Password reset"}


@router.post("/system/admin/users/create", status_code=201)
async def create_system_user(request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    data = await body(request)
    username = str(data.get("username") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    supplied = data.get("password")
    password = str(supplied) if supplied is not None else secrets.token_urlsafe(12)
    if not username or not email or len(password) < 6:
        raise validation("Username, email and a password of at least 6 characters are required")
    existing = db.fetch_one("SELECT * FROM users WHERE lower(email)=? AND deleted_at IS NULL", (email,))
    if existing:
        return {"success": True, "user": user_json(existing), "created": False}
    user_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO tenants(name, description, retriever_engines, status, business, storage_quota, storage_used, created_at, updated_at) VALUES (?, '', '{\"engines\":[]}', 'active', 'personal', ?, 0, ?, ?)",
            (f"{username} 的空间", 10 * 1024**3, now, now),
        )
        tenant_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO users(id, username, email, password_hash, avatar, tenant_id, is_active, can_access_all_tenants, preferences, created_at, updated_at, is_system_admin) VALUES (?, ?, ?, ?, '', ?, 1, 0, '{}', ?, ?, 0)",
            (user_id, username, email, hash_password(password), tenant_id, now, now),
        )
        conn.execute(
            "INSERT INTO tenant_members(user_id, tenant_id, role, status, joined_at, created_at, updated_at) VALUES (?, ?, 'owner', 'active', ?, ?, ?)",
            (user_id, tenant_id, now, now, now),
        )
    user = db.fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
    result = {"success": True, "user": user_json(user), "created": True}
    if supplied is None:
        result["generated_password"] = password
    _audit(ctx, "user.create", "user", user_id)
    return result


def _setting_json(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["is_secret"] = bool(result.get("is_secret"))
    result["requires_restart"] = bool(result.get("requires_restart"))
    if result.get("value_type") == "string_list":
        try:
            parsed = json.loads(str(result.get("value") or ""))
            if isinstance(parsed, list):
                result["value"] = parsed
        except ValueError:
            pass
    if result["is_secret"] and result.get("value"):
        result["value"] = "********"
    return result


def _setting_value(key: str, raw: Any) -> tuple[str, str, str, str]:
    if key == "auth.registration_mode":
        value = str(raw).strip()
        if value not in {"self_serve", "invite_register", "invite_only"}:
            raise validation(
                "auth.registration_mode must be self_serve, invite_register or invite_only"
            )
        return (
            value,
            "string",
            "auth",
            "注册模式。self_serve = 开放注册；invite_register = 仅持有效邀请链接的新用户可注册；"
            "invite_only = 禁止注册，邀请仅供已有账号接受。修改后立即生效。",
        )
    if key != "ssrf.whitelist":
        return str(raw), "string", "custom", ""
    if isinstance(raw, list):
        entries = [str(item) for item in raw]
    elif isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise validation("ssrf.whitelist must be a string list") from exc
            if not isinstance(parsed, list):
                raise validation("ssrf.whitelist must be a string list")
            entries = [str(item) for item in parsed]
        else:
            entries = [item.strip() for item in text.split(",") if item.strip()]
    else:
        raise validation("ssrf.whitelist must be a string list")
    try:
        entries = url_guard.validate_whitelist_entries(entries)
    except url_guard.URLGuardError as exc:
        raise validation(str(exc)) from exc
    return (
        json.dumps(entries, ensure_ascii=False),
        "string_list",
        "security",
        "SSRF 防护白名单。可填入 example.com / *.foo.com / 10.0.0.0/8 / 2001:db8::1。",
    )


@router.get("/system/admin/settings")
def list_settings(request: Request) -> list[dict[str, Any]]:
    _system_admin(request)
    rows = db.fetch_all("SELECT * FROM system_settings ORDER BY category, key")
    return [_setting_json(row) for row in rows]


@router.get("/system/admin/settings/{key}")
def get_setting(key: str, request: Request) -> dict[str, Any]:
    _system_admin(request)
    row = db.fetch_one("SELECT * FROM system_settings WHERE key=?", (key,))
    if not row:
        raise not_found("Setting not found")
    return _setting_json(row)


@router.put("/system/admin/settings/{key}")
async def update_setting(key: str, request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    data = await body(request)
    value, value_type, category, description = _setting_value(
        key, data.get("value") if "value" in data else data
    )
    with db.transaction() as conn:
        existing = conn.execute("SELECT id FROM system_settings WHERE key=?", (key,)).fetchone()
        if existing:
            if key == "ssrf.whitelist":
                conn.execute(
                    "UPDATE system_settings SET value=?, value_type=?, category=?, description=?, last_modified_by=?, updated_at=? WHERE key=?",
                    (value, value_type, category, description, ctx.user_id, db.utcnow(), key),
                )
            else:
                conn.execute(
                    "UPDATE system_settings SET value=?, last_modified_by=?, updated_at=? WHERE key=?",
                    (value, ctx.user_id, db.utcnow(), key),
                )
        else:
            conn.execute(
                "INSERT INTO system_settings(key, value, value_type, category, description, is_secret, requires_restart, last_modified_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?, ?)",
                (key, value, value_type, category, description, ctx.user_id, db.utcnow(), db.utcnow()),
            )
    return _setting_json(db.fetch_one("SELECT * FROM system_settings WHERE key=?", (key,)))


@router.delete("/system/admin/settings/{key}")
def reset_setting(key: str, request: Request) -> dict[str, Any]:
    _system_admin(request)
    with db.transaction() as conn:
        conn.execute("DELETE FROM system_settings WHERE key=?", (key,))
    return {"success": True, "message": "Setting reset"}


@router.post("/system/admin/tenants/apply-default-storage-quota")
def apply_storage_quota(request: Request) -> dict[str, Any]:
    _system_admin(request)
    row = db.fetch_one("SELECT value FROM system_settings WHERE key='tenant.default_storage_quota'")
    quota = int(row["value"]) if row and str(row["value"]).isdigit() else 10 * 1024**3
    with db.transaction() as conn:
        cur = conn.execute("UPDATE tenants SET storage_quota=?, updated_at=? WHERE deleted_at IS NULL", (quota, db.utcnow()))
    return {"success": True, "updated": cur.rowcount, "storage_quota": quota}


@router.get("/system/admin/audit-log")
def system_audit(request: Request) -> dict[str, Any]:
    _system_admin(request)
    try:
        after_id = max(0, int(request.query_params.get("after_id") or 0))
    except ValueError:
        after_id = 0
    try:
        limit = min(100, max(1, int(request.query_params.get("limit") or 50)))
    except ValueError:
        limit = 50
    clauses = ["id<?"]
    params: list[Any] = [after_id or 2**63 - 1]
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


@router.get("/system/admin/api-keys")
def platform_api_keys(request: Request) -> dict[str, Any]:
    _system_admin(request)
    rows = db.fetch_all("SELECT * FROM tenant_api_keys WHERE scope_type='platform' AND revoked_at IS NULL", ())
    for row in rows:
        row["knowledge_base_ids"] = loads(row.get("knowledge_base_ids"), [])
        row["capabilities"] = loads(row.get("capabilities"), [])
        row["api_key"] = row.get("api_key") or "••••"
        row.pop("key_hash", None)
    return {"success": True, "data": rows}


@router.post("/system/admin/api-keys", status_code=201)
async def create_platform_api_key(request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    data = await body(request)
    token = "wks_" + secrets.token_urlsafe(32)
    capabilities = normalize_capabilities(data.get("capabilities"))
    if not capabilities:
        raise validation("Platform API keys require at least one capability")

    now = db.utcnow()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO tenant_api_keys(tenant_id, scope_type, name, key_hash, api_key, full_access, knowledge_base_ids, capabilities, created_at, updated_at) VALUES (NULL, 'platform', ?, ?, ?, 0, '[]', ?, ?, ?)",
            (str(data.get("name") or "Platform key"), hash_token(token), token, dumps(capabilities), now, now),
        )
        key_id = int(cur.lastrowid)
    row = db.fetch_one("SELECT * FROM tenant_api_keys WHERE id=?", (key_id,))
    row["knowledge_base_ids"] = loads(row.get("knowledge_base_ids"), [])
    row["capabilities"] = loads(row.get("capabilities"), [])
    row["token"] = token
    row.pop("key_hash", None)
    return {"success": True, "data": row}


@router.delete("/system/admin/api-keys/{key_id}")
def delete_platform_api_key(key_id: int, request: Request) -> dict[str, Any]:
    _system_admin(request)
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_api_keys SET revoked_at=?, updated_at=? WHERE id=?", (db.utcnow(), db.utcnow(), key_id))
    return {"success": True}


@router.get("/system/admin/model-catalog")
def model_catalog_admin(request: Request) -> dict[str, Any]:
    _system_admin(request)
    return model_catalog.state()


@router.post("/system/admin/model-catalog/preview")
async def preview_model_catalog(request: Request) -> dict[str, Any]:
    _system_admin(request)
    return model_catalog.preview(await body(request))


@router.put("/system/admin/model-catalog")
async def publish_model_catalog(request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    return model_catalog.publish(await body(request), ctx.user_id)


RUNTIME_QUEUE = "database"
RERUNNABLE_TASK_KINDS = {
    "faq_import", "kb_clone", "knowledge_move", "knowledge_reparse",
    "summary_generation",
}


def _runtime_pool_limit(key: str, env_name: str, fallback: int) -> int:
    row = db.fetch_one("SELECT value FROM system_settings WHERE key=?", (key,))
    raw = row.get("value") if row else os.getenv(env_name, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value >= 1 else fallback


def _runtime_retry_pending(value: Any, now: float | None = None) -> bool:
    try:
        due = float(str(value or ""))
    except ValueError:
        return False
    return due > (time.time() if now is None else now)


def _runtime_task_state(status: str, retry_after: Any = "") -> str:
    if status == "pending" and _runtime_retry_pending(retry_after):
        return "retry"
    if status == "processing":
        return "active"
    if status in {"failed", "cancelled"}:
        return "archived"
    if status == "completed":
        return "completed"
    return "pending"


def _runtime_task(row: dict[str, Any]) -> dict[str, Any]:
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    state = _runtime_task_state(
        str(row.get("status") or "pending"), row.get("retry_after")
    )
    max_retry = int(row.get("max_retry") or 0)
    if max_retry <= 0 and str(row.get("kind") or "") == "faq_import":
        max_retry = 3 if payload.get("dry_run") else 5
    actions = ["cancel"] if state in {"pending", "active", "retry", "scheduled"} else ["delete"]
    if state in {"retry", "scheduled"} and str(row.get("kind") or "") in RERUNNABLE_TASK_KINDS:
        actions.append("run_now")
    if state == "archived" and str(row.get("kind") or "") in RERUNNABLE_TASK_KINDS:
        actions.insert(0, "run_now")
    return {
        "id": str(row["id"]),
        "task_id": str(row["id"]),
        "queue": RUNTIME_QUEUE,
        "type": str(row.get("kind") or ""),
        "state": state,
        "tenant_id": int(row.get("tenant_id") or 0),
        "knowledge_base_id": str(payload.get("kb_id") or payload.get("knowledge_base_id") or ""),
        "knowledge_id": str(payload.get("knowledge_id") or ""),
        "enqueued_at": str(row.get("created_at") or ""),
        "started_at": str(row.get("updated_at") or "") if state == "active" else "",
        "completed_at": str(row.get("updated_at") or "") if state == "completed" else "",
        "last_error": str(row.get("error") or ""),
        "last_failed_at": str(row.get("updated_at") or "") if state in {"archived", "retry"} else "",
        "next_process_at": (
            datetime.fromtimestamp(float(str(row.get("retry_after"))), tz=timezone.utc).isoformat()
            if state == "retry"
            else ""
        ),
        "retried": int(row.get("retry_count") or 0),
        "max_retry": max_retry,
        "is_orphaned": False,
        "allowed_actions": actions,
    }


@router.get("/system/admin/runtime/queues")
def runtime_queues(request: Request) -> dict[str, Any]:
    _system_admin(request)
    counts = {str(row["status"]): int(row["n"]) for row in db.fetch_all(
        "SELECT status, COUNT(*) AS n FROM py_task_results GROUP BY status", ()
    )}
    pending_rows = db.fetch_all(
        "SELECT retry_after FROM py_task_results WHERE status='pending'", ()
    )
    retry = sum(1 for row in pending_rows if _runtime_retry_pending(row.get("retry_after")))
    pending = len(pending_rows) - retry
    active = counts.get("processing", 0)
    archived = counts.get("failed", 0) + counts.get("cancelled", 0)
    completed = counts.get("completed", 0)
    core_concurrency = _runtime_pool_limit("asynq.core_concurrency", "WEKNORA_ASYNQ_CORE_CONCURRENCY", 8)
    postprocess_concurrency = _runtime_pool_limit("asynq.postprocess_concurrency", "WEKNORA_ASYNQ_POSTPROCESS_CONCURRENCY", 2)
    enrichment_concurrency = _runtime_pool_limit("asynq.enrichment_concurrency", "WEKNORA_ASYNQ_ENRICHMENT_CONCURRENCY", 12)
    maintenance_concurrency = _runtime_pool_limit("asynq.maintenance_concurrency", "WEKNORA_ASYNQ_MAINTENANCE_CONCURRENCY", 4)
    shared_concurrency = _runtime_pool_limit("asynq.shared_concurrency", "WEKNORA_ASYNQ_SHARED_CONCURRENCY", 6)
    wiki_concurrency = _runtime_pool_limit("asynq.wiki_concurrency", "WEKNORA_WIKI_ASYNQ_CONCURRENCY", 8)
    pools = [
        {"name": "core", "concurrency": core_concurrency, "queue_count": 2, "instances": 0, "cluster_capacity": 0, "active": 0, "utilization": 0.0},
        {"name": "postprocess", "concurrency": postprocess_concurrency, "queue_count": 1, "instances": 0, "cluster_capacity": 0, "active": 0, "utilization": 0.0},
        {"name": "enrichment", "concurrency": enrichment_concurrency, "queue_count": 5, "instances": 0, "cluster_capacity": 0, "active": 0, "utilization": 0.0},
        {"name": "maintenance", "concurrency": maintenance_concurrency, "queue_count": 2, "instances": 0, "cluster_capacity": 0, "active": 0, "utilization": 0.0},
        {"name": "shared", "concurrency": shared_concurrency, "queue_count": 1, "instances": 1, "cluster_capacity": shared_concurrency, "active": min(active, shared_concurrency), "utilization": min(active, shared_concurrency) / shared_concurrency},
        {"name": "wiki", "concurrency": wiki_concurrency, "queue_count": 1, "instances": 0, "cluster_capacity": 0, "active": 0, "utilization": 0.0},
    ]
    upstream_concurrency = core_concurrency + postprocess_concurrency + enrichment_concurrency + maintenance_concurrency + shared_concurrency
    return {
        "available": True,
        "upstream_concurrency": upstream_concurrency,
        "parse_concurrency": upstream_concurrency,
        "wiki_concurrency": wiki_concurrency,
        "pools": pools,
        "queues": [{
            "name": RUNTIME_QUEUE,
            "pool": "shared",
            "weight": 1,
            "size": sum(counts.values()),
            "pending": pending,
            "active": active,
            "scheduled": 0,
            "retry": retry,
            "archived": archived,
            "completed": completed,
            "processed": completed,
            "failed": archived,
            "paused": False,
            "latency_ms": 0,
            "memory_usage_bytes": 0,
        }],
        "model_limiter_available": True,
        "models": model_limiter.runtime_stats(),
        "timestamp": int(time.time()),
    }


@router.get("/system/admin/runtime/queues/{queue}/tasks")
def runtime_tasks(queue: str, request: Request) -> dict[str, Any]:
    _system_admin(request)
    if queue != RUNTIME_QUEUE:
        raise validation("unknown runtime queue")
    state = str(request.query_params.get("state") or "").strip().lower()
    if state not in {"pending", "active", "scheduled", "retry", "archived", "completed"}:
        raise validation("invalid runtime task state")
    try:
        offset = max(0, int(request.query_params.get("cursor") or 0))
    except ValueError as exc:
        raise validation("invalid runtime task cursor") from exc
    page_size = min(200, max(1, int(request.query_params.get("page_size") or 20)))
    if state == "scheduled":
        return {"available": True, "tasks": [], "page_size": page_size, "has_more": False, "next_cursor": ""}
    now = time.time()
    if state == "pending":
        where = "status='pending' AND (retry_after='' OR CAST(retry_after AS REAL)<=?)"
        params: tuple[Any, ...] = (now,)
    elif state == "retry":
        where = "status='pending' AND retry_after<>'' AND CAST(retry_after AS REAL)>?"
        params = (now,)
    else:
        wanted = {
            "active": ("processing",),
            "archived": ("failed", "cancelled"),
            "completed": ("completed",),
        }[state]
        where = f"status IN ({','.join('?' for _ in wanted)})"
        params = wanted
    rows = db.fetch_all(
        f"SELECT * FROM py_task_results WHERE {where} "
        "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
        (*params, page_size + 1, offset),
    )
    has_more = len(rows) > page_size
    tasks = [_runtime_task(row) for row in rows[:page_size]]
    return {
        "available": True,
        "tasks": tasks,
        "page_size": page_size,
        "has_more": has_more,
        "next_cursor": str(offset + len(tasks)) if has_more else "",
    }


@router.post("/system/admin/runtime/queues/{queue}/tasks/{task_id}/actions/{action}")
async def runtime_task_action(queue: str, task_id: str, action: str, request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    if queue != RUNTIME_QUEUE:
        raise validation("unknown runtime queue")
    if action not in {"cancel", "run_now", "delete"}:
        raise validation("unsupported runtime task action")
    row = db.fetch_one("SELECT * FROM py_task_results WHERE id=?", (task_id,))
    if not row:
        raise conflict("Task is no longer available")
    status = str(row.get("status") or "")
    if action == "cancel":
        if status not in {"pending", "processing"}:
            raise conflict("Action is not allowed for the current task state")
        result = loads(row.get("result"), {})
        if not isinstance(result, dict):
            result = {}
        result.update({"status": "cancelled", "message": "任务已被系统管理员取消"})
        with db.transaction() as conn:
            conn.execute(
                "UPDATE py_task_results SET status='cancelled', result=?, error=?, updated_at=? WHERE id=?",
                (dumps(result), "canceled by system administrator", db.utcnow(), task_id),
            )
        _audit(ctx, "system.queue_task_cancelled", "queue_task", task_id, {"queue": queue, "task_type": row.get("kind")})
    elif action == "delete":
        if status in {"pending", "processing"}:
            raise conflict("Action is not allowed for the current task state")
        with db.transaction() as conn:
            conn.execute("DELETE FROM py_task_results WHERE id=?", (task_id,))
        _audit(ctx, "system.queue_task_deleted", "queue_task", task_id, {"queue": queue, "task_type": row.get("kind")})
    else:
        retry_pending = status == "pending" and _runtime_retry_pending(row.get("retry_after"))
        if (
            status not in {"failed", "cancelled"}
            and not retry_pending
        ) or str(row.get("kind") or "") not in RERUNNABLE_TASK_KINDS:
            raise conflict("Action is not allowed for the current task state")
        result = loads(row.get("result"), {})
        if not isinstance(result, dict):
            result = {}
        result.update({"status": "pending", "message": "任务已进入待执行队列"})
        if retry_pending:
            condition = (
                "id=? AND status='pending' AND retry_after<>'' "
                "AND CAST(retry_after AS REAL)>?"
            )
            action_params: tuple[Any, ...] = (time.time(),)
        else:
            condition = "id=? AND status IN ('failed','cancelled')"
            action_params = ()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE py_task_results SET status='pending', result=?, error='', retry_after='', updated_at=? "
                f"WHERE {condition}",
                (dumps(result), db.utcnow(), task_id, *action_params),
            )
        kind = str(row.get("kind") or "")
        if kind in RERUNNABLE_TASK_KINDS:
            from . import knowledge as knowledge_router

            knowledge_router.schedule_task_worker(task_id)
        _audit(ctx, "system.queue_task_run_now", "queue_task", task_id, {"queue": queue, "task_type": row.get("kind")})
    return {"success": True}


@router.delete("/system/admin/runtime/queues/{queue}/archived")
def purge_archived(queue: str, request: Request) -> dict[str, Any]:
    ctx = _system_admin(request)
    if queue != RUNTIME_QUEUE:
        raise validation("unknown runtime queue")
    with db.transaction() as conn:
        cursor = conn.execute("DELETE FROM py_task_results WHERE status IN ('failed','cancelled')")
        deleted = int(cursor.rowcount or 0)
    _audit(ctx, "system.queue_archived_purged", "task_queue", queue, {"deleted": deleted})
    return {"success": True, "deleted": deleted}


@router.get("/system/admin/tenants")
def admin_tenants(request: Request) -> dict[str, Any]:
    _system_admin(request)
    rows = db.fetch_all("SELECT * FROM tenants WHERE deleted_at IS NULL ORDER BY id", ())
    return {"success": True, "data": rows}

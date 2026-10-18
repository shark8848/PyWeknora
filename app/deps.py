"""Request-scoped authentication and tenant resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from . import config, db
from .api_keys import (
    APIKeyScope,
    authenticate as authenticate_api_key,
    authorize_file_serve,
    authorize_request_knowledge_bases,
    authorize_route,
    invalid_key,
    is_platform_tenant_optional,
    route_path,
)
from .errors import AppError, auth_failure, forbidden
from .security import decode_token


@dataclass(slots=True)
class AuthContext:
    user: dict[str, Any]
    tenant_id: int
    tenant: dict[str, Any]
    role: str
    token_payload: dict[str, Any]
    api_key: APIKeyScope | None = None
    access_token: str = ""

    @property
    def user_id(self) -> str:
        return str(self.user["id"])

    @property
    def is_api_key(self) -> bool:
        return self.api_key is not None


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip()
    return None


def _tenant_from_header(request: Request) -> int:
    raw = str(request.headers.get("x-tenant-id") or "").strip()
    if not raw:
        return 0
    if not raw.isdigit() or int(raw) <= 0:
        raise auth_failure("Invalid X-Tenant-ID header", status=400, code=1010)
    return int(raw)


def _api_key_user(scope: APIKeyScope, tenant_id: int) -> dict[str, Any]:
    if scope.is_platform:
        user_id = f"platform-api-key-{scope.key_id}"
        email = f"{user_id}@api-key.local"
    else:
        user = db.fetch_one(
            """
            SELECT * FROM users
            WHERE tenant_id=? AND is_active=1 AND deleted_at IS NULL
            ORDER BY created_at, id LIMIT 1
            """,
            (tenant_id,),
        )
        if user:
            return user
        user_id = f"system-{tenant_id}"
        email = f"{user_id}@api-key.local"
    now = db.utcnow()
    return {
        "id": user_id,
        "username": user_id,
        "email": email,
        "avatar": "",
        "tenant_id": tenant_id,
        "is_active": True,
        "can_access_all_tenants": False,
        "is_system_admin": False,
        "preferences": "{}",
        "created_at": now,
        "updated_at": now,
    }


def _api_key_context(request: Request, token: str) -> AuthContext:
    scope = authenticate_api_key(token)
    if scope is None:
        raise invalid_key()

    tenant_header = str(request.headers.get("x-tenant-id") or "").strip()
    if scope.is_platform:
        if not tenant_header:
            if not is_platform_tenant_optional(request.method, route_path(request)):
                raise auth_failure(
                    "Workspace required: platform API keys must send X-Tenant-ID",
                    status=409,
                    code=1011,
                    extra={"code": "TENANT_REQUIRED"},
                )
            tenant_id = 0
        else:
            tenant_id = _tenant_from_header(request)
    else:
        row = db.fetch_one("SELECT tenant_id FROM tenant_api_keys WHERE id=?", (scope.key_id,))
        tenant_id = int((row or {}).get("tenant_id") or 0)
        if tenant_id <= 0:
            raise invalid_key()
        if tenant_header and _tenant_from_header(request) != tenant_id:
            raise auth_failure(
                "Forbidden: workspace API key cannot switch workspaces",
                status=403,
                code=1002,
            )

    tenant = (
        db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (tenant_id,))
        if tenant_id
        else {}
    )
    if tenant_id and not tenant:
        raise invalid_key()

    user = _api_key_user(scope, tenant_id)
    role = "owner" if scope.full_access else "viewer"
    authorize_route(scope, request.method, route_path(request))
    if route_path(request) == "/files":
        authorize_file_serve(scope)
    authorize_request_knowledge_bases(request, scope, route_path(request))
    return AuthContext(
        user=user,
        tenant_id=tenant_id,
        tenant=tenant or {},
        role=role,
        token_payload={},
        api_key=scope,
    )


def parse_auth(request: Request, *, required: bool = True) -> AuthContext | None:
    token = _extract_token(request)
    jwt_error = ""
    if token:
        try:
            payload = decode_token(token, "access")
            token_row = db.fetch_one(
                """
                SELECT 1 FROM auth_tokens
                WHERE token=? AND token_type='access_token' AND is_revoked=0
                """,
                (token,),
            )
            if token_row is None:
                raise ValueError("token is revoked")
            user = db.fetch_one(
                "SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (payload.get("sub"),)
            )
            if user and user.get("is_active"):
                requested = _tenant_from_header(request)
                home = int(user.get("tenant_id") or 0)
                tenant_id = requested or int(payload.get("tenant_id") or 0) or home

                member = db.fetch_one(
                    """
                    SELECT role FROM tenant_members
                    WHERE user_id=? AND tenant_id=? AND status='active' AND deleted_at IS NULL
                    """,
                    (user["id"], tenant_id),
                )
                role = member["role"] if member else ""
                if not role and user.get("can_access_all_tenants") and config.ENABLE_CROSS_TENANT_ACCESS:
                    role = "admin"
                if not role:
                    if tenant_id == home:
                        role = "owner"
                    else:
                        raise auth_failure(
                            "Forbidden: not a member of the target workspace",
                            status=403,
                            code=1002,
                        )

                tenant = db.fetch_one(
                    "SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (tenant_id,)
                )
                if not tenant:
                    raise auth_failure(
                        "Unauthorized: invalid workspace",
                        status=401,
                        code=1001,
                    )
                return AuthContext(
                    user=user,
                    tenant_id=tenant_id,
                    tenant=tenant,
                    role=role,
                    token_payload=payload,
                    access_token=token,
                )
            jwt_error = "Unauthorized: invalid or expired token"
        except ValueError:
            jwt_error = "Unauthorized: invalid or expired token"

    api_key = str(request.headers.get("x-api-key") or "").strip()
    if api_key:
        if not required:
            try:
                return _api_key_context(request, api_key)
            except AppError:
                return None
        return _api_key_context(request, api_key)
    if required:
        raise auth_failure(jwt_error or "Unauthorized: missing authentication")
    return None


def require_role(ctx: AuthContext, minimum: str) -> None:
    if ctx.is_api_key:
        return
    levels = {"viewer": 10, "contributor": 20, "admin": 30, "owner": 40}
    if levels.get(ctx.role, 0) < levels.get(minimum, 0):
        raise forbidden(f"{minimum.title()} role required")


def user_json(user: dict[str, Any]) -> dict[str, Any]:
    preferences = user.get("preferences") or "{}"
    if isinstance(preferences, str):
        try:
            preferences = json.loads(preferences)
        except ValueError:
            preferences = {}
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "avatar": user.get("avatar") or "",
        "tenant_id": int(user.get("tenant_id") or 0),
        "is_active": bool(user.get("is_active")),
        "can_access_all_tenants": bool(user.get("can_access_all_tenants")) and config.ENABLE_CROSS_TENANT_ACCESS,
        "is_system_admin": bool(user.get("is_system_admin")),
        "preferences": preferences,
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
    }


def _json_value(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return default


def tenant_json(tenant: dict[str, Any], role: str | None = None) -> dict[str, Any]:
    engines = _json_value(tenant.get("retriever_engines"), {"engines": []})
    if isinstance(engines, list):
        engines = {"engines": engines}
    elif not isinstance(engines, dict):
        engines = {"engines": []}
    context = _json_value(
        tenant.get("context_config"),
        {
            "max_tokens": 0,
            "compression_strategy": "",
            "recent_message_count": 0,
            "summarize_threshold": 0,
        },
    )
    if not isinstance(context, dict):
        context = {}
    result = {
        "id": int(tenant["id"]),
        "name": tenant["name"],
        "description": tenant.get("description") or "",
        "status": tenant.get("status") or "active",
        "retriever_engines": engines,
        "business": tenant.get("business") or "",
        "storage_quota": int(tenant.get("storage_quota") or 0),
        "storage_used": int(tenant.get("storage_used") or 0),
        "context_config": context,
        "created_at": tenant.get("created_at"),
        "updated_at": tenant.get("updated_at"),
        "deleted_at": tenant.get("deleted_at"),
    }
    return result

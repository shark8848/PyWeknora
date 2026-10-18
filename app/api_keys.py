"""API key authentication and route authorization."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request

from . import config, db
from .errors import auth_failure


RETRIEVE = "retrieve"
CHAT = "chat"
READ_AGENTS = "read_agents"
INGEST = "ingest"
MANAGE_KBS = "manage_kbs"
MANAGE_AGENTS = "manage_agents"
MESSAGE_HISTORY = "message_history"
MANAGE_MODELS = "manage_models"
MANAGE_MCP_SERVICES = "manage_mcp_services"
MANAGE_DATASOURCES = "manage_datasources"
MANAGE_CHANNELS = "manage_channels"
MANAGE_VECTOR_STORES = "manage_vector_stores"
MANAGE_STORAGE_BACKENDS = "manage_storage_backends"
MANAGE_WEB_SEARCH = "manage_web_search"
RUN_EVALUATIONS = "run_evaluations"
MANAGE_MEMBERS = "manage_members"
MANAGE_SPACES = "manage_spaces"
MANAGE_TENANT_SETTINGS = "manage_tenant_settings"
SYSTEM_TENANTS_READ = "system_tenants_read"
SYSTEM_TENANTS_MANAGE = "system_tenants_manage"
SYSTEM_SETTINGS_READ = "system_settings_read"
SYSTEM_SETTINGS_MANAGE = "system_settings_manage"
SYSTEM_RUNTIME_READ = "system_runtime_read"
SYSTEM_RUNTIME_MANAGE = "system_runtime_manage"
SYSTEM_AUDIT_READ = "system_audit_read"

CAPABILITIES = frozenset({
    RETRIEVE,
    CHAT,
    READ_AGENTS,
    INGEST,
    MANAGE_KBS,
    MANAGE_AGENTS,
    MESSAGE_HISTORY,
    MANAGE_MODELS,
    MANAGE_MCP_SERVICES,
    MANAGE_DATASOURCES,
    MANAGE_CHANNELS,
    MANAGE_VECTOR_STORES,
    MANAGE_STORAGE_BACKENDS,
    MANAGE_WEB_SEARCH,
    RUN_EVALUATIONS,
    MANAGE_MEMBERS,
    MANAGE_SPACES,
    MANAGE_TENANT_SETTINGS,
    SYSTEM_TENANTS_READ,
    SYSTEM_TENANTS_MANAGE,
    SYSTEM_SETTINGS_READ,
    SYSTEM_SETTINGS_MANAGE,
    SYSTEM_RUNTIME_READ,
    SYSTEM_RUNTIME_MANAGE,
    SYSTEM_AUDIT_READ,
})

_PARAM_RE = re.compile(r"\{[^}]+\}")
_LAST_USED_INTERVAL = timedelta(minutes=1)


def normalize_capabilities(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or []:
        capability = str(value).strip().lower()
        if capability in CAPABILITIES and capability not in result:
            result.append(capability)
    return result


def normalize_knowledge_base_ids(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or []:
        kb_id = str(value).strip()
        if kb_id and kb_id not in result:
            result.append(kb_id)
    return result


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(slots=True)
class APIKeyScope:
    key_id: int
    name: str
    scope_type: str
    full_access: bool
    knowledge_base_ids: tuple[str, ...] = field(default_factory=tuple)
    capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_platform(self) -> bool:
        return self.scope_type == "platform"

    @property
    def is_kb_restricted(self) -> bool:
        return bool(self.knowledge_base_ids)

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def allows_kb(self, kb_id: str) -> bool:
        return not self.is_kb_restricted or str(kb_id) in self.knowledge_base_ids


@dataclass(frozen=True, slots=True)
class RoutePolicy:
    platform_only: bool = False
    full_access: bool = False
    capabilities: frozenset[str] = frozenset()


ANY = RoutePolicy()
FULL = RoutePolicy(full_access=True)


def _cap(*capabilities: str) -> RoutePolicy:
    return RoutePolicy(full_access=True, capabilities=frozenset(capabilities))


def _platform(*capabilities: str) -> RoutePolicy:
    return RoutePolicy(platform_only=True, capabilities=frozenset(capabilities))


RETRIEVE_OR_INGEST = _cap(RETRIEVE, INGEST)
RETRIEVE_OR_MANAGE_KBS = _cap(RETRIEVE, MANAGE_KBS)
AGENT_READ = _cap(READ_AGENTS, MANAGE_AGENTS, CHAT)


def normalize_route(path: str) -> str:
    value = _PARAM_RE.sub("{}", str(path or ""))
    return value.rstrip("/") or "/"


def route_path(request: Request) -> str:
    route = request.scope.get("route")
    path = str(getattr(route, "path", request.url.path))
    prefix = config.API_PREFIX.rstrip("/")
    if request.url.path.startswith(prefix + "/") and not path.startswith(prefix + "/"):
        path = prefix + path
    return normalize_route(path)


def policy_for(method: str, path: str) -> RoutePolicy | None:
    """Return the declared API-key policy, or None for a default-denied route."""
    method = method.upper()
    p = normalize_route(path)
    get = method == "GET"
    post = method == "POST"
    put = method == "PUT"
    delete = method == "DELETE"

    if get and p == "/api/v1/auth/me":
        return ANY
    if get and p == "/files":
        return ANY
    if get and p == "/api/v1/system/capabilities":
        return ANY

    if p.startswith("/api/v1/system/admin"):
        rest = p.removeprefix("/api/v1/system/admin")
        if get and rest in {"", "/settings", "/settings/{}"}:
            return _platform(SYSTEM_SETTINGS_READ, SYSTEM_SETTINGS_MANAGE)
        if put or delete:
            if rest == "/settings/{}":
                return _platform(SYSTEM_SETTINGS_MANAGE)
        if get and rest in {"/runtime/queues", "/runtime/queues/{}/tasks"}:
            return _platform(SYSTEM_RUNTIME_READ, SYSTEM_RUNTIME_MANAGE)
        if rest == "/runtime/queues/{}/tasks/{}/actions/{}" and post:
            return _platform(SYSTEM_RUNTIME_MANAGE)
        if rest == "/runtime/queues/{}/archived" and delete:
            return _platform(SYSTEM_RUNTIME_MANAGE)
        if rest == "/tenants/apply-default-storage-quota" and post:
            return _platform(SYSTEM_TENANTS_MANAGE)
        if rest == "/audit-log" and get:
            return _platform(SYSTEM_AUDIT_READ)
        return None

    if p in {"/api/v1/tenants/all", "/api/v1/tenants/search"} and get:
        return _platform(SYSTEM_TENANTS_READ, SYSTEM_TENANTS_MANAGE)
    if p == "/api/v1/tenants" and post:
        return _platform(SYSTEM_TENANTS_MANAGE)
    if p == "/api/v1/tenants" and get:
        return _cap(MANAGE_TENANT_SETTINGS)
    if p in {
        "/api/v1/tenants/kv/prompt-templates",
        "/api/v1/tenants/kv/web-search-config",
        "/api/v1/tenants/kv/{}",
    } and method in {"GET", "PUT"}:
        return _cap(MANAGE_TENANT_SETTINGS)
    if p.startswith("/api/v1/tenants/"):
        rest = p.removeprefix("/api/v1/tenants/")
        first, _, tail = rest.partition("/")
        if first == "{}":
            if not tail and get:
                return _platform(SYSTEM_TENANTS_READ, SYSTEM_TENANTS_MANAGE)
            if not tail and method in {"PUT", "DELETE"}:
                return _platform(SYSTEM_TENANTS_MANAGE)
            if tail == "members" or tail.startswith("members/"):
                return _cap(MANAGE_MEMBERS)
            if tail == "invitations" or tail.startswith("invitations/"):
                return _cap(MANAGE_MEMBERS)
            if tail == "invite-links":
                return _cap(MANAGE_MEMBERS)
        return None

    if p == "/api/v1/knowledge-bases" and get:
        return _cap(RETRIEVE)
    if p == "/api/v1/knowledge-bases" and post:
        return _cap(MANAGE_KBS)
    if p == "/api/v1/knowledge-bases/move-targets" and get:
        return _cap(RETRIEVE)
    if p == "/api/v1/knowledge-bases/copy" and post:
        return _cap(MANAGE_KBS)
    if p == "/api/v1/knowledge-bases/copy/progress/{}" and get:
        return RETRIEVE_OR_MANAGE_KBS
    if p.startswith("/api/v1/knowledge-bases/"):
        rest = p.removeprefix("/api/v1/knowledge-bases/")
        first, _, tail = rest.partition("/")
        if first != "{}":
            return None
        if tail.startswith("shares"):
            return FULL
        if not tail:
            if get:
                return _cap(RETRIEVE)
            if method in {"PUT", "DELETE"}:
                return _cap(MANAGE_KBS)
            return None
        if tail == "pin" and put:
            return _cap(RETRIEVE)
        if tail in {"gallery-config", "images", "move-targets", "files"} and get:
            return _cap(RETRIEVE)
        if tail == "hybrid-search" and method in {"GET", "POST"}:
            return _cap(RETRIEVE)
        if tail == "duplicate" and post:
            return _cap(MANAGE_KBS)
        if tail in {"profile/generate", "rebuild-index"} and post:
            return _cap(MANAGE_KBS)
        if tail == "knowledge" and delete:
            return FULL
        if tail == "knowledge/batch-download" and post:
            return _cap(RETRIEVE)
        if tail == "faq/search" and post:
            return _cap(RETRIEVE)
        if get and (tail.startswith("knowledge") or tail.startswith("tags") or tail.startswith("faq")):
            return _cap(RETRIEVE)
        if method in {"POST", "PUT", "DELETE"} and (
            tail.startswith("knowledge") or tail.startswith("tags") or tail.startswith("faq")
        ):
            return _cap(INGEST)
        return None

    if p == "/api/v1/knowledge-search" and post:
        return _cap(RETRIEVE)
    if p == "/api/v1/knowledge/search" and get:
        return _cap(RETRIEVE)
    if p == "/api/v1/knowledge/batch" and get:
        return _cap(RETRIEVE)
    if p.startswith("/api/v1/knowledge/"):
        if get:
            return _cap(RETRIEVE)
        if method in {"POST", "PUT", "DELETE"}:
            return _cap(INGEST)
        return None

    if p.startswith("/api/v1/chunks"):
        if get:
            return _cap(RETRIEVE)
        if method in {"POST", "PUT", "DELETE"}:
            return _cap(INGEST)
        return None
    if p == "/api/v1/chunker/preview" and method in {"GET", "POST"}:
        return RETRIEVE_OR_INGEST
    if p == "/api/v1/image-attrs/schema" and get:
        return _cap(RETRIEVE)
    if p == "/api/v1/faq/import/progress/{}" and get:
        return RETRIEVE_OR_INGEST
    if p == "/api/v1/wiki-search" and post:
        return _cap(RETRIEVE)
    if p.startswith("/api/v1/knowledgebase/{}/wiki"):
        if get:
            return _cap(RETRIEVE)
        if method in {"POST", "PUT", "DELETE"}:
            return _cap(INGEST)
        return None

    if p == "/api/v1/messages/search" and post:
        return _cap(MESSAGE_HISTORY)
    if p == "/api/v1/messages/chat-history-stats" and get:
        return _cap(MESSAGE_HISTORY)
    if p.startswith("/api/v1/messages"):
        return _cap(CHAT)
    if p.startswith("/api/v1/sessions") or p.startswith("/api/v1/artifacts"):
        return _cap(CHAT)
    if p.startswith("/api/v1/knowledge-chat/") or p.startswith("/api/v1/agent-chat/"):
        return _cap(CHAT)

    if p.startswith("/api/v1/agents/{}" ):
        rest = p.removeprefix("/api/v1/agents/")
        _, _, tail = rest.partition("/")
        if tail.startswith("shares"):
            return FULL
        if tail.startswith("im-channels") or tail.startswith("embed-channels"):
            return _cap(MANAGE_CHANNELS)
    if p == "/api/v1/agents" or p.startswith("/api/v1/agents/"):
        if get:
            return AGENT_READ
        if method in {"POST", "PUT", "DELETE"}:
            return _cap(MANAGE_AGENTS)
        return None
    if (
        p.startswith("/api/v1/im-channels")
        or p.startswith("/api/v1/embed-channels")
        or p.startswith("/api/v1/wechat")
    ):
        return _cap(MANAGE_CHANNELS)

    if p.startswith("/api/v1/organizations") or p in {
        "/api/v1/shared-knowledge-bases",
        "/api/v1/shared-agents",
        "/api/v1/shared-agents/disabled",
    }:
        return _cap(MANAGE_SPACES)

    if p.startswith("/api/v1/initialization/config/") and get:
        return _cap(RETRIEVE)
    if p.startswith("/api/v1/initialization/config/") and put:
        return _cap(MANAGE_KBS)
    if p.startswith("/api/v1/initialization/initialize/") and post:
        return _cap(MANAGE_KBS)
    if p.startswith("/api/v1/initialization"):
        return _cap(MANAGE_MODELS)

    if p.startswith("/api/v1/sandbox-configs"):
        return FULL
    if p == "/api/v1/skills/catalog" and get:
        return None
    if p.startswith("/api/v1/skills/catalog"):
        return FULL
    if p.startswith("/api/v1/memory"):
        return FULL

    if p.startswith("/api/v1/models"):
        return _cap(MANAGE_MODELS)
    if p.startswith("/api/v1/weknoracloud"):
        return _cap(MANAGE_MODELS)
    if p.startswith("/api/v1/evaluation"):
        return _cap(RUN_EVALUATIONS)
    if p.startswith("/api/v1/mcp-services"):
        return _cap(MANAGE_MCP_SERVICES)
    if p.startswith("/api/v1/mcp-endpoints"):
        return _cap(MANAGE_CHANNELS)
    if p.startswith("/api/v1/web-search-providers"):
        return _cap(MANAGE_WEB_SEARCH)
    if p.startswith("/api/v1/vector-stores"):
        return _cap(MANAGE_VECTOR_STORES)
    if p.startswith("/api/v1/storage-backends"):
        return _cap(MANAGE_STORAGE_BACKENDS)
    if p.startswith("/api/v1/datasource"):
        return _cap(MANAGE_DATASOURCES)
    if p == "/api/v1/system/host-project-dir":
        return None
    if p.startswith("/api/v1/system"):
        return _cap(MANAGE_VECTOR_STORES)
    return None


def authorize_route(scope: APIKeyScope, method: str, path: str) -> None:
    policy = policy_for(method, path)
    if policy is None:
        raise auth_failure("Forbidden: API key scope does not allow this operation", status=403, code=1002)
    if policy.platform_only and not scope.is_platform:
        raise auth_failure("Forbidden: API key scope does not allow this operation", status=403, code=1002)
    if policy.platform_only and not policy.capabilities:
        raise auth_failure("Forbidden: API key scope does not allow this operation", status=403, code=1002)
    if scope.full_access:
        return
    if policy.capabilities and policy.capabilities.intersection(scope.capabilities):
        return
    if not policy.full_access and not policy.capabilities:
        return
    raise auth_failure("Forbidden: API key scope does not allow this operation", status=403, code=1002)


def authorize_file_serve(scope: APIKeyScope) -> None:
    if scope.full_access:
        return
    if not scope.is_kb_restricted and scope.has(RETRIEVE):
        return
    raise auth_failure("Forbidden: API key scope does not allow this operation", status=403, code=1002)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def authenticate(token: str) -> APIKeyScope | None:
    token = str(token or "").strip()
    if not token:
        return None
    row = db.fetch_one(
        "SELECT * FROM tenant_api_keys WHERE key_hash=? AND revoked_at IS NULL",
        (hash_token(token),),
    )
    if not row:
        return None
    expires_at = _parse_timestamp(row.get("expires_at"))
    if expires_at is not None and datetime.now(timezone.utc) >= expires_at:
        return None

    last_used = _parse_timestamp(row.get("last_used_at"))
    now = datetime.now(timezone.utc)
    if last_used is None or now - last_used >= _LAST_USED_INTERVAL:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE tenant_api_keys SET last_used_at=?, updated_at=? WHERE id=?",
                (db.utcnow(), db.utcnow(), row["id"]),
            )

    scope_type = "platform" if str(row.get("scope_type") or "").lower() == "platform" else "tenant"
    return APIKeyScope(
        key_id=int(row["id"]),
        name=str(row.get("name") or ""),
        scope_type=scope_type,
        full_access=bool(row.get("full_access")) and scope_type == "tenant",
        knowledge_base_ids=tuple(normalize_knowledge_base_ids(json_load(row.get("knowledge_base_ids")))),
        capabilities=frozenset(normalize_capabilities(json_load(row.get("capabilities")))),
    )


def json_load(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    import json

    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def is_platform_tenant_optional(method: str, path: str) -> bool:
    p = normalize_route(path)
    if p == "/api/v1/system/admin" or p.startswith("/api/v1/system/admin/"):
        return True
    if method.upper() == "GET" and p in {"/api/v1/tenants/all", "/api/v1/tenants/search"}:
        return True
    return method.upper() == "POST" and p == "/api/v1/tenants"


def request_knowledge_base_ids(request: Request, path: str) -> set[str]:
    """Resolve KB ids referenced directly by the URL, when the route has one."""
    p = normalize_route(path)
    params = request.path_params
    kb_id = ""
    if p == "/api/v1/knowledge-bases/{}" or p.startswith("/api/v1/knowledge-bases/{}/"):
        kb_id = str(params.get("kb_id") or params.get("id") or "")
    elif p.startswith("/api/v1/knowledgebase/"):
        kb_id = str(params.get("kb_id") or params.get("id") or "")
    elif p.startswith("/api/v1/initialization/"):
        kb_id = str(params.get("kb_id") or params.get("id") or "")
    elif p.startswith("/api/v1/knowledge/"):
        knowledge_id = str(params.get("knowledge_id") or "")
        if knowledge_id:
            row = db.fetch_one("SELECT knowledge_base_id FROM knowledges WHERE id=?", (knowledge_id,))
            kb_id = str(row.get("knowledge_base_id") or "") if row else ""
    elif p.startswith("/api/v1/chunks/"):
        knowledge_id = str(params.get("knowledge_id") or "")
        if knowledge_id:
            row = db.fetch_one("SELECT knowledge_base_id FROM knowledges WHERE id=?", (knowledge_id,))
            kb_id = str(row.get("knowledge_base_id") or "") if row else ""
        else:
            chunk_id = str(params.get("chunk_id") or params.get("id") or "")
            if chunk_id:
                row = db.fetch_one("SELECT knowledge_base_id FROM chunks WHERE id=?", (chunk_id,))
                kb_id = str(row.get("knowledge_base_id") or "") if row else ""
    return {kb_id} if kb_id else set()


def authorize_knowledge_bases(scope: APIKeyScope, kb_ids: set[str] | list[str] | tuple[str, ...]) -> None:
    if not scope.is_kb_restricted:
        return
    for kb_id in kb_ids:
        if not scope.allows_kb(str(kb_id)):
            raise auth_failure(
                "Forbidden: API key scope does not allow one or more knowledge bases",
                status=403,
                code=1002,
            )


def authorize_request_knowledge_bases(request: Request, scope: APIKeyScope, path: str) -> None:
    if scope.is_kb_restricted:
        authorize_knowledge_bases(scope, request_knowledge_base_ids(request, path))
        kb_id = str(request.query_params.get("kb_id") or "")
        if kb_id:
            authorize_knowledge_bases(scope, {kb_id})


def invalid_key() -> Exception:
    return auth_failure("Unauthorized: invalid API key")

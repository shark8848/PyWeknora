"""Custom agents, skills, IM channels, favorites and user environment values."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request

from .. import db
from ..api_keys import authorize_knowledge_bases
from ..deps import parse_auth, require_role
from ..errors import forbidden, not_found, validation
from ..services.agents import (
    USER_BUILTIN_AGENT_IDS,
    agent_config,
    agent_type_presets,
    agent_suggested_questions,
    builtin_agent,
    builtin_agents,
)
from ..services import im_runtime
from ..utils import dumps, loads, ok, resolve_locale


router = APIRouter(tags=["agents"])

IM_PLATFORMS = frozenset({
    "wecom", "feishu", "lark", "slack", "telegram", "dingtalk",
    "mattermost", "wechat", "qqbot", "yunzhijia",
})
IM_LOCALES = frozenset({"zh-CN", "zh-TW", "en-US", "ko-KR", "ja-JP", "ru-RU"})
IM_WEBHOOK_PLATFORMS = frozenset({"mattermost", "yunzhijia"})


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _serialize(row: dict[str, Any], *, with_creator_name: bool = False) -> dict[str, Any]:
    result = {
        "id": row.get("id"),
        "name": row.get("name") or "",
        "description": row.get("description") or "",
        "avatar": row.get("avatar") or "",
        "is_builtin": bool(row.get("is_builtin")),
        "tenant_id": row.get("tenant_id"),
        "created_by": row.get("created_by") or "",
        "config": loads(row.get("config"), {}),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "deleted_at": row.get("deleted_at"),
    }
    if with_creator_name and not result["is_builtin"] and result["created_by"]:
        creator = db.fetch_one(
            "SELECT username, email FROM users WHERE id=?",
            (result["created_by"],),
        )
        if creator:
            result["creator_name"] = creator.get("username") or creator.get("email") or ""
    return result


def _builtin(agent_id: str) -> dict[str, Any] | None:
    return builtin_agent(agent_id)


def _request_locale(request: Request) -> str:
    return resolve_locale(request.headers.get("accept-language", ""))


def _im_platform(value: Any) -> str:
    platform = str(value or "").strip()
    if not platform:
        raise validation("platform is required")
    if platform not in IM_PLATFORMS:
        choices = ", ".join(f"'{item}'" for item in sorted(IM_PLATFORMS))
        raise validation("platform must be one of: " + choices)
    return platform


def _im_locale(value: Any) -> str:
    locale = str(value or "").strip()
    if not locale:
        return ""
    if locale not in IM_LOCALES:
        raise validation(
            "locale must be one of: 'en-US', 'ja-JP', 'ko-KR', 'ru-RU', 'zh-CN', 'zh-TW'"
        )
    return locale


def _im_knowledge_base_id(ctx: Any, value: Any) -> str:
    kb_id = str(value or "").strip()
    if not kb_id:
        return ""
    if ctx.api_key is not None and not ctx.api_key.allows_kb(kb_id):
        raise validation("knowledge base not found")
    row = db.fetch_one(
        "SELECT id FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, ctx.tenant_id),
    )
    if not row:
        raise validation("knowledge base not found")
    return kb_id


def _require_im_agent(ctx: Any, agent_id: str) -> None:
    if _builtin(agent_id):
        return
    row = db.fetch_one(
        "SELECT id FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (agent_id, ctx.tenant_id),
    )
    if not row:
        raise validation("agent not found")


def _stored_builtin(
    agent_id: str, ctx: Any, locale: str
) -> dict[str, Any] | None:
    if not _builtin(agent_id):
        return None
    row = db.fetch_one(
        "SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (agent_id, ctx.tenant_id),
    )
    if not row:
        return None
    result = _serialize(row)
    localized = builtin_agent(agent_id, ctx.tenant_id, locale)
    if localized:
        for key in ("name", "description", "avatar"):
            result[key] = localized.get(key, result[key])
    return result


def _agent_row(agent_id: str, ctx: Any) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (agent_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Agent not found")
    return row


def _owned_or_admin(ctx: Any, owner_id: Any) -> None:
    if owner_id and str(owner_id) == ctx.user_id:
        return
    require_role(ctx, "admin")


@router.get("/agents")
def list_agents(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    creator = request.query_params.get("creator", "all")
    locale = _request_locale(request)
    rows = db.fetch_all(
        "SELECT * FROM custom_agents WHERE tenant_id=? AND deleted_at IS NULL ORDER BY created_at DESC",
        (ctx.tenant_id,),
    )
    builtin_rows = {
        str(row["id"]): _stored_builtin(str(row["id"]), ctx, locale)
        for row in rows
        if str(row["id"]) in USER_BUILTIN_AGENT_IDS
    }
    agents = [
        builtin_rows.get(agent_id) or builtin_agent(agent_id, ctx.tenant_id, locale)
        for agent_id in USER_BUILTIN_AGENT_IDS
    ]
    agents.extend(
        _serialize(row, with_creator_name=True)
        for row in rows
        if not _builtin(str(row["id"]))
    )
    if creator == "mine":
        agents = [
            item for item in agents
            if item["is_builtin"] or item["created_by"] == ctx.user_id
        ]
    elif creator == "others":
        agents = [
            item for item in agents
            if item["is_builtin"] or (
                item["created_by"] and item["created_by"] != ctx.user_id
            )
        ]
    disabled = db.fetch_all(
        "SELECT agent_id FROM tenant_disabled_shared_agents WHERE tenant_id=?", (ctx.tenant_id,)
    )
    return {"success": True, "data": agents, "disabled_own_agent_ids": [row.get("agent_id") or row.get("shared_agent_id") for row in disabled]}


@router.get("/agents/placeholders")
def placeholders(request: Request) -> dict[str, Any]:
    parse_auth(request)
    query = {
        "name": "query",
        "label": "用户问题",
        "description": "用户当前的问题或查询内容",
    }
    contexts = {
        "name": "contexts",
        "label": "检索内容",
        "description": "从知识库检索到的相关内容列表",
    }
    current_time = {
        "name": "current_time",
        "label": "当前时间",
        "description": "当前日期（ISO 格式：2006-01-02）。只用日期、不用时钟，避免秒级变化打断 provider 前缀缓存。",
    }
    current_week = {
        "name": "current_week",
        "label": "当前星期",
        "description": "当前星期几（如：星期一、Monday）",
    }
    conversation = {
        "name": "conversation",
        "label": "历史对话",
        "description": "格式化的历史对话内容，用于多轮对话改写",
    }
    yesterday = {
        "name": "yesterday",
        "label": "昨天日期",
        "description": "昨天的日期（格式：2006-01-02）",
    }
    answer = {
        "name": "answer",
        "label": "助手回答",
        "description": "助手的回答内容（用于对话历史格式化）",
    }
    knowledge_bases = {
        "name": "knowledge_bases",
        "label": "知识库列表",
        "description": "自动格式化的知识库列表，包含名称、描述、文档数量等信息",
    }
    web_search_status = {
        "name": "web_search_status",
        "label": "网络搜索状态",
        "description": "网络搜索工具是否启用的状态（Enabled 或 Disabled）",
    }
    language = {
        "name": "language",
        "label": "用户语言",
        "description": "用户界面的语言偏好，如 Chinese (Simplified)、English、Korean 等，用于控制 LLM 回答语言",
    }
    return {
        "success": True,
        "data": {
            "all": [
                query, contexts, current_time, current_week, conversation,
                yesterday, answer, knowledge_bases, web_search_status, language,
            ],
            "system_prompt": [
                query, contexts, current_time, current_week, language,
            ],
            "agent_system_prompt": [
                knowledge_bases, web_search_status, current_time, language,
            ],
            "context_template": [
                query, contexts, current_time, current_week, language,
            ],
            "rewrite_system_prompt": [
                query, conversation, current_time, yesterday, language,
            ],
            "rewrite_prompt": [
                query, conversation, current_time, yesterday, language,
            ],
            "fallback_prompt": [query, language],
        },
    }


@router.get("/agents/type-presets")
def type_presets(request: Request) -> dict[str, Any]:
    parse_auth(request)
    locale = _request_locale(request)
    return {"success": True, "data": agent_type_presets(locale)}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    locale = _request_locale(request)
    if _builtin(agent_id):
        return ok(
            _stored_builtin(agent_id, ctx, locale)
            or builtin_agent(agent_id, ctx.tenant_id, locale)
        )
    row = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
    if not row:
        raise not_found("Agent not found")
    return ok(_serialize(row))


@router.post("/agents", status_code=201)
async def create_agent(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "contributor")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("Agent name is required")
    agent_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO custom_agents(id, name, description, avatar, is_builtin,
              tenant_id, created_by, runnable_by_viewer, config, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_id, name, str(data.get("description") or ""), str(data.get("avatar") or ""),
                ctx.tenant_id, ctx.user_id, 1,
                dumps(data.get("config") or {}), now, now,
            ),
        )
    return ok(_serialize(db.fetch_one("SELECT * FROM custom_agents WHERE id=?", (agent_id,))))


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    if _builtin(agent_id):
        raise forbidden("Built-in agents cannot be modified")
    existing = _agent_row(agent_id, ctx)
    _owned_or_admin(ctx, existing.get("created_by"))
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    for key in ("name", "description", "avatar"):
        if key in data:
            updates[key] = str(data.get(key) or "")
    if "config" in data:
        updates["config"] = dumps(data["config"] or {})
    with db.transaction() as conn:
        db.update("custom_agents", updates, "id=? AND tenant_id=?", (agent_id, ctx.tenant_id), conn)
    row = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
    if not row:
        raise not_found("Agent not found")
    return ok(_serialize(row))


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    if _builtin(agent_id):
        raise forbidden("Built-in agents cannot be deleted")
    existing = _agent_row(agent_id, ctx)
    _owned_or_admin(ctx, existing.get("created_by"))
    with db.transaction() as conn:
        conn.execute("UPDATE custom_agents SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), agent_id, ctx.tenant_id))
    return ok(message="Agent deleted")


@router.post("/agents/{agent_id}/copy", status_code=201)
def copy_agent(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "contributor")
    locale = _request_locale(request)
    if _builtin(agent_id):
        source = (
            _stored_builtin(agent_id, ctx, locale)
            or builtin_agent(agent_id, ctx.tenant_id, locale)
        )
        assert source is not None
    else:
        row = db.fetch_one("SELECT * FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (agent_id, ctx.tenant_id))
        if not row:
            raise not_found("Agent not found")
        source = _serialize(row)
    new_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO custom_agents(id, name, description, avatar, is_builtin,
              tenant_id, created_by, runnable_by_viewer, config, created_at, updated_at)
            VALUES (?, ?, ?, ?, 0, ?, ?, 1, ?, ?, ?)
            """,
            (new_id, source.get("name", "Agent") + " 副本", source.get("description", ""), source.get("avatar", ""), ctx.tenant_id, ctx.user_id, dumps(source.get("config") or {}), now, now),
        )
    return ok(_serialize(db.fetch_one("SELECT * FROM custom_agents WHERE id=?", (new_id,))))


@router.get("/agents/{agent_id}/suggested-questions")
def suggested_questions(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    if not _builtin(agent_id):
        _agent_row(agent_id, ctx)
    raw_tag_scopes = request.query_params.get("tag_scopes", "").strip()
    if raw_tag_scopes:
        try:
            tag_scopes = json.loads(raw_tag_scopes)
        except ValueError as exc:
            raise validation("tag_scopes must be valid JSON") from exc
        if not isinstance(tag_scopes, list):
            raise validation("tag_scopes must be valid JSON")
        for scope in tag_scopes:
            if not isinstance(scope, dict):
                raise validation("tag_scopes must be valid JSON")
            knowledge_base_id = scope.get("knowledge_base_id")
            tag_ids = scope.get("tag_ids")
            if knowledge_base_id is not None and not isinstance(knowledge_base_id, str):
                raise validation("tag_scopes must be valid JSON")
            if tag_ids is not None and (
                not isinstance(tag_ids, list)
                or any(not isinstance(tag_id, str) for tag_id in tag_ids)
            ):
                raise validation("tag_scopes must be valid JSON")
    else:
        tag_scopes = []
    try:
        limit = int(request.query_params.get("limit") or 0)
    except ValueError:
        limit = 0
    kb_ids = [
        value.strip()
        for value in request.query_params.get("knowledge_base_ids", "").split(",")
        if value.strip()
    ]
    knowledge_ids = [
        value.strip()
        for value in request.query_params.get("knowledge_ids", "").split(",")
        if value.strip()
    ]
    restricted_kb_ids = None
    if ctx.api_key is not None and ctx.api_key.is_kb_restricted:
        if knowledge_ids:
            raise forbidden(
                "API key scope does not allow knowledge_ids without a verified knowledge base"
            )
        authorize_knowledge_bases(ctx.api_key, kb_ids)
        if any(scope.get("tag_ids") for scope in tag_scopes):
            raise forbidden(
                "API key scope does not allow tag_ids without a verified knowledge base"
            )
        restricted_kb_ids = list(ctx.api_key.knowledge_base_ids)
    questions = agent_suggested_questions(
        ctx.tenant_id,
        agent_config(ctx.tenant_id, agent_id),
        kb_ids,
        knowledge_ids,
        limit,
        _request_locale(request),
        tag_scopes=tag_scopes,
        allowed_kb_ids=restricted_kb_ids,
    )
    return ok({"questions": questions})


@router.get("/im-channels")
def list_all_im_channels(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    locale = _request_locale(request)
    rows = db.fetch_all(
        "SELECT c.id, c.tenant_id, c.agent_id, c.platform, c.name, c.enabled, c.mode, "
        "c.output_mode, c.locale, c.session_mode, c.bot_identity, c.created_at, "
        "c.updated_at, a.id AS custom_agent_id, a.name AS custom_agent_name "
        "FROM im_channels c LEFT JOIN custom_agents a "
        "ON a.id=c.agent_id AND a.tenant_id=c.tenant_id AND a.deleted_at IS NULL "
        "WHERE c.tenant_id=? AND c.deleted_at IS NULL ORDER BY c.created_at DESC",
        (ctx.tenant_id,),
    )
    result = []
    for row in rows:
        agent_name = row.get("custom_agent_name") or ""
        builtin = builtin_agent(
            str(row.get("agent_id") or ""), ctx.tenant_id, locale
        )
        if builtin and builtin.get("name"):
            agent_name = builtin["name"]
        if row.get("custom_agent_id") is None and builtin is None:
            continue
        result.append(
            {
                "id": row.get("id"),
                "tenant_id": row.get("tenant_id"),
                "agent_id": row.get("agent_id"),
                "agent_name": agent_name,
                "platform": row.get("platform"),
                "name": row.get("name"),
                "enabled": bool(row.get("enabled")),
                "mode": row.get("mode"),
                "output_mode": row.get("output_mode"),
                "locale": row.get("locale"),
                "session_mode": row.get("session_mode"),
                "bot_identity": row.get("bot_identity"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return ok(result)


@router.get("/agents/{agent_id}/im-channels")
def list_agent_im_channels(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT * FROM im_channels WHERE tenant_id=? AND agent_id=? AND deleted_at IS NULL "
        "ORDER BY created_at DESC",
        (ctx.tenant_id, agent_id),
    )
    result = []
    for row in rows:
        text = str(row.get("credentials") or "").strip()
        result.append(
            {
                "id": row.get("id"),
                "tenant_id": row.get("tenant_id"),
                "agent_id": row.get("agent_id"),
                "platform": row.get("platform"),
                "name": row.get("name"),
                "enabled": bool(row.get("enabled")),
                "mode": row.get("mode"),
                "output_mode": row.get("output_mode"),
                "locale": row.get("locale"),
                "knowledge_base_id": row.get("knowledge_base_id"),
                "bot_identity": row.get("bot_identity"),
                "session_mode": row.get("session_mode"),
                "credentials_configured": text not in {"", "{}"},
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return ok(result)


def _im_channel_response(channel_id: str, tenant_id: int) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM im_channels WHERE id=? AND tenant_id=?", (channel_id, tenant_id))
    if row:
        row["credentials"] = loads(row.get("credentials"), {})
    return row


def _im_channel(ctx: Any, channel_id: str) -> dict[str, Any]:
    row = _im_channel_response(channel_id, ctx.tenant_id)
    if not row:
        raise not_found("IM channel not found")
    return row


@router.post("/agents/{agent_id}/im-channels")
async def create_im_channel(agent_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    platform = _im_platform(data.get("platform"))
    locale = _im_locale(data.get("locale"))
    knowledge_base_id = _im_knowledge_base_id(ctx, data.get("knowledge_base_id"))
    _require_im_agent(ctx, agent_id)
    channel_id = db.new_id()
    now = db.utcnow()
    credentials = data.get("credentials") if isinstance(data.get("credentials"), dict) else data.get("config") or {}
    if platform == "wechat":
        mode = "longpoll"
        output_mode = "full"
    else:
        mode = str(data.get("mode") or ("webhook" if platform in IM_WEBHOOK_PLATFORMS else "websocket"))
        output_mode = str(data.get("output_mode") or "stream")
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO im_channels(id, tenant_id, agent_id, platform, name, enabled, mode,
              output_mode, credentials, knowledge_base_id, session_mode, locale, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                ctx.tenant_id,
                agent_id,
                platform,
                str(data.get("name") or ""),
                1 if data.get("enabled", True) else 0,
                mode,
                output_mode,
                dumps(credentials),
                knowledge_base_id,
                str(data.get("session_mode") or "user"),
                locale,
                now,
                now,
            ),
        )
    im_runtime.notify_channel(channel_id)
    return ok(_im_channel_response(channel_id, ctx.tenant_id))


@router.put("/im-channels/{channel_id}")
async def update_im_channel(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    existing = _im_channel(ctx, channel_id)
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "mode", "output_mode", "session_mode"):
        if key in data:
            updates[key] = str(data[key] or "")
    if "locale" in data:
        updates["locale"] = _im_locale(data.get("locale"))
    if "knowledge_base_id" in data:
        updates["knowledge_base_id"] = _im_knowledge_base_id(
            ctx, data.get("knowledge_base_id")
        )
    if "agent_id" in data:
        assigned_agent_id = str(data.get("agent_id") or "").strip()
        if assigned_agent_id and assigned_agent_id != existing.get("agent_id"):
            _require_im_agent(ctx, assigned_agent_id)
            updates["agent_id"] = assigned_agent_id
    if "enabled" in data:
        updates["enabled"] = 1 if data["enabled"] else 0
    credentials = data.get("credentials") if isinstance(data.get("credentials"), dict) else data.get("config")
    if isinstance(credentials, dict):
        updates["credentials"] = dumps(credentials)
    with db.transaction() as conn:
        db.update("im_channels", updates, "id=? AND tenant_id=?", (channel_id, ctx.tenant_id), conn)
    im_runtime.notify_channel(channel_id)
    return ok(_im_channel_response(channel_id, ctx.tenant_id))


@router.delete("/im-channels/{channel_id}")
def delete_im_channel(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _im_channel(ctx, channel_id)
    with db.transaction() as conn:
        conn.execute("UPDATE im_channels SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), channel_id, ctx.tenant_id))
    im_runtime.notify_channel(channel_id)
    return ok(message="Channel deleted")


@router.post("/im-channels/{channel_id}/toggle")
def toggle_im_channel(channel_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _im_channel(ctx, channel_id)
    with db.transaction() as conn:
        conn.execute("UPDATE im_channels SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), channel_id, ctx.tenant_id))
    im_runtime.notify_channel(channel_id)
    return ok(_im_channel_response(channel_id, ctx.tenant_id))


@router.get("/user/favorites")
def list_favorites(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all("SELECT * FROM user_resource_favorites WHERE user_id=? ORDER BY created_at DESC", (ctx.user_id,))
    return ok(rows)


@router.post("/user/favorites")
async def add_favorite(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_resource_favorites(user_id, tenant_id, resource_type, resource_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (ctx.user_id, ctx.tenant_id, str(data.get("type") or data.get("resource_type") or ""), str(data.get("id") or data.get("resource_id") or ""), db.utcnow()),
        )
    return ok(message="Favorite added")


@router.delete("/user/favorites/{resource_type}/{resource_id}")
def remove_favorite(resource_type: str, resource_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    with db.transaction() as conn:
        conn.execute("DELETE FROM user_resource_favorites WHERE user_id=? AND resource_type=? AND resource_id=?", (ctx.user_id, resource_type, resource_id))
    return ok(message="Favorite removed")


@router.get("/me/env-vars")
def list_env_vars(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all("SELECT * FROM tenant_user_env_vars WHERE principal_type='user' AND principal_id=? AND tenant_id=?", (ctx.user_id, ctx.tenant_id))
    return ok(rows)


@router.put("/me/env-vars/skill")
async def set_skill_env(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    if not str(data.get("name") or "").strip():
        raise validation("Environment variable name is required")
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tenant_user_env_vars(id, tenant_id, principal_type, principal_id, sandbox_config_id, skill_id, name, value, created_at, updated_at)
            VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?)
            """,
            (db.new_id(), ctx.tenant_id, ctx.user_id, str(data.get("sandbox_config_id") or ""), str(data.get("skill_id") or ""), str(data.get("name") or ""), str(data.get("value") or ""), db.utcnow(), db.utcnow()),
        )
    return {"success": True}


@router.delete("/me/env-vars/skill")
async def delete_skill_env(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    with db.transaction() as conn:
        conn.execute("DELETE FROM tenant_user_env_vars WHERE tenant_id=? AND principal_type='user' AND principal_id=? AND skill_id=? AND name=?", (ctx.tenant_id, ctx.user_id, str(data.get("skill_id") or ""), str(data.get("name") or "")))
    return {"success": True}


@router.put("/me/env-vars/sandbox")
async def set_sandbox_env(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    if not str(data.get("name") or "").strip():
        raise validation("Environment variable name is required")
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO tenant_user_env_vars(id, tenant_id, principal_type, principal_id, sandbox_config_id, name, value, created_at, updated_at)
            VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?)
            """,
            (db.new_id(), ctx.tenant_id, ctx.user_id, str(data.get("sandbox_config_id") or ""), str(data.get("name") or ""), str(data.get("value") or ""), db.utcnow(), db.utcnow()),
        )
    return {"success": True}


@router.delete("/me/env-vars/sandbox")
async def delete_sandbox_env(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    with db.transaction() as conn:
        conn.execute("DELETE FROM tenant_user_env_vars WHERE tenant_id=? AND principal_type='user' AND principal_id=? AND sandbox_config_id=? AND name=?", (ctx.tenant_id, ctx.user_id, str(data.get("sandbox_config_id") or ""), str(data.get("name") or "")))
    return {"success": True}


@router.get("/skills")
def list_skills(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    clauses = ["tenant_id=?", "enabled=1", "deleted_at IS NULL"]
    params: list[Any] = [ctx.tenant_id]
    config_id = str(request.query_params.get("sandbox_config_id") or "").strip()
    if config_id:
        clauses.append("sandbox_config_id=?")
        params.append(config_id)
    rows = db.fetch_all(
        "SELECT * FROM tenant_skills WHERE " + " AND ".join(clauses) + " ORDER BY name",
        params,
    )
    return ok(rows)

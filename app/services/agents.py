"""Agent configuration resolution shared by chat, IM and embed channels."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .. import db
from ..utils import loads


_BUILTIN_AGENTS = {
    item["id"]: item
    for item in json.loads(Path(__file__).with_name("builtin_agents.json").read_text())
}
_BUILTIN_AGENT_I18N = json.loads(
    Path(__file__).with_name("builtin_agent_i18n.json").read_text()
)
_AGENT_TYPE_PRESETS = json.loads(
    Path(__file__).with_name("agent_type_presets.json").read_text()
)

# These mirror the user-facing order in Go's builtinAgentIDsOrdered. The wiki
# fixer and skill installer are internal agents and are only returned by ID.
USER_BUILTIN_AGENT_IDS = (
    "builtin-quick-answer",
    "builtin-smart-reasoning",
    "builtin-wiki-researcher",
    "builtin-data-analyst",
)


def _builtin_label(agent_id: str, locale: str) -> dict[str, Any]:
    labels = _BUILTIN_AGENT_I18N.get(agent_id) or {}
    if not labels:
        return {}
    if locale in labels:
        return labels[locale]
    if "-" in locale or "_" in locale:
        language = locale.replace("_", "-").split("-", 1)[0]
        if language in labels:
            return labels[language]
        for key, value in labels.items():
            if key.startswith(language):
                return value
    return labels.get("default") or next(iter(labels.values()))


def builtin_agent(
    agent_id: str, tenant_id: int = 0, locale: str = "zh-CN"
) -> dict[str, Any] | None:
    item = _BUILTIN_AGENTS.get(str(agent_id))
    if item is None:
        return None
    result = deepcopy(item)
    result["tenant_id"] = int(tenant_id)
    label = _builtin_label(str(agent_id), locale)
    for key in ("name", "description"):
        if label.get(key):
            result[key] = label[key]
    return result


def builtin_agents(tenant_id: int = 0, locale: str = "zh-CN") -> list[dict[str, Any]]:
    return [
        builtin_agent(agent_id, tenant_id, locale)
        for agent_id in USER_BUILTIN_AGENT_IDS
    ]


def _go_json_value(value: Any) -> bool:
    if value is None or value is False or value == "" or value == 0 or value == []:
        return False
    return True


def agent_type_presets(locale: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source in _AGENT_TYPE_PRESETS:
        item = deepcopy(source)
        labels = item.get("i18n") or {}
        resolved: dict[str, Any] = {}
        if "default" in labels:
            resolved["default"] = labels["default"]
        if locale and locale in labels:
            resolved[locale] = labels[locale]
        item["i18n"] = resolved or labels
        if isinstance(item.get("config"), dict):
            item["config"] = {
                key: value
                for key, value in item["config"].items()
                if _go_json_value(value)
            }
        if isinstance(item.get("kb_filter"), dict):
            item["kb_filter"] = {
                key: value
                for key, value in item["kb_filter"].items()
                if _go_json_value(value)
            }
        result.append(item)
    return result


def agent_config(tenant_id: int, agent_id: str | None) -> dict[str, Any]:
    if not agent_id:
        return {}
    builtin = builtin_agent(str(agent_id), tenant_id)
    if builtin:
        return dict(builtin.get("config") or {})
    row = db.fetch_one(
        "SELECT config FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (agent_id, tenant_id),
    )
    if not row:
        return {}
    config = loads(row.get("config"), {})
    return config if isinstance(config, dict) else {}


def _string_ids(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("id") or value.get("knowledge_base_id")
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _starter_config(config: dict[str, Any]) -> dict[str, Any]:
    suggestions = config.get("question_suggestions")
    if not isinstance(suggestions, dict):
        return {"enabled": True, "mode": "hybrid", "items": [], "count": 6}
    starters = suggestions.get("starters")
    if not isinstance(starters, dict):
        starters = {}
    return {
        "enabled": bool(starters.get("enabled", False)),
        "mode": str(starters.get("mode") or "hybrid"),
        "items": [
            str(item).strip()
            for item in starters.get("items") or []
            if str(item).strip()
        ],
        "count": int(starters.get("count") or 6),
    }


def _suggestion_kb_ids(
    tenant_id: int,
    config: dict[str, Any],
    requested: list[str],
    allowed: list[str] | None = None,
) -> list[str]:
    if requested:
        marks = ",".join("?" for _ in requested)
        rows = db.fetch_all(
            f"SELECT id FROM knowledge_bases WHERE tenant_id=? AND deleted_at IS NULL "
            f"AND id IN ({marks})",
            (tenant_id, *requested),
        )
        existing = {str(row["id"]) for row in rows}
        result = [value for value in requested if value in existing]
    else:
        mode = str(config.get("kb_selection_mode") or "").strip().lower()
        if mode == "none":
            result = []
        elif mode == "selected":
            result = _string_ids(config.get("knowledge_bases"))
        else:
            rows = db.fetch_all(
                "SELECT id FROM knowledge_bases WHERE tenant_id=? AND deleted_at IS NULL",
                (tenant_id,),
            )
            result = [str(row["id"]) for row in rows]
    if allowed is not None:
        keep = set(allowed)
        result = [value for value in result if value in keep]
    return result


def _resolve_suggestion_tag_scopes(
    tenant_id: int, tag_scopes: list[dict[str, Any]]
) -> dict[str, list[str]]:
    by_kb: dict[str, list[str]] = {}
    for scope in tag_scopes:
        kb_id = str(scope.get("knowledge_base_id") or "").strip()
        if not kb_id:
            continue
        requested = by_kb.setdefault(kb_id, [])
        for value in scope.get("tag_ids") or []:
            tag_id = str(value or "").strip()
            if tag_id and tag_id not in requested:
                requested.append(tag_id)

    result = {
        "knowledge_base_ids": [],
        "knowledge_ids": [],
        "tag_ids": [],
        "tag_seq_ids": [],
    }
    for kb_id, requested in by_kb.items():
        marks = ",".join("?" for _ in requested)
        rows = db.fetch_all(
            f"SELECT id, seq_id FROM knowledge_tags WHERE tenant_id=? "
            f"AND knowledge_base_id=? AND deleted_at IS NULL AND id IN ({marks})",
            (tenant_id, kb_id, *requested),
        )
        valid = {str(row["id"]): row.get("seq_id") for row in rows}
        tag_ids = [tag_id for tag_id in requested if tag_id in valid]
        if not tag_ids:
            continue
        result["knowledge_base_ids"].append(kb_id)
        for tag_id in tag_ids:
            if tag_id not in result["tag_ids"]:
                result["tag_ids"].append(tag_id)
            try:
                seq_id = int(valid[tag_id])
            except (TypeError, ValueError):
                continue
            if seq_id not in result["tag_seq_ids"]:
                result["tag_seq_ids"].append(seq_id)
        marks = ",".join("?" for _ in tag_ids)
        knowledge_rows = db.fetch_all(
            f"SELECT DISTINCT k.id FROM knowledges k "
            f"JOIN knowledge_tag_relations r ON r.knowledge_id=k.id "
            f"WHERE k.tenant_id=? AND k.knowledge_base_id=? AND k.deleted_at IS NULL "
            f"AND r.tag_id IN ({marks})",
            (tenant_id, kb_id, *tag_ids),
        )
        for row in knowledge_rows:
            knowledge_id = str(row["id"])
            if knowledge_id not in result["knowledge_ids"]:
                result["knowledge_ids"].append(knowledge_id)
    return result


def _first_generated_question(metadata: Any) -> str:
    value = loads(metadata, {})
    if not isinstance(value, dict):
        return ""
    questions = value.get("generated_questions")
    if not isinstance(questions, list):
        return ""
    for item in questions:
        if isinstance(item, dict):
            item = item.get("question") or item.get("content") or ""
        text = str(item or "").strip()
        if text:
            return text
    return ""


def agent_suggested_questions(
    tenant_id: int,
    config: dict[str, Any],
    requested_kb_ids: list[str],
    knowledge_ids: list[str],
    limit: int,
    locale: str,
    tag_scopes: list[dict[str, Any]] | None = None,
    allowed_kb_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    starter = _starter_config(config)
    if not starter["enabled"]:
        return []
    if limit <= 0:
        limit = starter["count"]
    limit = min(30, limit)
    if limit <= 0:
        return []

    curated = [
        {"question": question, "source": "agent_config"}
        for question in starter["items"]
    ]
    if starter["mode"] == "curated":
        return curated[:limit]

    tag_scopes = tag_scopes or []
    requested_tag_ids = [
        str(tag_id).strip()
        for scope in tag_scopes
        for tag_id in scope.get("tag_ids") or []
        if str(tag_id).strip()
    ]
    resolved_tags = _resolve_suggestion_tag_scopes(tenant_id, tag_scopes)
    if requested_tag_ids:
        if not knowledge_ids and not resolved_tags["tag_ids"]:
            return curated[:limit]
        for knowledge_id in resolved_tags["knowledge_ids"]:
            if knowledge_id not in knowledge_ids:
                knowledge_ids.append(knowledge_id)

    if requested_tag_ids:
        kb_ids = (
            _suggestion_kb_ids(
                tenant_id, config, requested_kb_ids, allowed_kb_ids
            )
            if requested_kb_ids
            else []
        )
        kb_ids = [
            kb_id
            for kb_id in kb_ids
            if kb_id not in resolved_tags["knowledge_base_ids"]
        ]
    else:
        kb_ids = _suggestion_kb_ids(
            tenant_id, config, requested_kb_ids, allowed_kb_ids
        )

    if not kb_ids and not knowledge_ids and not resolved_tags["tag_ids"]:
        knowledge: list[dict[str, Any]] = []
    else:
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        if not knowledge_ids or resolved_tags["tag_seq_ids"]:
            faq_clauses: list[str] = []
            faq_params: list[Any] = [tenant_id]
            if kb_ids:
                faq_clauses.append(
                    "knowledge_base_id IN (" + ",".join("?" for _ in kb_ids) + ")"
                )
                faq_params.extend(kb_ids)
            if resolved_tags["tag_seq_ids"]:
                faq_clauses.append(
                    "tag_id IN ("
                    + ",".join("?" for _ in resolved_tags["tag_seq_ids"])
                    + ")"
                )
                faq_params.extend(resolved_tags["tag_seq_ids"])
            faq_rows = db.fetch_all(
                f"SELECT knowledge_base_id, standard_question FROM py_faq_entries "
                f"WHERE tenant_id=? AND deleted_at IS NULL AND is_enabled=1 "
                f"{'AND (' + ' OR '.join(faq_clauses) + ')' if faq_clauses else ''} "
                f"ORDER BY is_recommended DESC, id DESC LIMIT ?",
                (*faq_params, max(20, limit * 5)),
            )
            for row in faq_rows:
                question = str(row.get("standard_question") or "").strip()
                key = question.casefold()
                if question and key not in seen:
                    seen.add(key)
                    found.append({
                        "question": question,
                        "source": "faq",
                        "knowledge_base_id": row.get("knowledge_base_id") or "",
                    })

        if kb_ids or knowledge_ids:
            clauses = ["tenant_id=?", "deleted_at IS NULL", "is_enabled=1"]
            params: list[Any] = [tenant_id]
            if kb_ids:
                clauses.append(
                    "knowledge_base_id IN (" + ",".join("?" for _ in kb_ids) + ")"
                )
                params.extend(kb_ids)
            if knowledge_ids:
                clauses.append(
                    "knowledge_id IN (" + ",".join("?" for _ in knowledge_ids) + ")"
                )
                params.extend(knowledge_ids)
            where = " AND ".join(clauses)
            chunk_rows = db.fetch_all(
                f"SELECT knowledge_id, knowledge_base_id, metadata FROM chunks "
                f"WHERE {where} AND metadata LIKE '%generated_questions%' "
                f"ORDER BY created_at DESC LIMIT ?",
                (*params, max(20, limit * 5)),
            )
            for row in chunk_rows:
                question = _first_generated_question(row.get("metadata"))
                key = question.casefold()
                if question and key not in seen:
                    seen.add(key)
                    found.append({
                        "question": question,
                        "source": "document",
                        "knowledge_base_id": row.get("knowledge_base_id") or "",
                        "knowledge_id": row.get("knowledge_id") or "",
                    })

        if str(config.get("agent_mode") or "") != "quick-answer" and requested_kb_ids:
            marks = ",".join("?" for _ in requested_kb_ids)
            wiki_rows = db.fetch_all(
                f"SELECT knowledge_base_id, title, page_type FROM wiki_pages "
                f"WHERE tenant_id=? AND deleted_at IS NULL AND knowledge_base_id IN ({marks}) "
                f"ORDER BY updated_at DESC LIMIT ?",
                (tenant_id, *requested_kb_ids, max(20, limit * 5)),
            )
            for row in wiki_rows:
                title = str(row.get("title") or "").strip()
                if not title:
                    continue
                page_type = str(row.get("page_type") or "")
                if page_type == "concept":
                    question = (
                        f"What is {title}?"
                        if locale.lower().startswith("en")
                        else f"什么是{title}？"
                    )
                elif page_type in {"entity", "summary"}:
                    question = (
                        f"Tell me about {title}"
                        if locale.lower().startswith("en")
                        else f"介绍一下{title}"
                    )
                else:
                    question = title
                key = question.casefold()
                if key not in seen:
                    seen.add(key)
                    found.append({
                        "question": question,
                        "source": "wiki",
                        "knowledge_base_id": row.get("knowledge_base_id") or "",
                    })
        knowledge = found

    if starter["mode"] != "hybrid":
        return knowledge[:limit]
    knowledge_slots = (limit + 1) // 3 if limit > 1 else 0
    curated_slots = limit - knowledge_slots
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append(items: list[dict[str, Any]], maximum: int) -> None:
        added = 0
        for item in items:
            if len(result) == limit or (maximum >= 0 and added == maximum):
                return
            key = str(item.get("question") or "").strip().casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(item)
            added += 1

    append(curated, curated_slots)
    append(knowledge, knowledge_slots)
    append(curated, -1)
    append(knowledge, -1)
    return result


def resolve_config(session: dict[str, Any], tenant_id: int) -> dict[str, Any]:
    session_config = loads(session.get("agent_config"), {})
    if not isinstance(session_config, dict):
        session_config = {}
    resolved = agent_config(tenant_id, str(session.get("agent_id") or ""))
    resolved.update(session_config)
    return resolved


def selected_mcp_service_ids(
    tenant_id: int,
    config: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> list[str]:
    payload = payload or {}
    requested = payload.get("mcp_service_ids") or payload.get("mcp_services") or []
    if isinstance(requested, str):
        requested = [requested]
    requested_ids = [str(value) for value in requested if value]

    mode = str(config.get("mcp_selection_mode") or "").strip().lower()
    raw_services = config.get("mcp_services") or config.get("mcp_service_ids") or []
    if isinstance(raw_services, str):
        raw_services = [raw_services]
    configured_ids = [
        str(value.get("id") if isinstance(value, dict) else value)
        for value in raw_services
        if value
    ]
    if mode == "none":
        return []
    if mode == "selected":
        ids = configured_ids
    else:
        rows = db.fetch_all(
            "SELECT id FROM mcp_services WHERE tenant_id=? AND deleted_at IS NULL AND enabled=1 ORDER BY created_at",
            (tenant_id,),
        )
        ids = [str(row["id"]) for row in rows]
    for value in requested_ids:
        if value not in ids:
            ids.append(value)
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    enabled = {
        str(row["id"])
        for row in db.fetch_all(
            f"SELECT id FROM mcp_services WHERE tenant_id=? AND deleted_at IS NULL AND enabled=1 AND id IN ({marks})",
            (tenant_id, *ids),
        )
    }
    return [value for value in ids if value in enabled]

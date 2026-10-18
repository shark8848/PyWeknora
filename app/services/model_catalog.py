"""Persisted model-catalog overlay and effective provider state."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .. import db
from ..errors import conflict, validation
from ..utils import dumps, loads
from . import model_runtime


def _builtin() -> list[dict[str, Any]]:
    providers: list[dict[str, Any]] = []
    for item in model_runtime.PROVIDERS:
        provider_id = str(item.get("value") or "")
        providers.append(
            {
                "id": provider_id,
                "name": str(item.get("label") or provider_id),
                "api": str(item.get("api") or "openai-completions"),
                "model_types": list(item.get("modelTypes") or []),
                "settings": {
                    "names": {"zh-CN": str(item.get("label") or provider_id)},
                    "descriptions": {"zh-CN": str(item.get("description") or "")},
                    "default_urls": dict(item.get("defaultUrls") or {}),
                    "order": int(item.get("order") or 0),
                },
                "models": [],
            }
        )
    return providers


def _baseline(providers: list[dict[str, Any]]) -> str:
    raw = json.dumps(providers, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _deployment() -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT * FROM models WHERE deleted_at IS NULL ORDER BY tenant_id, type, name")
    providers: dict[str, dict[str, Any]] = {}
    for row in rows:
        parameters = loads(row.get("parameters"), {})
        if not isinstance(parameters, dict):
            parameters = {}
        provider_id = str(parameters.get("provider") or row.get("source") or "openai-compatible")
        entry = providers.setdefault(
            provider_id,
            {
                "id": provider_id,
                "name": provider_id,
                "api": str((parameters.get("spec") or {}).get("api") or "openai-completions") if isinstance(parameters.get("spec"), dict) else "openai-completions",
                "model_types": [],
                "settings": {"names": {"zh-CN": provider_id}},
                "models": [],
            },
        )
        model_type = str(row.get("type") or "KnowledgeQA")
        if model_type not in entry["model_types"]:
            entry["model_types"].append(model_type)
        entry["models"].append(
            {
                "id": str(row.get("name") or ""),
                "type": model_type,
                "name": str(row.get("display_name") or row.get("name") or ""),
                "context_window": int(parameters.get("context_window") or 0),
                "max_output_tokens": int(parameters.get("max_output_tokens") or 0),
                "reasoning": bool(parameters.get("reasoning")),
                "deprecated": str(row.get("status") or "") != "active",
            }
        )
    return list(providers.values())


def _merge(base: Any, patch: Any) -> Any:
    if isinstance(base, dict) and isinstance(patch, dict):
        result = copy.deepcopy(base)
        for key, value in patch.items():
            result[key] = _merge(result.get(key), value)
        return result
    if isinstance(patch, list):
        return copy.deepcopy(patch)
    return copy.deepcopy(patch)


def _merge_provider_model(entry: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = _merge(entry, patch)
    model_id = str(entry.get("id") or "").lower()
    model_type = str(entry.get("type") or "KnowledgeQA")
    for key, override in (patch.get("model_overrides") or {}).items() if isinstance(patch.get("model_overrides"), dict) else []:
        if str(key).lower() == model_id and isinstance(override, dict):
            merged = _merge(merged, override)
    merged.pop("model_overrides", None)
    merged["type"] = str(merged.get("type") or model_type)
    return merged


def _apply_overlay(base: list[dict[str, Any]], overlay: dict[str, Any]) -> list[dict[str, Any]]:
    providers = copy.deepcopy(base)
    by_id = {str(provider.get("id") or ""): provider for provider in providers}
    patches = overlay.get("providers") if isinstance(overlay.get("providers"), dict) else {}
    for provider_id, patch in patches.items():
        if not isinstance(patch, dict):
            continue
        provider = by_id.get(str(provider_id))
        if provider is None:
            provider = {
                "id": str(provider_id),
                "name": str(provider_id),
                "api": "openai-completions",
                "model_types": [],
                "settings": {},
                "models": [],
            }
            providers.append(provider)
            by_id[str(provider_id)] = provider
        provider_patch = {key: value for key, value in patch.items() if key not in {"models", "model_overrides"}}
        provider.update(_merge(provider, provider_patch))
        existing = {f"{item.get('type') or 'KnowledgeQA'}:{str(item.get('id') or '').lower()}": item for item in provider.get("models", [])}
        for item in patch.get("models") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            key = f"{item.get('type') or 'KnowledgeQA'}:{str(item['id']).lower()}"
            if key in existing:
                existing[key].update(_merge_provider_model(existing[key], item))
            else:
                new_item = _merge_provider_model({"id": item["id"], "type": item.get("type") or "KnowledgeQA"}, item)
                provider["models"].append(new_item)
                existing[key] = new_item
        overrides = patch.get("model_overrides") if isinstance(patch.get("model_overrides"), dict) else {}
        for model_id, fields in overrides.items():
            if not isinstance(fields, dict):
                continue
            for entry in provider.get("models", []):
                if str(entry.get("id") or "").lower() == str(model_id).lower():
                    entry.update(_merge(entry, fields))
    return providers


def _row() -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM py_model_catalog WHERE id=1")
    if row:
        return row
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_model_catalog(id, version, baseline, overlay, history, updated_by, updated_at) VALUES (1, 1, '', '{\"providers\":{}}', '[]', '', ?)",
            (now,),
        )
    return db.fetch_one("SELECT * FROM py_model_catalog WHERE id=1") or {
        "version": 1,
        "baseline": "",
        "overlay": "{\"providers\":{}}",
        "history": "[]",
        "updated_at": now,
    }


def state() -> dict[str, Any]:
    row = _row()
    builtin = _builtin()
    baseline = _baseline(builtin)
    overlay = loads(row.get("overlay"), {})
    if not isinstance(overlay, dict):
        overlay = {}
    overlay.setdefault("providers", {})
    deployment = _deployment()
    merged_base = _apply_overlay(builtin, {"providers": {item["id"]: item for item in deployment}})
    effective = _apply_overlay(merged_base, overlay)
    history = loads(row.get("history"), [])
    return {
        "version": int(row.get("version") or 1),
        "baseline": baseline,
        "overlay": overlay,
        "history": history if isinstance(history, list) else [],
        "builtin": builtin,
        "deployment": deployment,
        "effective": effective,
        "applied_version": int(row.get("version") or 1),
    }


def _validate_update(data: dict[str, Any], current: dict[str, Any]) -> tuple[int, str, dict[str, Any]]:
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError) as exc:
        raise validation("invalid model catalog version") from exc
    if version != current["version"]:
        raise conflict("Model catalog version conflict; reload and retry")
    baseline = str(data.get("baseline") or "")
    if baseline and baseline != current["baseline"]:
        raise conflict("Model catalog baseline changed; reload and retry")
    overlay = data.get("overlay")
    if not isinstance(overlay, dict) or not isinstance(overlay.get("providers"), dict):
        raise validation("overlay.providers must be an object")
    raw = json.dumps(overlay, ensure_ascii=False)
    if len(raw.encode()) > 1024 * 1024:
        raise validation("model catalog overlay is too large")
    return version, baseline or current["baseline"], overlay


def preview(data: dict[str, Any]) -> dict[str, Any]:
    current = state()
    _, _, overlay = _validate_update(data, current)
    candidate = _apply_overlay(current["builtin"], {"providers": {item["id"]: item for item in current["deployment"]}})
    effective = _apply_overlay(candidate, overlay)
    return {**current, "overlay": overlay, "effective": effective, "history": []}


def publish(data: dict[str, Any], user_id: str) -> dict[str, Any]:
    current = state()
    version, baseline, overlay = _validate_update(data, current)
    now = db.utcnow()
    history = list(current["history"])
    history.append({"version": version, "overlay": overlay, "updated_by": user_id, "updated_at": now})
    history = history[-50:]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE py_model_catalog SET version=?, baseline=?, overlay=?, history=?, updated_by=?, updated_at=? WHERE id=1",
            (version + 1, baseline, dumps(overlay), dumps(history), user_id, now),
        )
    return state()

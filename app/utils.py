"""Small shared helpers for API responses and row projections."""

from __future__ import annotations

import json
import math
import os
from typing import Any


def ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"success": True}
    if data is not None:
        result["data"] = data
    result.update(extra)
    return result


def page(items: list[Any], total: int, page_no: int, page_size: int) -> dict[str, Any]:
    return {
        "success": True,
        "data": items,
        "items": items,
        "total": total,
        "page": page_no,
        "page_size": page_size,
        "total_pages": math.ceil(total / page_size) if page_size else 0,
    }


def loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list, bool, int, float)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def resolve_locale(accept_language: str = "") -> str:
    env_locale = os.getenv("WEKNORA_LANGUAGE", "").strip()
    header_locale = accept_language.split(",", 1)[0].split(";", 1)[0].strip()
    return env_locale or header_locale or "zh-CN"


def truthy(value: Any) -> bool:
    return value in (True, 1, "1", "true", "True", "yes", "on")


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pagination(page_no: Any = 1, page_size: Any = 20, maximum: int = 200) -> tuple[int, int, int]:
    p = max(1, as_int(page_no, 1))
    size = min(maximum, max(1, as_int(page_size, 20)))
    return p, size, (p - 1) * size

"""Model configuration resolution and upstream protocol calls.

The Web UI stores model endpoints in the ``models`` table.  Runtime callers
must use those rows rather than reading process-wide environment variables so
that a workspace can switch models without restarting the service.
"""

from __future__ import annotations

import base64
import json
import math
import mimetypes
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import config, db
from ..errors import validation
from ..utils import loads
from . import image_prep, model_limiter


MODEL_TYPES = ("KnowledgeQA", "Embedding", "Rerank", "VLLM", "ASR")
FRONTEND_MODEL_TYPES = {
    "chat": "KnowledgeQA",
    "qa": "KnowledgeQA",
    "knowledgeqa": "KnowledgeQA",
    "embedding": "Embedding",
    "rerank": "Rerank",
    "vllm": "VLLM",
    "asr": "ASR",
}


class UpstreamHTTPError(RuntimeError):
    """An upstream model endpoint returned a non-success HTTP status."""

    def __init__(self, status: int, body: str) -> None:
        self.status = int(status)
        self.body = str(body or "")
        super().__init__(f"upstream returned HTTP {self.status}: {self.body[:2000]}")

# The provider table is intentionally data-only: the model editor renders
# every vendor-specific field from this response.
PROVIDERS: list[dict[str, Any]] = json.loads(
    Path(__file__).with_name("model_providers.json").read_text()
)
PROVIDER_ALIASES = {"openai-compatible": "generic"}
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_NO_SAMPLING = {
    "claude-fable-5",
    "claude-fable-5-1",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-sonnet-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
}
ANTHROPIC_THINKING_BUDGETS = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 32768,
    "max": 63999,
}
ANTHROPIC_REASONING_LEVELS = ("off", "auto", "minimal", "low", "medium", "high", "xhigh", "max")
ANTHROPIC_GRADED_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")


def provider_definition(value: str | None) -> dict[str, Any] | None:
    key = str(value or "").lower()
    key = PROVIDER_ALIASES.get(key, key)
    return next(
        (item for item in PROVIDERS if str(item.get("value") or "").lower() == key),
        None,
    )


def backend_model_type(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if text in MODEL_TYPES:
        return text
    return FRONTEND_MODEL_TYPES.get(text.lower(), "")


def providers_for(model_type: str | None = None) -> list[dict[str, Any]]:
    wanted = backend_model_type(model_type)
    frontend = {
        "KnowledgeQA": "chat",
        "Embedding": "embedding",
        "Rerank": "rerank",
        "VLLM": "vllm",
        "ASR": "asr",
    }.get(wanted, "")
    result = []
    for provider in PROVIDERS:
        if frontend and frontend not in provider.get("modelTypes", []):
            continue
        item = dict(provider)
        item.setdefault("thinking", {"format": "none", "levels": []})
        result.append(item)
    return result


def _frontend_model_type(model_type: str) -> str:
    return {
        "KnowledgeQA": "chat",
        "Embedding": "embedding",
        "Rerank": "rerank",
        "VLLM": "vllm",
        "ASR": "asr",
    }.get(model_type, "chat")


def _catalog_model(
    provider: str, model: str, model_type: str
) -> tuple[dict[str, Any], dict[str, Any]] | tuple[None, None]:
    definition = provider_definition(provider)
    if not definition or not model:
        return None, None
    wanted = _frontend_model_type(model_type)
    lowered = model.lower()
    for item in definition.get("models") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "chat").lower() != wanted:
            continue
        if str(item.get("id") or "").lower() == lowered:
            return definition, item
    return None, None


def _parameters(raw: Any) -> dict[str, Any]:
    value = loads(raw, {})
    return value if isinstance(value, dict) else {}


def _image_mime(data: bytes) -> str:
    return image_prep.detect_image_mime(data)


def image_mime(data: bytes) -> str:
    return _image_mime(data)


def _embedding_api(provider: str, name: str, spec: dict[str, Any]) -> str:
    compat = spec.get("compat") if isinstance(spec.get("compat"), dict) else {}
    configured = str(compat.get("api") or spec.get("api") or "").lower()
    if configured in {"ark-embeddings", "dashscope-embeddings", "google-embeddings", "openai-embeddings"}:
        return configured
    lowered_provider = provider.lower()
    lowered_name = name.lower()
    inputs = spec.get("input")
    accepts_image = isinstance(inputs, list) and "image" in inputs
    if lowered_provider in {"generic", "openai-compatible", "ollama"}:
        return "openai-embeddings"
    if "doubao-embedding-vision" in lowered_name or (
        lowered_provider in {"volcengine", "ark"} and accepts_image
    ):
        return "ark-embeddings"
    if (
        "embedding-vision" in lowered_name
        or "multimodal-embedding" in lowered_name
        or "vl-embedding" in lowered_name
        or (lowered_provider in {"aliyun", "dashscope"} and accepts_image)
    ):
        return "dashscope-embeddings"
    if lowered_provider in {"gemini", "google"} or "gemini-embedding" in lowered_name:
        return "google-embeddings"
    return "openai-embeddings"


def _embedding_messages_profile(
    provider: str, name: str, spec: dict[str, Any]
) -> dict[str, Any]:
    compat = spec.get("compat") if isinstance(spec.get("compat"), dict) else {}

    def setting(field: str, default: Any = None) -> Any:
        return compat.get(field, spec.get(field, default))

    inferred_qwen = (
        provider.lower() in {"generic", "openai-compatible", "ollama"}
        and "qwen3-vl-embedding" in name.lower()
    )
    try:
        max_images = int(setting("max_image_batch_size") or 0)
    except (TypeError, ValueError):
        max_images = 0
    if inferred_qwen and max_images <= 0:
        max_images = 5
    extra_body = setting("extra_body")
    return {
        "text_as_messages": bool(setting("text_as_messages", inferred_qwen)),
        "batch_messages": bool(setting("batch_messages", inferred_qwen)),
        "max_image_batch_size": max_images,
        "send_encoding_format": bool(setting("send_encoding_format", inferred_qwen)),
        "extra_body": dict(extra_body) if isinstance(extra_body, dict) else {},
    }


def _embedding_image_profile(provider: str, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    compat = spec.get("compat") if isinstance(spec.get("compat"), dict) else {}

    def setting(field: str) -> Any:
        return compat.get(field, spec.get(field))

    lowered_provider = provider.lower()
    lowered_name = name.lower()
    known = any(
        marker in lowered_name
        for marker in (
            "doubao-embedding-vision",
            "tongyi-embedding-vision",
            "multimodal-embedding",
            "qwen3-vl-embedding",
            "qwen2.5-vl-embedding",
            "gemini-embedding-2",
            "jina-embeddings-v5-omni",
            "jina-embeddings-v4",
        )
    )
    inputs = spec.get("input")
    if isinstance(inputs, list):
        if "image" not in inputs:
            return None
    elif not known:
        return None

    api = _embedding_api(provider, name, spec)

    image_format = str(setting("image_format") or "").lower()
    image_field = str(setting("image_field") or "")
    if api == "openai-embeddings":
        if lowered_provider == "jina":
            image_format = "object"
            image_field = image_field or "image"
        elif image_format not in {"object", "messages"}:
            image_format = "messages" if lowered_provider in {"generic", "openai-compatible", "ollama"} else ""
        if image_format == "object" and not image_field:
            image_field = "image"
        if not image_format:
            return None
    else:
        image_format = ""

    mime_types = setting("image_mime_types")
    if not isinstance(mime_types, list):
        mime_types = []
    max_bytes = setting("max_image_bytes")
    try:
        max_bytes = int(max_bytes or 0)
    except (TypeError, ValueError):
        max_bytes = 0
    if not max_bytes and lowered_provider == "jina":
        max_bytes = 5_000_000
    if not mime_types and lowered_provider == "gemini" and "gemini-embedding-2" in lowered_name:
        mime_types = ["image/png", "image/jpeg"]
    messages = _embedding_messages_profile(provider, name, spec)
    return {
        "api": api,
        "image_format": image_format,
        "image_field": image_field,
        "image_prompt": str(setting("image_prompt") or ""),
        "truncate": lowered_provider == "jina" or bool(setting("truncate")),
        "mime_types": [str(item) for item in mime_types],
        "max_bytes": max_bytes,
        **messages,
    }


def _rerank_image_profile(provider: str, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    compat = spec.get("compat") if isinstance(spec.get("compat"), dict) else {}

    def setting(field: str) -> Any:
        return compat.get(field, spec.get(field))

    lowered_provider = provider.lower()
    lowered_name = name.lower()
    known = any(
        marker in lowered_name
        for marker in ("jina-reranker-m0", "nemotron-rerank-vl")
    )
    inputs = spec.get("input")
    if isinstance(inputs, list):
        if "image" not in inputs:
            return None
    elif not known:
        return None

    image_field = str(setting("image_field") or "")
    if lowered_provider == "nvidia" or "nemotron-rerank-vl" in lowered_name:
        api = "nim"
    elif lowered_provider == "jina" or image_field:
        api = "cohere"
        image_field = image_field or "image"
    else:
        return None

    mime_types = setting("image_mime_types")
    if not isinstance(mime_types, list):
        mime_types = []
    if not mime_types and api == "nim":
        mime_types = ["image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"]
    max_bytes = setting("max_image_bytes")
    try:
        max_bytes = int(max_bytes or 0)
    except (TypeError, ValueError):
        max_bytes = 0
    return {
        "api": api,
        "image_field": image_field,
        "mime_types": [str(item) for item in mime_types],
        "max_bytes": max_bytes,
    }


def visible_model(tenant_id: int, model_id: str) -> dict[str, Any] | None:
    if not model_id:
        return None
    row = db.fetch_one(
        "SELECT * FROM models WHERE id=? AND (tenant_id=? OR is_builtin=1) AND deleted_at IS NULL",
        (model_id, tenant_id),
    )
    if not row:
        return None
    row["parameters"] = _parameters(row.get("parameters"))
    row["is_default"] = bool(row.get("is_default"))
    row["is_builtin"] = bool(row.get("is_builtin"))
    return row


def default_model(tenant_id: int, model_type: str) -> dict[str, Any] | None:
    row = db.fetch_one(
        """
        SELECT * FROM models
        WHERE (tenant_id=? OR is_builtin=1) AND type=? AND status='active' AND deleted_at IS NULL
        ORDER BY CASE WHEN tenant_id=? THEN 0 ELSE 1 END, is_default DESC, created_at ASC
        LIMIT 1
        """,
        (tenant_id, model_type, tenant_id),
    )
    if not row:
        return None
    row["parameters"] = _parameters(row.get("parameters"))
    row["is_default"] = bool(row.get("is_default"))
    row["is_builtin"] = bool(row.get("is_builtin"))
    return row


def _base_url(params: dict[str, Any], source: str, provider: str, model_type: str) -> str:
    explicit = str(params.get("base_url") or "").strip().rstrip("/")
    if explicit:
        return explicit
    lowered = provider.lower()
    frontend = {
        "KnowledgeQA": "chat",
        "Embedding": "embedding",
        "Rerank": "rerank",
        "VLLM": "vllm",
        "ASR": "asr",
    }.get(model_type, "chat")
    definition = provider_definition(lowered)
    if definition:
        return str((definition.get("defaultUrls") or {}).get(frontend) or "").rstrip("/")
    if source == "local" and model_type in {"KnowledgeQA", "Embedding", "VLLM"}:
        return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    return ""


def _secret(params: dict[str, Any], field: str) -> str:
    if params.get(field):
        return str(params[field])
    hidden = params.get("_credentials")
    if isinstance(hidden, dict) and hidden.get(field):
        return str(hidden[field])
    nested = params.get("credentials")
    if isinstance(nested, dict):
        value = nested.get(field)
        if isinstance(value, dict):
            value = value.get("value")
        if value:
            return str(value)
    return ""


def runtime_config(row: dict[str, Any] | None, model_type: str) -> dict[str, Any]:
    if row is None:
        fallback = _environment_runtime(model_type)
        if fallback:
            return fallback
        return {}
    params = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
    source = str(row.get("source") or "remote").lower()
    provider = str(params.get("provider") or row.get("source") or "openai-compatible").lower()
    base_url = _base_url(params, source, provider, model_type)
    api_key = _secret(params, "api_key") or _secret(params, "app_secret")
    headers = {
        str(key): str(value)
        for key, value in (params.get("custom_headers") or {}).items()
        if key and value is not None
    } if isinstance(params.get("custom_headers"), dict) else {}
    definition = provider_definition(provider)
    _, catalog_model = _catalog_model(provider, str(row.get("name") or ""), model_type)
    catalog_model = catalog_model or {}
    auth = str((definition or {}).get("auth") or "bearer")
    if api_key and "authorization" not in {key.lower() for key in headers}:
        if auth == "x-api-key":
            headers["x-api-key"] = api_key
        elif auth == "api-key":
            headers["api-key"] = api_key
        elif auth in {"x-goog-api-key", "google-api-key"}:
            headers["x-goog-api-key"] = api_key
        elif auth != "none":
            headers["Authorization"] = f"Bearer {api_key}"
    spec = params.get("spec") if isinstance(params.get("spec"), dict) else {}
    compat = spec.get("compat") if isinstance(spec.get("compat"), dict) else {}
    if not compat and isinstance(catalog_model.get("compat"), dict):
        compat = dict(catalog_model["compat"])
    thinking_levels = spec.get("thinking_levels") or catalog_model.get("thinking_levels")
    if not isinstance(thinking_levels, list):
        thinking = (definition or {}).get("thinking") if isinstance((definition or {}).get("thinking"), dict) else {}
        thinking_levels = thinking.get("levels") if isinstance(thinking.get("levels"), list) else []
    dimension = params.get("dimension") or params.get("dimensions") or spec.get("dimension") or spec.get("dimensions") or 0
    try:
        dimension = int(dimension or 0)
    except (TypeError, ValueError):
        dimension = 0
    return {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "display_name": str(row.get("display_name") or row.get("name") or ""),
        "type": model_type,
        "source": source,
        "provider": provider,
        "api": str((params.get("spec") or {}).get("api") or (definition or {}).get("api") or "openai-completions"),
        "base_url": base_url,
        "api_key": api_key,
        "headers": headers,
        "extra_config": params.get("extra_config") if isinstance(params.get("extra_config"), dict) else {},
        "spec": spec,
        "dimension": dimension,
        "context_window": int(params.get("context_window") or 0),
        "max_output_tokens": int(params.get("max_output_tokens") or 0),
        "max_concurrency": int(params.get("max_concurrency") or 0),
        "supports_vision": bool(params.get("supports_vision")) or model_type == "VLLM",
        "thinking": params.get("thinking") if isinstance(params.get("thinking"), bool) else None,
        "reasoning_effort": str(params.get("reasoning_effort") or ""),
        "thinking_budget_tokens": int(params.get("thinking_budget_tokens") or 0),
        "thinking_levels": [str(level) for level in thinking_levels],
        "compat": compat,
        "max_tokens": int(params.get("max_completion_tokens") or params.get("max_tokens") or 0),
    }


def _environment_runtime(model_type: str) -> dict[str, Any]:
    if model_type == "KnowledgeQA" and config.LLM_BASE_URL:
        return {
            "id": "env-llm",
            "name": config.LLM_MODEL,
            "display_name": config.LLM_MODEL,
            "type": model_type,
            "source": "remote",
            "provider": "openai-compatible",
            "api": "openai-completions",
            "base_url": config.LLM_BASE_URL,
            "api_key": config.LLM_API_KEY,
            "headers": {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {},
            "extra_config": {},
            "context_window": 0,
            "max_output_tokens": 0,
        }
    if model_type == "Embedding" and config.EMBEDDING_BASE_URL:
        return {
            "id": "env-embedding",
            "name": config.EMBEDDING_MODEL,
            "display_name": config.EMBEDDING_MODEL,
            "type": model_type,
            "source": "remote",
            "provider": "openai-compatible",
            "api": "openai-completions",
            "base_url": config.EMBEDDING_BASE_URL,
            "api_key": config.EMBEDDING_API_KEY,
            "headers": {"Authorization": f"Bearer {config.EMBEDDING_API_KEY}"} if config.EMBEDDING_API_KEY else {},
            "extra_config": {},
            "context_window": 0,
            "max_output_tokens": 0,
        }
    return {}


def chat_runtime(
    tenant_id: int,
    agent_config: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config_data = agent_config or {}
    body_data = payload or {}
    model_id = str(body_data.get("model_id") or config_data.get("model_id") or "").strip()
    row = visible_model(tenant_id, model_id) if model_id else default_model(tenant_id, "KnowledgeQA")
    runtime = runtime_config(row, "KnowledgeQA")
    for key in ("thinking", "reasoning_effort", "thinking_budget_tokens", "max_tokens"):
        if key in body_data:
            runtime[key] = body_data[key]
        elif key in config_data:
            runtime[key] = config_data[key]
    if "max_completion_tokens" in body_data:
        runtime["max_tokens"] = body_data["max_completion_tokens"]
    elif "max_completion_tokens" in config_data:
        runtime["max_tokens"] = config_data["max_completion_tokens"]
    return runtime


def embedding_runtime(tenant_id: int, knowledge_base_ids: list[str] | None = None) -> dict[str, Any]:
    model_id = ""
    if knowledge_base_ids:
        marks = ",".join("?" for _ in knowledge_base_ids)
        rows = db.fetch_all(
            f"SELECT embedding_model_id FROM knowledge_bases WHERE tenant_id=? AND id IN ({marks}) AND deleted_at IS NULL ORDER BY created_at",
            (tenant_id, *knowledge_base_ids),
        )
        model_id = next((str(row.get("embedding_model_id") or "") for row in rows if row.get("embedding_model_id")), "")
    row = visible_model(tenant_id, model_id) if model_id else default_model(tenant_id, "Embedding")
    return runtime_config(row, "Embedding")


def available(runtime: dict[str, Any] | None) -> bool:
    return bool(runtime and runtime.get("base_url"))


def request_url(runtime: dict[str, Any], path: str) -> str:
    base = str(runtime.get("base_url") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation("Model base URL must use http or https")
    if not path.startswith("/"):
        path = "/" + path
    suffixes = ("/chat/completions", "/embeddings", "/rerank", "/audio/transcriptions")
    if any(base.endswith(suffix) for suffix in suffixes):
        return base
    if path.startswith("/chat/completions") or path.startswith("/embeddings"):
        if parsed.path.rstrip("/").endswith("/v1") or parsed.path.rstrip("/").endswith("/v4"):
            return base + path
        return base + "/v1" + path
    return base + path


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 120,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise UpstreamHTTPError(exc.code, detail) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"upstream request failed: {exc.reason}") from exc


def _json_headers(runtime: dict[str, Any]) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        **{str(key): str(value) for key, value in (runtime.get("headers") or {}).items()},
    }


def _chat_api(runtime: dict[str, Any]) -> str:
    api = str(runtime.get("api") or "").strip().lower()
    path = urllib.parse.urlparse(str(runtime.get("base_url") or "")).path.rstrip("/")
    if path.endswith("/anthropic") and api in {"", "openai-completions"}:
        return "anthropic-messages"
    if api:
        return api
    definition = provider_definition(str(runtime.get("provider") or ""))
    return str((definition or {}).get("api") or "openai-completions")


def _anthropic_messages_url(runtime: dict[str, Any]) -> str:
    base = str(runtime.get("base_url") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation("Model base URL must use http or https")
    path = parsed.path.rstrip("/")
    if path.endswith("/messages"):
        return base
    if path.endswith("/v1") or path.endswith("/v1beta"):
        return base + "/messages"
    return base + "/v1/messages"


def _anthropic_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def _anthropic_image_block(ref: str) -> dict[str, Any] | None:
    if ref.startswith("data:") and ";base64," in ref:
        media_type, data = ref[5:].split(";base64,", 1)
        if media_type and data:
            return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
        return None
    parsed = urllib.parse.urlparse(ref)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return {"type": "image", "source": {"type": "url", "url": ref}}
    return None


def _anthropic_user_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and str(part.get("text") or "").strip():
            blocks.append({"type": "text", "text": str(part["text"]).strip()})
        elif part.get("type") == "image_url":
            image = part.get("image_url") if isinstance(part.get("image_url"), dict) else {}
            block = _anthropic_image_block(str(image.get("url") or ""))
            if block:
                blocks.append(block)
    return blocks


def _anthropic_tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(str(arguments or "{}"))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _anthropic_requested_reasoning(runtime: dict[str, Any]) -> tuple[str, bool]:
    raw = str(runtime.get("reasoning_effort") or "").strip().lower()
    if raw in ANTHROPIC_REASONING_LEVELS:
        return raw, True
    thinking = runtime.get("thinking")
    if isinstance(thinking, bool):
        return ("auto" if thinking else "off"), True
    return "", False


def _anthropic_clamp_level(level: str, levels: list[str]) -> str:
    if level not in ANTHROPIC_GRADED_LEVELS or not levels or level in levels:
        return level
    index = ANTHROPIC_GRADED_LEVELS.index(level)
    for candidate in ANTHROPIC_GRADED_LEVELS[index + 1:]:
        if candidate in levels:
            return candidate
    for candidate in reversed(ANTHROPIC_GRADED_LEVELS[:index]):
        if candidate in levels:
            return candidate
    return "auto"


def _anthropic_thinking_plan(runtime: dict[str, Any]) -> dict[str, Any]:
    level, requested = _anthropic_requested_reasoning(runtime)
    if not requested:
        return {"enabled": False, "body": None, "effort": "", "budget": 0}
    compat = runtime.get("compat") if isinstance(runtime.get("compat"), dict) else {}
    levels = [str(item) for item in runtime.get("thinking_levels") or [] if str(item)]
    mode = str(compat.get("thinking_mode") or "budget").strip().lower()
    if level in ANTHROPIC_GRADED_LEVELS:
        level = _anthropic_clamp_level(level, levels)
    if level == "off":
        if mode == "adaptive" and "off" in levels:
            return {
                "enabled": False,
                "body": {"type": str(compat.get("thinking_off_type") or "disabled")},
                "effort": "",
                "budget": 0,
            }
        return {"enabled": False, "body": None, "effort": "", "budget": 0}
    if mode == "adaptive":
        effort = level if level in ANTHROPIC_GRADED_LEVELS and bool(compat.get("supports_effort")) else ""
        return {"enabled": True, "body": {"type": "adaptive"}, "effort": effort, "budget": 0}
    budgets = compat.get("thinking_budgets") if isinstance(compat.get("thinking_budgets"), dict) else {}
    budget = int(runtime.get("thinking_budget_tokens") or 0)
    if budget <= 0:
        budget = int(budgets.get(level) or ANTHROPIC_THINKING_BUDGETS.get(level, ANTHROPIC_THINKING_BUDGETS["medium"]))
    budget = max(1024, budget)
    return {
        "enabled": True,
        "body": {"type": "enabled", "budget_tokens": budget},
        "effort": "",
        "budget": budget,
    }


def _anthropic_replay_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = message.get("reasoning_metadata")
    stored = metadata.get("anthropic_thinking_blocks") if isinstance(metadata, dict) else None
    if isinstance(stored, list):
        blocks: list[dict[str, Any]] = []
        for item in stored:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "thinking" and item.get("signature"):
                blocks.append(
                    {
                        "type": "thinking",
                        "thinking": str(item.get("thinking") or ""),
                        "signature": str(item["signature"]),
                    }
                )
            elif item.get("type") == "redacted_thinking" and item.get("data"):
                blocks.append({"type": "redacted_thinking", "data": str(item["data"])})
        return blocks
    signature = str(message.get("reasoning_signature") or "")
    content = str(message.get("reasoning_content") or "")
    if content and signature:
        return [{"type": "thinking", "thinking": content, "signature": signature}]
    return []


def _apply_legacy_thinking_control(runtime: dict[str, Any], payload: dict[str, Any]) -> None:
    extra = runtime.get("extra_config") if isinstance(runtime.get("extra_config"), dict) else {}
    control = str(extra.get("thinking_control") or extra.get("thinkingControl") or "").strip().lower()
    provider = str(runtime.get("provider") or "").lower()
    thinking = runtime.get("thinking")
    enabled = thinking if isinstance(thinking, bool) else bool(runtime.get("reasoning_effort"))
    if not control or (control == "none" and provider == "openrouter"):
        if provider == "openrouter" and isinstance(thinking, bool):
            payload["reasoning"] = {"enabled": thinking}
        return
    if control == "none":
        return
    if control == "reasoning":
        payload["reasoning"] = {"enabled": enabled}
        return
    payload["chat_template_kwargs"] = {"enable_thinking": enabled}


def _anthropic_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if role == "system":
            text = _anthropic_text(content)
            if text:
                system.append(text)
            continue
        if role == "assistant":
            blocks = _anthropic_replay_blocks(message)
            text = _anthropic_text(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": _anthropic_tool_input(function.get("arguments")),
                    }
                )
            if blocks:
                converted.append(
                    {
                        "role": "assistant",
                        "content": blocks[0]["text"] if len(blocks) == 1 and blocks[0]["type"] == "text" else blocks,
                    }
                )
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": str(content or ""),
            }
            if (
                converted
                and converted[-1]["role"] == "user"
                and isinstance(converted[-1].get("content"), list)
                and converted[-1]["content"]
                and converted[-1]["content"][0].get("type") == "tool_result"
            ):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue
        user_content = _anthropic_user_content(content)
        if user_content:
            converted.append({"role": "user", "content": user_content})
    return "\n\n".join(system), converted


def _anthropic_supports_sampling(model: str) -> bool:
    lowered = model.lower()
    return not any(lowered == item or lowered.startswith(item + "-") for item in ANTHROPIC_NO_SAMPLING)


def _anthropic_response(value: dict[str, Any]) -> dict[str, Any]:
    error = value.get("error") if isinstance(value.get("error"), dict) else {}
    if error:
        raise RuntimeError("API error: " + str(error.get("message") or error.get("type") or "unknown error"))
    text: list[str] = []
    thinking: list[str] = []
    thinking_blocks: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    for block in value.get("content") if isinstance(value.get("content"), list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            text.append(str(block["text"]))
        elif block.get("type") == "thinking":
            reasoning = str(block.get("thinking") or "")
            signature = str(block.get("signature") or "")
            thinking.append(reasoning)
            thinking_blocks.append({"type": "thinking", "thinking": reasoning, "signature": signature})
        elif block.get("type") == "redacted_thinking":
            thinking_blocks.append({"type": "redacted_thinking", "data": str(block.get("data") or "")})
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                }
            )
    stop_reason = str(value.get("stop_reason") or "")
    finish_reason = {
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }.get(stop_reason, "tool_calls" if tool_calls else "stop")
    usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
    prompt = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )
    completion = int(usage.get("output_tokens") or 0)
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if thinking:
        message["reasoning_content"] = "".join(thinking)
    if thinking_blocks:
        message["reasoning_metadata"] = {"anthropic_thinking_blocks": thinking_blocks}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": str(value.get("id") or ""),
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


def _anthropic_chat_completion(
    runtime: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    system, converted = _anthropic_messages(messages)
    plan = _anthropic_thinking_plan(runtime)
    completion_budget = int(max_tokens or runtime.get("max_tokens") or 4096)
    if plan["budget"] > 0 and completion_budget <= plan["budget"]:
        completion_budget = plan["budget"] + max(completion_budget, 1024)
    payload: dict[str, Any] = {
        "model": str(runtime.get("name") or ""),
        "max_tokens": completion_budget,
        "messages": converted,
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [
            {
                "name": str(tool.get("function", {}).get("name") or ""),
                "description": str(tool.get("function", {}).get("description") or ""),
                "input_schema": tool.get("function", {}).get("parameters")
                or {"type": "object", "properties": {}},
            }
            for tool in tools
            if isinstance(tool.get("function"), dict)
        ]
        payload["tool_choice"] = {"type": "auto"}
    if _anthropic_supports_sampling(str(runtime.get("name") or "")):
        compat = runtime.get("compat") if isinstance(runtime.get("compat"), dict) else {}
        temperature_with_thinking = bool(compat.get("temperature_with_thinking"))
        if temperature is not None and temperature > 0 and (
            not plan["enabled"] or temperature_with_thinking
        ):
            payload["temperature"] = temperature
        elif top_p is not None and top_p > 0 and not plan["enabled"]:
            payload["top_p"] = top_p
    if plan["body"] is not None:
        payload["thinking"] = plan["body"]
    if plan["effort"]:
        payload["output_config"] = {"effort": plan["effort"]}
    headers = _json_headers(runtime)
    headers.setdefault("anthropic-version", ANTHROPIC_VERSION)
    if runtime.get("api_key") and not any(key.lower() in {"x-api-key", "authorization"} for key in headers):
        headers["x-api-key"] = str(runtime["api_key"])
    release = model_limiter.acquire(runtime)
    try:
        _, _, raw = _http(
            "POST",
            _anthropic_messages_url(runtime),
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    finally:
        release()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("upstream returned an invalid chat response")
    return _anthropic_response(value)


def chat_completion(
    runtime: dict[str, Any],
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    if _chat_api(runtime) == "anthropic-messages":
        return _anthropic_chat_completion(
            runtime,
            messages,
            tools=tools,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )
    wire_messages = []
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("tool_calls")
            and not message.get("content")
        ):
            message = {**message, "content": ""}
        wire_messages.append(message)
    payload: dict[str, Any] = {
        "model": runtime.get("name"),
        "messages": wire_messages,
        "stream": False,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    _apply_legacy_thinking_control(runtime, payload)
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    release = model_limiter.acquire(runtime)
    try:
        _, _, raw = _http(
            "POST",
            request_url(runtime, "/chat/completions"),
            headers=_json_headers(runtime),
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    finally:
        release()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("upstream returned an invalid chat response")
    return value


def embedding(runtime: dict[str, Any], texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    name = str(runtime.get("name") or "")
    spec = runtime.get("spec") if isinstance(runtime.get("spec"), dict) else {}
    api = _embedding_api(str(runtime.get("provider") or ""), name, spec)
    url = _embedding_url(runtime, api)
    release = model_limiter.acquire(runtime)
    try:
        if api == "ark-embeddings":
            vectors: list[list[float]] = []
            for text in texts:
                payload = {
                    "model": name,
                    "encoding_format": "float",
                    "input": [{"type": "text", "text": text}],
                }
                _, _, raw = _http(
                    "POST",
                    url,
                    headers=_json_headers(runtime),
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=60,
                )
                vectors.extend(_embedding_vectors(json.loads(raw.decode("utf-8"))))
        elif api == "dashscope-embeddings":
            fuses = "qwen2.5-vl-embedding" in name.lower()
            batches = [[text] for text in texts] if fuses else [texts]
            vectors = []
            for batch in batches:
                payload = {
                    "model": name,
                    "input": {"contents": [{"text": text} for text in batch]},
                }
                _, _, raw = _http(
                    "POST",
                    url,
                    headers=_json_headers(runtime),
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=60,
                )
                vectors.extend(_embedding_vectors(json.loads(raw.decode("utf-8"))))
        elif api == "google-embeddings":
            qualified = "models/" + name.removeprefix("models/")
            payload = {
                "requests": [
                    {"model": qualified, "content": {"parts": [{"text": text}]}}
                    for text in texts
                ],
            }
            _, _, raw = _http(
                "POST",
                url,
                headers=_json_headers(runtime),
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=60,
            )
            vectors = _embedding_vectors(json.loads(raw.decode("utf-8")))
        else:
            profile = _embedding_messages_profile(
                str(runtime.get("provider") or ""), name, spec
            )
            if profile["text_as_messages"]:
                conversations = [
                    [{"role": "user", "content": [{"type": "text", "text": text}]}]
                    for text in texts
                ]
                if profile["batch_messages"] and len(conversations) > 1:
                    payload = _embedding_openai_body(runtime, "messages", conversations)
                    _, _, raw = _http(
                        "POST",
                        url,
                        headers=_json_headers(runtime),
                        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        timeout=60,
                    )
                    vectors = _embedding_vectors(json.loads(raw.decode("utf-8")))
                else:
                    vectors = []
                    for conversation in conversations:
                        payload = _embedding_openai_body(runtime, "messages", conversation)
                        _, _, raw = _http(
                            "POST",
                            url,
                            headers=_json_headers(runtime),
                            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            timeout=60,
                        )
                        vectors.extend(_embedding_vectors(json.loads(raw.decode("utf-8"))))
            else:
                payload = _embedding_openai_body(runtime, "input", texts)
                _, _, raw = _http(
                    "POST",
                    url,
                    headers=_json_headers(runtime),
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=60,
                )
                vectors = _embedding_vectors(json.loads(raw.decode("utf-8")))
    finally:
        release()
    if len(vectors) != len(texts) or any(not vector for vector in vectors):
        raise RuntimeError("upstream returned an unexpected embedding count")
    return vectors


def embedding_accepts_images(runtime: dict[str, Any]) -> bool:
    return _embedding_image_profile(
        str(runtime.get("provider") or ""),
        str(runtime.get("name") or ""),
        runtime.get("spec") if isinstance(runtime.get("spec"), dict) else {},
    ) is not None


def image_embedding_batch_size(runtime: dict[str, Any]) -> int:
    profile = _embedding_image_profile(
        str(runtime.get("provider") or ""),
        str(runtime.get("name") or ""),
        runtime.get("spec") if isinstance(runtime.get("spec"), dict) else {},
    )
    if not profile or not profile["batch_messages"]:
        return 1
    return max(1, int(profile["max_image_batch_size"] or 1))


def _embedding_url(runtime: dict[str, Any], api: str) -> str:
    base = str(runtime.get("base_url") or "").rstrip("/")
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation("Model base URL must use http or https")
    if api == "ark-embeddings":
        if base.endswith("/embeddings/multimodal"):
            return base
        root = base.split("/api/", 1)[0]
        return root + "/api/v3/embeddings/multimodal"
    if api == "dashscope-embeddings":
        root = base
        for marker in ("/compatible-mode", "/api/v1"):
            if marker in root:
                root = root.split(marker, 1)[0]
        return root + "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
    if api == "google-embeddings":
        if base.endswith("/openai"):
            base = base[:-7]
        model = str(runtime.get("name") or "").removeprefix("models/")
        return base + "/models/" + urllib.parse.quote(model, safe="") + ":batchEmbedContents"
    return request_url(runtime, "/embeddings")


def _embedding_openai_body(runtime: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    profile = _embedding_messages_profile(
        str(runtime.get("provider") or ""),
        str(runtime.get("name") or ""),
        runtime.get("spec") if isinstance(runtime.get("spec"), dict) else {},
    )
    payload: dict[str, Any] = {"model": runtime.get("name"), key: value}
    if profile["send_encoding_format"]:
        payload["encoding_format"] = "float"
    for field, item in profile["extra_body"].items():
        payload.setdefault(field, item)
    return payload


def _embedding_vectors(value: Any) -> list[list[float]]:
    if not isinstance(value, dict):
        raise RuntimeError("upstream returned an invalid embedding response")
    data = value.get("data")
    if isinstance(data, dict) and isinstance(data.get("embedding"), list):
        return [list(data["embedding"])]
    if isinstance(data, list):
        items = [item for item in data if isinstance(item, dict)]
        if items and all(isinstance(item.get("index"), int) for item in items):
            items.sort(key=lambda item: int(item["index"]))
        return [list(item.get("embedding") or []) for item in items]
    embeddings = value.get("embeddings")
    if isinstance(embeddings, list):
        return [list(item.get("values") or []) for item in embeddings if isinstance(item, dict)]
    output = value.get("output")
    if isinstance(output, dict) and isinstance(output.get("embeddings"), list):
        return [list(item.get("embedding") or []) for item in output["embeddings"] if isinstance(item, dict)]
    raise RuntimeError("upstream returned an invalid embedding response")


def image_embedding(runtime: dict[str, Any], data: bytes) -> list[list[float]]:
    return image_embeddings(runtime, [data])


def image_embeddings(runtime: dict[str, Any], images: list[bytes]) -> list[list[float]]:
    if not images:
        return []
    profile = _embedding_image_profile(
        str(runtime.get("provider") or ""),
        str(runtime.get("name") or ""),
        runtime.get("spec") if isinstance(runtime.get("spec"), dict) else {},
    )
    if profile is None:
        raise validation("embedding model does not accept images")
    api = profile["api"]
    prepared_images: list[tuple[bytes, str]] = []
    for data in images:
        prepared_images.append(
            image_prep.prepare(
                data,
                max_bytes=profile["max_bytes"],
                mime_types=profile["mime_types"],
            )
        )
    if api != "openai-embeddings":
        vectors: list[list[float]] = []
        for prepared, mime_type in prepared_images:
            vectors.extend(_native_image_embeddings(runtime, profile, prepared, mime_type))
        return vectors

    urls = [
        f"data:{mime_type};base64,{base64.b64encode(prepared).decode('ascii')}"
        for prepared, mime_type in prepared_images
    ]
    url = _embedding_url(runtime, api)
    if profile["image_format"] == "object":
        inputs = [{profile["image_field"]: value} for value in urls]
        payload = _embedding_openai_body(runtime, "input", inputs)
        if profile["truncate"]:
            payload["truncate"] = True
        if profile["image_prompt"]:
            for item in inputs:
                item["text"] = profile["image_prompt"]
        vectors = _embedding_image_requests(runtime, url, [payload])
    else:
        limit = profile["max_image_batch_size"] if profile["batch_messages"] and len(urls) > 1 else 1
        limit = max(1, int(limit or 1))
        payloads: list[dict[str, Any]] = []
        for start in range(0, len(urls), limit):
            conversations: list[list[dict[str, Any]]] = []
            for value in urls[start:start + limit]:
                content: list[dict[str, Any]] = [
                    {"type": "image_url", "image_url": {"url": value}}
                ]
                if profile["image_prompt"]:
                    content.append({"type": "text", "text": profile["image_prompt"]})
                conversations.append([{"role": "user", "content": content}])
            payload = _embedding_openai_body(
                runtime,
                "messages",
                conversations[0] if len(conversations) == 1 else conversations,
            )
            payload.setdefault("encoding_format", "float")
            payload["truncate_prompt_tokens"] = 511
            payloads.append(payload)
        vectors = _embedding_image_requests(runtime, url, payloads)
    if len(vectors) != len(images) or any(not vector for vector in vectors):
        raise RuntimeError("upstream returned an unexpected embedding count")
    return vectors


def _embedding_image_requests(
    runtime: dict[str, Any], url: str, payloads: list[dict[str, Any]]
) -> list[list[float]]:
    vectors: list[list[float]] = []
    for payload in payloads:
        release = model_limiter.acquire(runtime)
        try:
            _, _, raw = _http(
                "POST",
                url,
                headers=_json_headers(runtime),
                body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                timeout=60,
            )
        finally:
            release()
        vectors.extend(_embedding_vectors(json.loads(raw.decode("utf-8"))))
    return vectors


def _native_image_embeddings(
    runtime: dict[str, Any],
    profile: dict[str, Any],
    prepared: bytes,
    mime_type: str,
) -> list[list[float]]:
    encoded = base64.b64encode(prepared).decode("ascii")
    data_uri = f"data:{mime_type};base64,{encoded}"
    model = runtime.get("name")
    api = profile["api"]
    if api == "ark-embeddings":
        payload: dict[str, Any] = {
            "model": model,
            "encoding_format": "float",
            "input": [{"type": "image_url", "image_url": {"url": data_uri}}],
        }
    elif api == "dashscope-embeddings":
        payload = {"model": model, "input": {"contents": [{"image": data_uri}]}}
    else:
        payload = {
            "requests": [{
                "model": "models/" + str(model or "").removeprefix("models/"),
                "content": {"parts": [{"inlineData": {"mimeType": mime_type, "data": encoded}}]},
            }],
        }
    return _embedding_image_requests(runtime, _embedding_url(runtime, api), [payload])


def _rerank_compat(runtime: dict[str, Any]) -> dict[str, Any]:
    compat = runtime.get("compat")
    return compat if isinstance(compat, dict) else {}


def _rerank_extra_body(runtime: dict[str, Any]) -> dict[str, Any]:
    extra = _rerank_compat(runtime).get("extra_body")
    return dict(extra) if isinstance(extra, dict) else {}


def _rerank_batch_size(runtime: dict[str, Any], default: int) -> int:
    try:
        value = int(_rerank_compat(runtime).get("max_documents") or default)
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def _tei_rerank(runtime: dict[str, Any], query: str, documents: list[str]) -> list[dict[str, Any]]:
    extra = _rerank_extra_body(runtime)
    limit = _rerank_batch_size(runtime, 32)
    output: list[dict[str, Any]] = []
    for offset in range(0, len(documents), limit):
        batch = documents[offset:offset + limit]
        payload: dict[str, Any] = {"query": query, "texts": batch, "raw_scores": False}
        for key, value in extra.items():
            if key not in payload:
                payload[key] = value
        payload.setdefault("truncate", True)
        _, _, raw = _http(
            "POST",
            request_url(runtime, "/rerank"),
            headers=_json_headers(runtime),
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        results = json.loads(raw.decode("utf-8"))
        if not isinstance(results, list) or len(results) != len(batch):
            raise RuntimeError("upstream returned an invalid rerank response")
        seen: set[int] = set()
        for item in results:
            index = item.get("index") if isinstance(item, dict) else None
            score = item.get("score") if isinstance(item, dict) else None
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(batch):
                raise RuntimeError("TEI rerank index is missing or out of range")
            if index in seen:
                raise RuntimeError("TEI returned duplicate rerank index")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise RuntimeError("TEI rerank score is missing")
            seen.add(index)
            output.append({"index": offset + index, "relevance_score": score, "document": batch[index]})
    return sorted(output, key=lambda item: float(item.get("relevance_score") or 0), reverse=True)


def _pinecone_rerank(runtime: dict[str, Any], query: str, documents: list[str]) -> list[dict[str, Any]]:
    compat = _rerank_compat(runtime)
    limit = _rerank_batch_size(runtime, 100)
    extra = _rerank_extra_body(runtime)
    output: list[dict[str, Any]] = []
    for offset in range(0, len(documents), limit):
        batch = documents[offset:offset + limit]
        payload: dict[str, Any] = {
            "model": runtime.get("name"),
            "query": query,
            "documents": [{"text": document} for document in batch],
            "top_n": len(batch),
            "rank_fields": ["text"],
            "return_documents": False,
        }
        truncate = compat.get("truncate") or (runtime.get("spec") or {}).get("truncate")
        if truncate:
            payload["parameters"] = {"truncate": str(truncate)}
        for key, value in extra.items():
            if key not in payload:
                payload[key] = value
        headers = _json_headers(runtime)
        headers.setdefault("X-Pinecone-Api-Version", "2026-07")
        _, _, raw = _http(
            "POST",
            request_url(runtime, "/rerank"),
            headers=headers,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        value = json.loads(raw.decode("utf-8"))
        results = value.get("data") if isinstance(value, dict) else None
        if not isinstance(results, list) or len(results) != len(batch):
            raise RuntimeError("upstream returned an invalid rerank response")
        seen: set[int] = set()
        for item in results:
            index = item.get("index") if isinstance(item, dict) else None
            score = item.get("score") if isinstance(item, dict) else None
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(batch):
                raise RuntimeError("Pinecone rerank index is missing or out of range")
            if index in seen:
                raise RuntimeError("Pinecone returned duplicate rerank index")
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise RuntimeError("Pinecone rerank score is missing")
            seen.add(index)
            output.append({"index": offset + index, "relevance_score": score, "document": batch[index]})
    return sorted(output, key=lambda item: float(item.get("relevance_score") or 0), reverse=True)


def rerank(runtime: dict[str, Any], query: str, documents: list[str]) -> list[dict[str, Any]]:
    provider = str(runtime.get("provider") or "").lower()
    if provider == "huggingface_tei":
        return _tei_rerank(runtime, query, documents)
    if provider == "pinecone":
        return _pinecone_rerank(runtime, query, documents)
    payload = {"model": runtime.get("name"), "query": query, "documents": documents, "top_n": len(documents)}
    _, _, raw = _http(
        "POST",
        request_url(runtime, "/rerank"),
        headers=_json_headers(runtime),
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    value = json.loads(raw.decode("utf-8"))
    results = value.get("results") if isinstance(value, dict) else None
    if results is None and isinstance(value, dict):
        results = value.get("data")
    if not isinstance(results, list):
        raise RuntimeError("upstream returned an invalid rerank response")
    output = []
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        output.append(
            {
                "index": int(index) if isinstance(index, int) else index,
                "relevance_score": item.get("relevance_score", item.get("score", 0)),
                "document": documents[int(index)] if isinstance(index, int) and 0 <= index < len(documents) else item.get("document"),
            }
        )
    return sorted(output, key=lambda item: float(item.get("relevance_score") or 0), reverse=True)


def rerank_accepts_images(runtime: dict[str, Any]) -> bool:
    return _rerank_image_profile(
        str(runtime.get("provider") or ""),
        str(runtime.get("name") or ""),
        runtime.get("spec") if isinstance(runtime.get("spec"), dict) else {},
    ) is not None


def image_rerank(runtime: dict[str, Any], query: str, data: bytes) -> list[dict[str, Any]]:
    profile = _rerank_image_profile(
        str(runtime.get("provider") or ""),
        str(runtime.get("name") or ""),
        runtime.get("spec") if isinstance(runtime.get("spec"), dict) else {},
    )
    if profile is None:
        raise validation("rerank model does not accept images")
    prepared, mime_type = image_prep.prepare(
        data,
        max_bytes=profile["max_bytes"],
        mime_types=profile["mime_types"],
    )
    data_uri = f"data:{mime_type};base64,{base64.b64encode(prepared).decode('ascii')}"
    model = runtime.get("name")
    if profile["api"] == "nim":
        payload = {
            "model": model,
            "query": {"text": query},
            "truncate": "END",
            "passages": [{"image": data_uri}],
        }
        url = str(runtime.get("base_url") or "").rstrip("/")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise validation("Model base URL must use http or https")
    else:
        payload = {
            "model": model,
            "query": query,
            "return_documents": False,
            "documents": [{profile["image_field"]: data_uri}],
        }
        url = request_url(runtime, "/rerank")

    release = model_limiter.acquire(runtime)
    try:
        _, _, raw = _http(
            "POST",
            url,
            headers=_json_headers(runtime),
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
    finally:
        release()
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("upstream returned an invalid rerank response")
    results = value.get("results")
    nim = value.get("rankings")
    if isinstance(results, list):
        output = []
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            output.append({
                "index": int(index) if isinstance(index, int) else index,
                "relevance_score": item.get("relevance_score", item.get("score", 0)),
            })
    elif isinstance(nim, list):
        output = []
        for item in nim:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            logit = float(item.get("logit") or 0)
            score = 1 / (1 + math.exp(-logit)) if logit >= 0 else math.exp(logit) / (1 + math.exp(logit))
            output.append({"index": int(index) if isinstance(index, int) else index, "relevance_score": score})
    else:
        raise RuntimeError("upstream returned an invalid rerank response")
    return sorted(output, key=lambda item: float(item.get("relevance_score") or 0), reverse=True)


def transcribe(
    runtime: dict[str, Any],
    data: bytes,
    filename: str,
    *,
    language: str = "",
) -> dict[str, Any]:
    boundary = "----PyWeKnora" + base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    model = str(runtime.get("name") or "whisper-1")
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    language = language.replace("\r", " ").replace("\n", " ").strip()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
        (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n".encode()
            if language
            else b""
        ),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{os.path.basename(filename)}\"\r\nContent-Type: {content_type}\r\n\r\n".encode(),
        data,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    headers = _json_headers(runtime)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    _, _, raw = _http("POST", request_url(runtime, "/audio/transcriptions"), headers=headers, body=b"".join(parts))
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else {"text": str(value)}


def text_from_chat_response(value: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    choices = value.get("choices") if isinstance(value.get("choices"), list) else []
    if not choices or not isinstance(choices[0], dict):
        return "", None
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    return str(message.get("content") or ""), message or None


def safe_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    clean = dict(parameters or {})
    for field in ("api_key", "app_secret"):
        clean.pop(field, None)
    nested = clean.get("credentials")
    if isinstance(nested, dict):
        clean["credentials"] = {
            key: {"configured": bool(value.get("value") if isinstance(value, dict) else value)}
            for key, value in nested.items()
            if key in {"api_key", "app_secret"}
        }
    clean.pop("_credentials", None)
    extra = clean.get("extra_config")
    if isinstance(extra, dict):
        clean["extra_config"] = {
            str(key): "[REDACTED]" if any(word in str(key).lower() for word in ("secret", "token", "password", "api_key", "authorization")) else value
            for key, value in extra.items()
        }
    return clean


def serialize_model(row: dict[str, Any], *, include_credentials: bool = True) -> dict[str, Any]:
    result = dict(row)
    parameters = _parameters(result.get("parameters"))
    result["parameters"] = safe_parameters(parameters)
    result["is_default"] = bool(result.get("is_default"))
    result["is_builtin"] = bool(result.get("is_builtin"))
    if include_credentials:
        result["credentials"] = {
            "api_key": {"configured": bool(_secret(parameters, "api_key"))},
            "app_secret": {"configured": bool(_secret(parameters, "app_secret"))},
        }
    model_type = str(result.get("type") or "")
    if model_type in {"KnowledgeQA", "VLLM"}:
        resolution = catalog_resolution(
            {
                "provider": str(parameters.get("provider") or result.get("source") or ""),
                "model": str(result.get("name") or ""),
                "model_type": model_type,
                "base_url": str(parameters.get("base_url") or ""),
                "spec": parameters.get("spec") if isinstance(parameters.get("spec"), dict) else {},
            }
        )
        result["capabilities"] = resolution["capabilities"]
        if model_type == "VLLM" or parameters.get("supports_vision"):
            result["capabilities"]["input"] = ["text", "image"]
    elif model_type == "Embedding":
        spec = parameters.get("spec") if isinstance(parameters.get("spec"), dict) else {}
        provider = str(parameters.get("provider") or result.get("source") or "")
        profile = _embedding_image_profile(
            provider,
            str(result.get("name") or ""),
            spec,
        )
        result["capabilities"] = {
            "provider": provider,
            "api": _embedding_api(provider, str(result.get("name") or ""), spec),
            "cataloged": False,
            "reasoning": False,
            "thinking_levels": [],
            "thinking_format": "none",
            "input": ["text", "image"] if profile else ["text"],
        }
    elif model_type == "Rerank":
        spec = parameters.get("spec") if isinstance(parameters.get("spec"), dict) else {}
        profile = _rerank_image_profile(
            str(parameters.get("provider") or result.get("source") or ""),
            str(result.get("name") or ""),
            spec,
        )
        result["capabilities"] = {
            "provider": str(parameters.get("provider") or result.get("source") or ""),
            "api": profile["api"] if profile else "openai-completions",
            "cataloged": False,
            "reasoning": False,
            "thinking_levels": [],
            "thinking_format": "none",
            "input": ["text", "image"] if profile else ["text"],
        }
    return result


def catalog_resolution(data: dict[str, Any]) -> dict[str, Any]:
    provider = str(data.get("provider") or "openai-compatible").strip() or "openai-compatible"
    model_type = backend_model_type(str(data.get("model_type") or "KnowledgeQA")) or "KnowledgeQA"
    model = str(data.get("model") or data.get("remote_model_name") or "").strip()
    base_url = str(data.get("base_url") or "").strip()
    definition = provider_definition(provider)
    if not base_url and definition:
        frontend = _frontend_model_type(model_type)
        base_url = str((definition.get("defaultUrls") or {}).get(frontend) or "")
    catalog_definition, catalog_model = _catalog_model(provider, model, model_type)
    cataloged = catalog_model is not None
    overrides = data.get("spec") if isinstance(data.get("spec"), dict) else {}
    api = str(
        overrides.get("api")
        or data.get("api")
        or (catalog_model or {}).get("api")
        or (catalog_definition or {}).get("api")
        or "openai-completions"
    )
    if model_type == "Embedding":
        embedding_spec: dict[str, Any] = {"input": ["text"]}
        if data.get("api"):
            embedding_spec["api"] = data["api"]
        api = _embedding_api(provider, model, catalog_model or embedding_spec)

    model_spec = dict(catalog_model or {"id": model, "name": model, "type": _frontend_model_type(model_type)})
    model_spec.update(
        {
            "id": model,
            "name": str(model_spec.get("name") or model),
            "type": model_type,
        }
    )
    for field in ("reasoning", "input", "context_window", "max_output_tokens", "thinking_levels", "compat"):
        if field in overrides:
            model_spec[field] = overrides[field]
    thinking = (definition or {}).get("thinking") if isinstance((definition or {}).get("thinking"), dict) else {}
    levels = model_spec.get("thinking_levels")
    if not isinstance(levels, list):
        levels = thinking.get("levels") if isinstance(thinking.get("levels"), list) else []
    context_window = int(
        model_spec.get("context_window")
        or (200000 if model_type == "KnowledgeQA" and not cataloged else 0)
    )
    max_output_tokens = int(model_spec.get("max_output_tokens") or 0)
    reasoning = bool(model_spec.get("reasoning"))
    accepted_inputs = model_spec.get("input") if isinstance(model_spec.get("input"), list) else []
    if not accepted_inputs:
        accepted_inputs = ["text"]
    capabilities = {
        "provider": provider,
        "api": api,
        "cataloged": cataloged,
        "reasoning": reasoning,
        "thinking_levels": list(levels),
        "thinking_format": str(thinking.get("format") or "none"),
        "input": list(accepted_inputs),
        "context_window": context_window,
        "max_output_tokens": max_output_tokens,
        "max_tokens_field": "max_tokens",
    }
    if model_type == "Embedding" and _embedding_image_profile(provider, model, {}) is not None:
        capabilities["input"] = ["text", "image"]
    if model_type == "Rerank" and _rerank_image_profile(provider, model, {}) is not None:
        capabilities["input"] = ["text", "image"]
    return {
        "provider": provider,
        "api": api,
        "base_url": base_url,
        "remote_model": model,
        "cataloged": cataloged,
        "model": model_spec,
        "capabilities": capabilities,
    }

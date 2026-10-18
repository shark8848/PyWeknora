"""Knowledge bases, documents, chunks, tags, FAQ and search APIs."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import time
import urllib.parse
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .. import config, db
from ..api_keys import authorize_knowledge_bases
from ..deps import parse_auth, require_role
from ..errors import forbidden, not_found, validation
from ..services.chunking import (
    chunk_with_offsets,
    extract_document,
    extract_text,
    strip_embedded_markers,
)
from ..services import chunker_adaptive
from ..services import document_summary
from ..services import audio_asr
from ..services.access import (
    knowledge_base_share_permission,
    readable_knowledge,
    readable_knowledge_base,
    shared_knowledge_base_ids,
)
from ..services import file_proxy
from ..services import image_multimodal
from ..services import image_vectors
from ..services import model_limiter
from ..services import model_runtime
from ..services import object_storage
from ..services import resource_catalog
from ..services import storage_quota
from ..services import url_guard
from ..services.retrieval import references, retrieve
from ..services.wiki_ingest import ingest_all, ingest_knowledge, remove_knowledge_source
from ..utils import dumps, loads, ok, page, pagination, resolve_locale, truthy


router = APIRouter(tags=["knowledge"])
_knowledge_task_workers: set[asyncio.Task[None]] = set()
_task_worker_claims: dict[str, str] = {}
TASK_WORKER_LEASE_SECONDS = 300.0
TASK_WORKER_RENEW_SECONDS = 60.0
TASK_WORKER_MAX_RETRIES = 3
TASK_RETRY_BASE_SECONDS = 10.0
SUMMARY_TASK_KIND = "summary_generation"
SUMMARY_MIN_TEXT_CHARS = 10
SUMMARY_FALLBACK_CHARS = 500
SUMMARY_MAX_DESCRIPTION_CHARS = 4000

SUPPORTED_IMPORT_EXTENSIONS = {
    "pdf", "txt", "docx", "doc", "epub",
    "html", "htm", "mhtml", "md", "markdown",
    "xmind",
    "png", "jpg", "jpeg", "gif",
    "csv", "xlsx", "xls", "pptx", "ppt", "json",
    "mp3", "wav", "m4a", "flac", "ogg",
}

JSON_KB_FIELDS = (
    "chunking_config", "image_processing_config", "cos_config", "storage_provider_config",
    "vlm_config", "extract_config", "faq_config", "question_generation_config",
    "wiki_config", "indexing_strategy", "auto_tag_config", "profile_config",
    "generated_profile", "asr_config",
)
JSON_KNOWLEDGE_FIELDS = (
    "metadata", "custom_metadata", "last_faq_import_result", "profile",
)
JSON_CHUNK_FIELDS = (
    "image_info", "video_info", "relation_chunks", "indirect_relation_chunks",
    "metadata", "source_locators",
)


async def body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _max_file_size_mb() -> int:
    try:
        value = int(os.getenv("MAX_FILE_SIZE_MB", ""))
    except ValueError:
        value = 50
    return value if value > 0 else 50


def _split_upload_name(raw: str) -> tuple[str, str]:
    normalized = raw.replace("\\", "/")
    folder, separator, file_name = normalized.rpartition("/")
    if not separator:
        return "", normalized.strip()
    parts: list[str] = []
    for part in folder.split("/"):
        part = part.strip()
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts), file_name.strip()


def _validate_import_file_type(filename: str) -> str:
    if audio_asr.is_video_filename(filename):
        raise validation("暂不支持上传视频文件")
    extension = Path(filename).suffix.lower().lstrip(".")
    if not extension:
        raise validation("无法确定文件类型")
    if extension not in SUPPORTED_IMPORT_EXTENSIONS:
        raise validation(f"不支持的文件类型: {extension}")
    return extension


def _validate_json_upload(filename: str, raw: bytes) -> None:
    if Path(filename).suffix.lower() != ".json":
        return
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise validation("JSON 文件内容无效，请检查格式") from exc
    if decoded == "":
        raise validation("JSON 文件内容为空")
    try:
        json.loads(decoded)
    except ValueError as exc:
        raise validation("JSON 文件内容无效，请检查格式") from exc


_PROCESS_CONFIG_BOOL_FIELDS = (
    "summary_enabled", "enable_multimodel", "graph_enabled",
    "image_attrs_enabled", "image_vector_enabled",
)
_PROCESS_CONFIG_OBJECT_FIELDS = (
    "chunking_config", "vlm_config", "asr_config", "question_generation_config",
    "image_actions", "extract_config",
)
_PROCESS_CONFIG_INT_FIELDS = {
    "chunking_config": ("chunk_size", "chunk_overlap", "parent_chunk_size", "child_chunk_size", "token_limit"),
    "question_generation_config": ("question_count",),
}
_PROCESS_CONFIG_NESTED_BOOL_FIELDS = {
    "chunking_config": ("enable_parent_child",),
    "vlm_config": ("enabled",),
    "asr_config": ("enabled",),
    "question_generation_config": ("enabled",),
    "extract_config": ("enabled",),
}
_PROCESS_CONFIG_STRING_FIELDS = {
    "chunking_config": ("strategy", "table_metadata_instructions"),
    "vlm_config": (
        "model_id", "description_language", "custom_instructions", "model_name",
        "base_url", "api_key", "interface_type",
    ),
    "asr_config": ("model_id", "language"),
    "question_generation_config": ("custom_instructions",),
    "extract_config": ("text", "custom_instructions"),
}
_PROCESS_CONFIG_LIST_FIELDS = {
    "chunking_config": ("separators", "languages", "parser_engine_rules"),
    "extract_config": ("tags", "nodes", "relations"),
}


def _validate_process_config(process_config: dict[str, Any]) -> None:
    def invalid() -> None:
        raise validation("Invalid process_config format")

    for field in _PROCESS_CONFIG_BOOL_FIELDS:
        if field in process_config and not isinstance(process_config[field], bool):
            invalid()
    for field in _PROCESS_CONFIG_OBJECT_FIELDS:
        if field in process_config and not isinstance(process_config[field], dict):
            invalid()
    if "parser_engine_rules" in process_config and not isinstance(process_config["parser_engine_rules"], list):
        invalid()
    if "parser_engine_overrides" in process_config:
        value = process_config["parser_engine_overrides"]
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in value.items()
        ):
            invalid()
    for field, int_fields in _PROCESS_CONFIG_INT_FIELDS.items():
        value = process_config.get(field)
        if not isinstance(value, dict):
            continue
        for key in int_fields:
            if key in value and (not isinstance(value[key], int) or isinstance(value[key], bool)):
                invalid()
    for field, bool_fields in _PROCESS_CONFIG_NESTED_BOOL_FIELDS.items():
        value = process_config.get(field)
        if not isinstance(value, dict):
            continue
        for key in bool_fields:
            if key in value and not isinstance(value[key], bool):
                invalid()
    for field, string_fields in _PROCESS_CONFIG_STRING_FIELDS.items():
        value = process_config.get(field)
        if not isinstance(value, dict):
            continue
        for key in string_fields:
            if key in value and not isinstance(value[key], str):
                invalid()
    for field, list_fields in _PROCESS_CONFIG_LIST_FIELDS.items():
        value = process_config.get(field)
        if not isinstance(value, dict):
            continue
        for key in list_fields:
            if key in value and not isinstance(value[key], list):
                invalid()


def _merge_process_config(kb: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    effective = dict(kb)
    if not overrides:
        return effective

    chunking_override = overrides.get("chunking_config")
    if isinstance(chunking_override, dict):
        base = loads(kb.get("chunking_config"), {})
        chunking = dict(base) if isinstance(base, dict) else {}
        for key in ("chunk_size", "chunk_overlap", "parent_chunk_size", "child_chunk_size", "token_limit"):
            if chunking_override.get(key) not in (None, 0):
                chunking[key] = chunking_override[key]
        for key in ("separators", "languages", "table_metadata_instructions", "strategy"):
            if chunking_override.get(key) not in (None, "", []):
                chunking[key] = chunking_override[key]
        chunking["enable_parent_child"] = bool(chunking_override.get("enable_parent_child", False))
        if chunking_override.get("parser_engine_rules"):
            chunking["parser_engine_rules"] = chunking_override["parser_engine_rules"]
        effective["chunking_config"] = dumps(chunking)
    if overrides.get("parser_engine_rules"):
        base_chunking = loads(effective.get("chunking_config"), {})
        base_chunking = dict(base_chunking) if isinstance(base_chunking, dict) else {}
        effective["chunking_config"] = dumps({
            **base_chunking,
            "parser_engine_rules": overrides["parser_engine_rules"],
        })

    for field in ("vlm_config", "asr_config", "question_generation_config"):
        override = overrides.get(field)
        if not isinstance(override, dict):
            continue
        config = dict(override)
        base = loads(kb.get(field), {})
        if isinstance(base, dict):
            for key in ("description_language", "custom_instructions"):
                if field in {"vlm_config", "question_generation_config"} and not config.get(key):
                    config[key] = base.get(key, "")
        effective[field] = dumps(config)

    image_config = loads(kb.get("image_processing_config"), {})
    image_config = dict(image_config) if isinstance(image_config, dict) else {}
    for key in ("image_attrs_enabled", "image_vector_enabled"):
        if key in overrides:
            image_config[key] = overrides[key]
    if isinstance(overrides.get("image_actions"), dict):
        image_config["image_actions"] = overrides["image_actions"]
    if image_config:
        effective["image_processing_config"] = dumps(image_config)

    extract_override = overrides.get("extract_config")
    if isinstance(extract_override, dict):
        base = loads(kb.get("extract_config"), {})
        extract = dict(base) if isinstance(base, dict) else {}
        extract["enabled"] = bool(extract_override.get("enabled", False))
        for key in ("text", "tags", "nodes", "relations", "custom_instructions"):
            if extract_override.get(key) not in (None, "", []):
                extract[key] = extract_override[key]
        effective["extract_config"] = dumps(extract)

    extract = loads(effective.get("extract_config"), {})
    graph_enabled = overrides.get("graph_enabled")
    if isinstance(graph_enabled, bool) or isinstance(extract_override, dict):
        strategy = loads(kb.get("indexing_strategy"), {})
        strategy = dict(strategy) if isinstance(strategy, dict) else {}
        strategy_graph_enabled = strategy.get("graph_enabled") is True
        if isinstance(graph_enabled, bool):
            strategy_graph_enabled = graph_enabled
        strategy["graph_enabled"] = strategy_graph_enabled and bool(
            isinstance(extract, dict) and extract.get("enabled")
        )
        effective["indexing_strategy"] = dumps(strategy)
    return effective


def _knowledge_process_overrides(knowledge: dict[str, Any]) -> dict[str, Any] | None:
    metadata = loads(knowledge.get("metadata"), {})
    if not isinstance(metadata, dict):
        return None
    overrides = metadata.get("process_overrides")
    return overrides if isinstance(overrides, dict) else None


def _validate_file_process_admission(kb: dict[str, Any], filename: str, extension: str) -> None:
    if audio_asr.is_audio_filename(filename) and not audio_asr.asr_configured(kb):
        raise validation("上传音频文件需要设置ASR语音识别模型")
    file_type = (extension or Path(filename).suffix.lstrip(".")).lower().lstrip(".")
    if file_type in {"png", "jpg", "jpeg", "gif"} and not image_multimodal.vlm_configured(kb):
        raise validation("上传图片文件需要设置VLM模型")


def _decode_json_fields(row: dict[str, Any], fields: tuple[str, ...], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(row)
    defaults = defaults or {}
    for field in fields:
        result[field] = loads(result.get(field), defaults.get(field, {} if field not in {"image_info"} else []))
    for key in ("is_enabled", "is_temporary", "is_pinned", "is_default", "is_builtin"):
        if key in result and result[key] is not None:
            result[key] = bool(result[key])
    return result


def _creator_name(user_id: str | None) -> str:
    if not user_id:
        return ""
    row = db.fetch_one("SELECT username, email FROM users WHERE id=?", (user_id,))
    return (row.get("username") or row.get("email") or "") if row else ""


def serialize_kb(row: dict[str, Any], user_id: str | None = None) -> dict[str, Any]:
    result = _decode_json_fields(row, JSON_KB_FIELDS)
    counts = db.fetch_one(
        """
        SELECT
          (SELECT COUNT(*) FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL) AS knowledge_count,
          (SELECT COUNT(*) FROM chunks WHERE knowledge_base_id=? AND deleted_at IS NULL) AS chunk_count
        """,
        (row["id"], row["id"]),
    ) or {"knowledge_count": 0, "chunk_count": 0}
    result["knowledge_count"] = int(counts["knowledge_count"])
    result["document_count"] = int(counts["knowledge_count"])
    result["chunk_count"] = int(counts["chunk_count"])
    result["creator_name"] = _creator_name(row.get("creator_id"))
    result["is_pinned"] = False
    result["pinned_at"] = None
    if user_id:
        pin = db.fetch_one(
            "SELECT pinned_at FROM user_kb_pins WHERE user_id=? AND kb_id=?",
            (user_id, row["id"]),
        )
        if pin:
            result["is_pinned"] = True
            result["pinned_at"] = pin.get("pinned_at")
    result["vector_store_id"] = row.get("vector_store_id")
    result["vector_store_name"] = "Python 本地向量索引"
    result["vector_store_engine_type"] = "sqlite"
    result["vector_store_source"] = "env"
    result["vector_store_status"] = "available"
    return result


def _get_kb(ctx: Any, kb_id: str) -> dict[str, Any]:
    if ctx.api_key is not None:
        authorize_knowledge_bases(ctx.api_key, {kb_id})
    row = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Knowledge base not found")
    return row


def _require_kb_write(ctx: Any, kb: dict[str, Any]) -> None:
    if str(kb.get("creator_id") or "") == ctx.user_id:
        return
    require_role(ctx, "admin")


def _require_kb_write_access(ctx: Any, kb: dict[str, Any]) -> None:
    if int(kb.get("tenant_id") or 0) == ctx.tenant_id:
        _require_kb_write(ctx, kb)
        return
    permission = knowledge_base_share_permission(ctx.tenant_id, str(kb["id"]), ctx.role)
    if permission not in {"editor", "admin"}:
        raise forbidden("Knowledge base write access required")


def _get_writable_kb(ctx: Any, kb_id: str) -> dict[str, Any]:
    kb = _get_kb(ctx, kb_id)
    _require_kb_write(ctx, kb)
    return kb


def _get_writable_knowledge(ctx: Any, knowledge_id: str, kb_id: str | None = None) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM knowledges WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (knowledge_id, ctx.tenant_id),
    )
    if not row or (kb_id and str(row.get("knowledge_base_id")) != kb_id):
        raise not_found("Knowledge not found")
    _get_writable_kb(ctx, str(row["knowledge_base_id"]))
    return row


def _get_writable_chunk(ctx: Any, chunk_id: str, knowledge_id: str | None = None) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM chunks WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (chunk_id, ctx.tenant_id),
    )
    if not row or (knowledge_id and str(row.get("knowledge_id")) != knowledge_id):
        raise not_found("Chunk not found")
    if ctx.api_key is not None:
        authorize_knowledge_bases(ctx.api_key, {str(row.get("knowledge_base_id") or "")})
    _get_writable_knowledge(ctx, str(row["knowledge_id"]))
    return row


def _get_readable_kb(ctx: Any, kb_id: str) -> dict[str, Any]:
    if ctx.api_key is not None:
        authorize_knowledge_bases(ctx.api_key, {kb_id})
    row = readable_knowledge_base(ctx.tenant_id, kb_id, ctx.role)
    if not row:
        raise not_found("Knowledge base not found")
    return row


def _get_readable_knowledge(ctx: Any, knowledge_id: str) -> dict[str, Any]:
    row = readable_knowledge(ctx.tenant_id, knowledge_id, ctx.role)
    if not row:
        raise not_found("Knowledge not found")
    if ctx.api_key is not None:
        authorize_knowledge_bases(ctx.api_key, {str(row.get("knowledge_base_id") or "")})
    return row


def _get_readable_chunk(ctx: Any, chunk_id: str, knowledge_id: str | None = None) -> dict[str, Any]:
    row = db.fetch_one("SELECT * FROM chunks WHERE id=? AND deleted_at IS NULL", (chunk_id,))
    if not row or (knowledge_id and str(row.get("knowledge_id")) != knowledge_id):
        raise not_found("Chunk not found")
    _get_readable_knowledge(ctx, str(row.get("knowledge_id") or ""))
    return row


def _text_references_file(value: str, reference: str) -> bool:
    if not value or not reference:
        return False
    for match in re.finditer(re.escape(reference), value):
        before = value[match.start() - 1] if match.start() else ""
        after = value[match.end()] if match.end() < len(value) else ""
        if not re.match(r"[0-9A-Za-z_./-]", before) and not re.match(r"[0-9A-Za-z_./-]", after):
            return True
    return False


def _value_references_file(value: Any, reference: str) -> bool:
    if isinstance(value, str):
        return _text_references_file(value, reference)
    if isinstance(value, list):
        return any(_value_references_file(item, reference) for item in value)
    if isinstance(value, dict):
        return any(_value_references_file(item, reference) for item in value.values())
    return False


def _kb_references_file(kb_id: str, tenant_id: int, reference: str) -> bool:
    checks = (
        ("chunks", ("content", "source_content", "image_info", "video_info", "metadata")),
        ("knowledges", ("file_path", "metadata")),
        ("wiki_pages", ("content", "source_refs", "page_metadata")),
    )
    for table, columns in checks:
        predicate = " OR ".join(f"instr(COALESCE({column}, ''), ?) > 0" for column in columns)
        rows = db.fetch_all(
            f"SELECT {', '.join(columns)} FROM {table} WHERE knowledge_base_id=? AND tenant_id=? "
            f"AND deleted_at IS NULL AND ({predicate}) LIMIT 20",
            (kb_id, tenant_id, *([reference] * len(columns))),
        )
        for row in rows:
            for column in columns:
                raw = row.get(column)
                value = loads(raw, None)
                if value is not None and _value_references_file(value, reference):
                    return True
                if _text_references_file(str(raw or ""), reference):
                    return True
    return False


def serialize_knowledge(row: dict[str, Any]) -> dict[str, Any]:
    result = _decode_json_fields(row, JSON_KNOWLEDGE_FIELDS)
    result["tags"] = db.fetch_all(
        """
        SELECT t.id, t.name, t.color, t.seq_id
        FROM knowledge_tag_relations r JOIN knowledge_tags t ON t.id=r.tag_id
        WHERE r.knowledge_id=? AND t.deleted_at IS NULL
        """,
        (row["id"],),
    )
    return result


def serialize_chunk(row: dict[str, Any]) -> dict[str, Any]:
    result = _decode_json_fields(row, JSON_CHUNK_FIELDS)
    result.pop("embedding", None)
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    result["questions"] = metadata.get("generated_questions") or []
    return result


def _chunk_questions(chunk_id: str, tenant_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    row = db.fetch_one(
        "SELECT * FROM chunks WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (chunk_id, tenant_id),
    )
    if not row:
        raise not_found("Chunk not found")
    metadata = loads(row.get("metadata"), {})
    if not isinstance(metadata, dict):
        metadata = {}
    questions = metadata.get("generated_questions")
    return row, questions if isinstance(questions, list) else []


def _save_chunk_questions(conn: Any, chunk: dict[str, Any], questions: list[dict[str, Any]]) -> None:
    metadata = loads(chunk.get("metadata"), {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["generated_questions"] = questions
    metadata["generated_questions_revision"] = int(chunk.get("content_revision") or 1)
    conn.execute(
        "UPDATE chunks SET metadata=?, updated_at=? WHERE id=?",
        (dumps(metadata), db.utcnow(), chunk["id"]),
    )


_GALLERY_FIELD_ATTRS = {
    "system:contain.text",
    "system:contain.data_visual",
}


def _gallery_sources() -> list[dict[str, Any]]:
    return [
        {
            "id": "builtin",
            "attrs": [
                {
                    "name": "caption", "type": "text", "label": "Caption",
                    "description": "The model-generated description of the image.",
                    "usage": {"in_filter": False, "in_searchfield": True, "in_sortfield": True},
                },
                {
                    "name": "ocr_text", "type": "text", "label": "OCR text",
                    "description": "Text extracted from the image by OCR.",
                    "usage": {"in_filter": False, "in_searchfield": True, "in_sortfield": False},
                },
                {
                    "name": "created_at", "type": "date", "label": "Created at",
                    "usage": {"in_filter": False, "in_searchfield": False, "in_sortfield": True},
                },
                {
                    "name": "updated_at", "type": "date", "label": "Updated at",
                    "usage": {"in_filter": False, "in_searchfield": False, "in_sortfield": True},
                },
                {
                    "name": "is_enabled", "type": "presence", "label": "Enabled",
                    "values": [
                        {"value": "true", "label": "Enabled"},
                        {"value": "false", "label": "Disabled"},
                    ],
                    "usage": {"in_filter": True, "in_searchfield": False, "in_sortfield": False},
                },
            ],
        },
        {
            "id": "system",
            "attrs": [
                {
                    "name": "contain.text", "type": "extent", "label": "Text in the image",
                    "description": "How much body text the picture itself carries. Decides whether reading its text is worth a separate OCR pass.",
                    "values": [
                        {"value": "none", "label": "None", "description": "no text at all"},
                        {"value": "sparse", "label": "Sparse", "description": "a few words — a logo, a road sign, a single label"},
                        {"value": "block", "label": "Block", "description": "a block of body text: screenshot, table, document page"},
                    ],
                    "usage": {"in_filter": True, "in_searchfield": False, "in_sortfield": False},
                },
                {
                    "name": "contain.data_visual", "type": "presence", "label": "Data visual",
                    "description": "Whether the picture conveys data as a chart, graph, diagram or infographic. Such images keep their labels on the OCR path even when the text looks sparse.",
                    "values": [
                        {"value": "true", "label": "Yes", "description": "a chart, graph or diagram with plotted values"},
                        {"value": "false", "label": "No", "description": "a photo, drawing, icon or decoration"},
                    ],
                    "usage": {"in_filter": True, "in_searchfield": False, "in_sortfield": False},
                },
            ],
        },
    ]


def _gallery_policy(raw: Any) -> dict[str, Any]:
    value = loads(raw, {}) if not isinstance(raw, dict) else raw
    return value if isinstance(value, dict) else {}


def _resolve_gallery(ctx: Any, kb_id: str) -> dict[str, Any]:
    setting = db.fetch_one("SELECT value FROM system_settings WHERE key='gallery.policy'")
    system_tier = _gallery_policy(setting.get("value") if setting else None)
    user_tier = _gallery_policy((ctx.user.get("preferences") or "{}")).get("gallery") or {}
    if not isinstance(user_tier, dict):
        user_tier = {}
    tiers = [("system", system_tier), ("user", user_tier)]
    attributes: list[dict[str, Any]] = []
    source_ids: list[str] = []
    for source in _gallery_sources():
        enabled = True
        for name, tier in tiers:
            sources = tier.get("sources") if isinstance(tier.get("sources"), dict) else {}
            if source["id"] in sources:
                enabled = bool(sources[source["id"]])
        if not enabled:
            continue
        source_ids.append(source["id"])
        for definition in source["attrs"]:
            attr_id = f"{source['id']}:{definition['name']}"
            usage = dict(definition["usage"])
            usage_from = "source"
            for name, tier in tiers:
                overrides = tier.get("overrides") if isinstance(tier.get("overrides"), dict) else {}
                override = overrides.get(attr_id)
                if not isinstance(override, dict):
                    continue
                touched = False
                for key in ("in_filter", "in_searchfield", "in_sortfield"):
                    if key in override:
                        usage[key] = bool(override[key])
                        touched = True
                if touched:
                    usage_from = name
            item = {
                **definition,
                "id": attr_id,
                "source": source["id"],
                "usage": usage,
                "usage_from": usage_from,
            }
            attributes.append(item)
    status = user_tier.get("status") if isinstance(user_tier.get("status"), dict) else {}
    return {
        "attribute_sources": source_ids,
        "attributes": attributes,
        "mode": user_tier.get("mode") if user_tier.get("mode") in {"all", "custom"} else "all",
        "status": {key: value for key, value in status.items() if value in {"on", "off"}},
    }


def _gallery_eligibility(config: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    filterable: set[str] = set()
    searchable: set[str] = set()
    sortable: set[str] = set()
    for attr in config.get("attributes") or []:
        usage = attr.get("usage") or {}
        if usage.get("in_filter"):
            filterable.add(attr["id"])
        if usage.get("in_searchfield"):
            searchable.add(attr["id"])
        if usage.get("in_sortfield"):
            sortable.add(attr["id"])
    return filterable, searchable, sortable


def _gallery_value_sql(attr_id: str) -> tuple[str, bool]:
    builtin = {
        "builtin:caption": "i.caption",
        "builtin:ocr_text": "i.ocr_text",
        "builtin:is_enabled": "(CASE WHEN i.is_enabled THEN 'true' ELSE 'false' END)",
    }
    if attr_id in builtin:
        return builtin[attr_id], True
    if attr_id in _GALLERY_FIELD_ATTRS:
        name = attr_id.split(":", 1)[1]
        path = "'$.\"" + name + "\"'"
        return (
            "(CASE json_type(i.attrs, " + path + ") WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' "
            "ELSE CAST(json_extract(i.attrs, " + path + ") AS TEXT) END)",
            True,
        )
    return "", False


def _knowledge_tags(knowledge_id: str) -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT t.id, t.name, t.color, t.seq_id FROM knowledge_tag_relations r
        JOIN knowledge_tags t ON t.id=r.tag_id
        WHERE r.knowledge_id=? AND t.deleted_at IS NULL
        """,
        (knowledge_id,),
    )


def _set_tags(
    conn: Any,
    knowledge_id: str,
    tag_ids: list[str],
    *,
    tenant_id: int,
    kb_id: str,
) -> None:
    unique = list(dict.fromkeys(tag_id for tag_id in tag_ids if tag_id))
    if unique:
        marks = ",".join("?" for _ in unique)
        rows = conn.execute(
            f"SELECT id FROM knowledge_tags WHERE tenant_id=? AND knowledge_base_id=? "
            f"AND deleted_at IS NULL AND id IN ({marks})",
            (tenant_id, kb_id, *unique),
        ).fetchall()
        if len(rows) != len(unique):
            raise validation("Tag does not belong to the knowledge base")
    conn.execute("DELETE FROM knowledge_tag_relations WHERE knowledge_id=?", (knowledge_id,))
    for tag_id in unique:
        conn.execute(
            "INSERT OR IGNORE INTO knowledge_tag_relations(knowledge_id, tag_id, created_at) VALUES (?, ?, ?)",
            (knowledge_id, tag_id, db.utcnow()),
        )


def _insert_image_chunk(
    conn: Any,
    *,
    tenant_id: int,
    kb_id: str,
    knowledge_id: str,
    reference: str,
    caption: str,
    chunk_index: int,
    parent_id: str | None = None,
    start_at: int = 0,
    end_at: int = 0,
    extracted: bool = False,
) -> str:
    now = db.utcnow()
    chunk_id = db.new_id()
    image_info = {
        "url": reference,
        "original_url": reference,
        "caption": caption,
        "start_pos": start_at,
        "end_pos": end_at,
    }
    if extracted:
        image_info["extracted"] = True
    conn.execute(
        """
        INSERT INTO chunks
        (id, tenant_id, knowledge_base_id, knowledge_id, content, source_content,
         content_revision, index_status, last_editor_id, context_header,
         chunk_index, is_enabled, start_at, end_at, chunk_type, parent_chunk_id,
         image_info, status, content_hash, flags, created_at, updated_at)
        VALUES (?, ?, ?, ?, '', '', 1, 'indexed', '', '', ?, 1, ?, ?, 'image', ?,
                ?, 0, ?, 1, ?, ?)
        """,
        (
            chunk_id, tenant_id, kb_id, knowledge_id, chunk_index, start_at, end_at,
            parent_id,
            dumps([image_info]), hashlib.sha256(b"").hexdigest(), now, now,
        ),
    )
    return chunk_id


def _materialize_embedded_images(
    tenant_id: int,
    kb: dict[str, Any],
    knowledge_id: str,
    text: str,
    assets: list[dict[str, Any]],
) -> tuple[str, set[str]]:
    if not assets:
        return strip_embedded_markers(text), set()
    tenant = db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (tenant_id,))
    try:
        backend_id, provider, _config, _bucket, _region = object_storage.resolve_write_target(
            tenant_id,
            str(kb.get("storage_backend_id") or ""),
            _kb_storage_provider(kb, tenant or {}),
        )
    except Exception:  # noqa: BLE001 - derived images must not fail document parsing
        backend_id, provider = "", "local"
    target_dir = config.FILES_DIR / str(tenant_id) / knowledge_id / "embedded"
    resolved: dict[str, str] = {}
    for asset in assets:
        marker = str(asset.get("marker") or "")
        data = asset.get("data")
        if not marker or not isinstance(data, bytes):
            continue
        name = Path(str(asset.get("name") or "image.png")).name
        try:
            if provider in object_storage.OBJECT_SCHEMES:
                reference = object_storage.save_bytes(
                    data,
                    tenant_id,
                    "embedded-" + name,
                    backend_id=backend_id,
                    provider=provider,
                    knowledge_id=knowledge_id,
                    content_type=str(asset.get("mime_type") or ""),
                )
            else:
                target_dir.mkdir(parents=True, exist_ok=True)
                suffix = Path(name).suffix.lower() or ".bin"
                stored = target_dir / f"{db.new_id()}{suffix}"
                stored.write_bytes(data)
                reference = f"local://{tenant_id}/{knowledge_id}/embedded/{stored.name}"
            resolved[marker] = reference
        except Exception:  # noqa: BLE001 - image extraction must not fail document parsing
            continue
    for marker, reference in resolved.items():
        text = text.replace(marker, reference)
    return strip_embedded_markers(text), set(resolved.values())


def _extracted_image_references(
    knowledge_id: str,
    tenant_id: int,
    *,
    include_deleted: bool = False,
    all_images: bool = False,
) -> list[str]:
    references: list[str] = []
    rows = db.fetch_all(
        "SELECT image_info FROM chunks WHERE knowledge_id=? AND tenant_id=?"
        + ("" if include_deleted else " AND deleted_at IS NULL"),
        (knowledge_id, tenant_id),
    )
    for row in rows:
        images = loads(row.get("image_info"), [])
        if not isinstance(images, list):
            continue
        for image in images:
            if not isinstance(image, dict):
                continue
            if not all_images and image.get("extracted") is not True:
                continue
            reference = str(image.get("url") or "").strip()
            if reference:
                references.append(reference)
    return list(dict.fromkeys(references))


def _bind_knowledge_resources(
    tenant_id: int,
    knowledge_id: str,
    *,
    text: str = "",
    references: tuple[str, ...] | list[str] = (),
    extracted_images: set[str] | list[str] | tuple[str, ...] = (),
) -> None:
    """Best-effort catalog claims for the files a knowledge entry owns."""
    try:
        resource_catalog.bind_content_references(tenant_id, knowledge_id, text)
        resource_catalog.bind_references(tenant_id, knowledge_id, references)
        resource_catalog.bind_references(
            tenant_id,
            knowledge_id,
            extracted_images,
            relation=resource_catalog.RESOURCE_RELATION_EXTRACTED_IMAGE,
        )
    except Exception:  # noqa: BLE001 - claim failures must not discard saved content
        return


def _chunk_resource_text(chunks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        for field in ("content", "source_content", "image_info"):
            value = str(chunk.get(field) or "")
            if value:
                parts.append(value)
    return "\n".join(parts)


def _insert_chunks(
    conn: Any,
    tenant_id: int,
    kb_id: str,
    knowledge_id: str,
    text: str,
    chunk_size: int,
    overlap: int,
    extracted_images: set[str] | None = None,
) -> int:
    now = db.utcnow()
    extracted_images = extracted_images or set()
    pieces = chunk_with_offsets(text, chunk_size, overlap)
    inserted: list[tuple[str, dict[str, Any]]] = []
    for piece in pieces:
        chunk_id = db.new_id()
        conn.execute(
            """
            INSERT INTO chunks
            (id, tenant_id, knowledge_base_id, knowledge_id, content, source_content,
             content_revision, index_status, last_editor_id, context_header,
             chunk_index, is_enabled, start_at, end_at, chunk_type, status, flags,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'indexed', '', '', ?, 1, ?, ?, 'text', 0, 0, ?, ?)
            """,
            (
                chunk_id, tenant_id, kb_id, knowledge_id, piece["content"], piece["content"],
                piece["index"], piece["start_at"], piece["end_at"], now, now,
            ),
        )
        inserted.append((chunk_id, piece))
    next_index = len(pieces)
    for parent_id, piece in inserted:
        for image in image_vectors.extract_image_entries(piece["content"]):
            _insert_image_chunk(
                conn,
                tenant_id=tenant_id,
                kb_id=kb_id,
                knowledge_id=knowledge_id,
                reference=image["url"],
                caption=image["caption"],
                chunk_index=next_index,
                start_at=piece["start_at"] + image["start_pos"],
                end_at=piece["start_at"] + image["end_pos"],
                parent_id=parent_id,
                extracted=image["url"] in extracted_images,
            )
            next_index += 1
    return len(pieces)


def _refresh_knowledge_indexes(
    tenant_id: int,
    kb: dict[str, Any],
    knowledge: dict[str, Any],
    *,
    multimodal: bool = True,
) -> None:
    if multimodal:
        image_multimodal.process_knowledge_images(tenant_id, kb, knowledge)
    ingest_knowledge(kb, knowledge)
    image_vectors.index_knowledge_images(tenant_id, kb, knowledge)


def _summary_enabled(kb: dict[str, Any], metadata: dict[str, Any]) -> bool:
    overrides = metadata.get("process_overrides")
    if isinstance(overrides, dict) and isinstance(overrides.get("summary_enabled"), bool):
        return bool(overrides["summary_enabled"])
    value = kb.get("summary_enabled")
    return value if isinstance(value, bool) else True


def _summary_language(kb: dict[str, Any], accept_language: str = "") -> str:
    vlm_config = loads(kb.get("vlm_config"), {})
    if isinstance(vlm_config, dict):
        configured = str(vlm_config.get("description_language") or "").strip()
        if configured:
            return configured
    locale = resolve_locale(accept_language)
    return {
        "zh": "Chinese",
        "en": "English",
        "ja": "Japanese",
        "ko": "Korean",
        "ru": "Russian",
    }.get(locale.split("-", 1)[0].lower(), "English")


def _summary_source_text(value: str) -> bool:
    plain = re.sub(r"<image\b[^>]*>.*?</image>", " ", value, flags=re.I | re.S)
    plain = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", plain)
    plain = re.sub(r"(?:https?|local|storage)://\S+", " ", plain)
    return len(re.sub(r"\s+", "", plain)) >= SUMMARY_MIN_TEXT_CHARS


def _summary_chunks(tenant_id: int, knowledge_id: str) -> tuple[tuple[Any, ...], str, str]:
    rows = db.fetch_all(
        "SELECT id, content, content_revision, is_enabled, chunk_index "
        "FROM chunks WHERE knowledge_id=? AND tenant_id=? AND deleted_at IS NULL "
        "AND is_enabled=1 ORDER BY chunk_index, rowid",
        (knowledge_id, tenant_id),
    )
    signature = tuple(
        (
            str(row.get("id") or ""),
            str(row.get("content") or ""),
            int(row.get("content_revision") or 1),
            int(row.get("is_enabled") or 0),
            int(row.get("chunk_index") or 0),
        )
        for row in rows
    )
    first = next(
        (str(row.get("content") or "").strip() for row in rows if str(row.get("content") or "").strip()),
        "",
    )
    text = "\n\n".join(
        str(row.get("content") or "").strip()
        for row in rows
        if str(row.get("content") or "").strip()
    )
    return signature, text, first


def _dispatch_task_worker(task_id: str) -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            from anyio import from_thread

            from_thread.run_sync(schedule_task_worker, task_id)
            return True
        except Exception:  # noqa: BLE001 - a pending task is recovered on startup
            return False
    return schedule_task_worker(task_id)


def _queue_summary_generation(
    ctx: Any,
    kb: dict[str, Any],
    knowledge: dict[str, Any],
    text: str,
    *,
    accept_language: str = "",
) -> str:
    if str(knowledge.get("parse_status") or "") != "completed" or not _summary_source_text(text):
        return ""
    metadata = loads(knowledge.get("metadata"), {})
    if not isinstance(metadata, dict):
        metadata = {}
    if not _summary_enabled(kb, metadata):
        return ""
    payload = {
        "knowledge_id": str(knowledge["id"]),
        "knowledge_base_id": str(knowledge["knowledge_base_id"]),
        "language": _summary_language(kb, accept_language),
    }
    try:
        task_id = _create_task(ctx, SUMMARY_TASK_KIND, 1, payload)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE knowledges SET summary_status='pending', updated_at=? "
                "WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (db.utcnow(), knowledge["id"], ctx.tenant_id),
            )
    except Exception:  # noqa: BLE001 - summary enqueue must not fail document creation
        with db.transaction() as conn:
            conn.execute(
                "UPDATE knowledges SET summary_status='failed', updated_at=? "
                "WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (db.utcnow(), knowledge["id"], ctx.tenant_id),
            )
        return ""
    _dispatch_task_worker(task_id)
    return task_id


def _create_knowledge(
    ctx: Any,
    kb: dict[str, Any],
    *,
    title: str,
    text: str,
    knowledge_type: str,
    source: str,
    channel: str = "web",
    file_name: str = "",
    file_type: str = "",
    file_path: str = "",
    file_size: int = 0,
    file_hash: str = "",
    tag_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    folder_path: str = "",
    parse_status: str = "completed",
    error_message: str = "",
    image_reference: str = "",
    image_caption: str = "",
    knowledge_id: str = "",
    extracted_images: set[str] | None = None,
    accept_language: str = "",
) -> dict[str, Any]:
    now = db.utcnow()
    knowledge_id = knowledge_id or db.new_id()
    storage_size = storage_quota.knowledge_storage_size(file_size, text)
    quota_failed = False
    chunking = loads(kb.get("chunking_config"), {})
    chunk_size = int(chunking.get("chunk_size") or chunking.get("max_chunk_size") or 512)
    overlap = int(chunking.get("chunk_overlap") or chunking.get("overlap_size") or 50)
    with db.transaction() as conn:
        try:
            storage_quota.reserve_storage(conn, ctx.tenant_id, storage_size)
        except storage_quota.StorageQuotaExceeded:
            quota_failed = True
        effective_status = "failed" if quota_failed else parse_status
        effective_error = "存储空间不足" if quota_failed else error_message
        conn.execute(
            """
            INSERT INTO knowledges
            (id, tenant_id, knowledge_base_id, type, title, description, source,
             parse_status, enable_status, embedding_model_id, file_name, file_type,
             file_size, file_path, file_hash, storage_size, metadata, custom_metadata,
             summary_status, channel, created_at, updated_at, processed_at,
             error_message, folder_path, pending_subtasks_count, profile)
            VALUES (?, ?, ?, ?, ?, '', ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, '{}',
                    'none', ?, ?, ?, ?, ?, ?, 0, '{}')
            """,
            (
                knowledge_id, ctx.tenant_id, kb["id"], knowledge_type, title, source,
                effective_status, "disabled" if quota_failed else "enabled",
                file_name, file_type, file_size, file_path, file_hash,
                0 if quota_failed else storage_size, dumps(metadata or {}), channel,
                now, now, now, effective_error,
                folder_path,
            ),
        )
        chunk_count = 0
        if not quota_failed and text and parse_status == "completed":
            chunk_count = _insert_chunks(
                conn,
                ctx.tenant_id,
                kb["id"],
                knowledge_id,
                text,
                chunk_size,
                overlap,
                extracted_images,
            )
        if not quota_failed and image_reference and parse_status == "completed":
            _insert_image_chunk(
                conn,
                tenant_id=ctx.tenant_id,
                kb_id=str(kb["id"]),
                knowledge_id=knowledge_id,
                reference=image_reference,
                caption=image_caption,
                chunk_index=chunk_count,
            )
        _set_tags(conn, knowledge_id, tag_ids or [], tenant_id=ctx.tenant_id, kb_id=str(kb["id"]))
    if not quota_failed:
        _bind_knowledge_resources(
            ctx.tenant_id,
            knowledge_id,
            text=text,
            references=(image_reference,),
            extracted_images=extracted_images or (),
        )
    row = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
    if row and not quota_failed:
        _refresh_knowledge_indexes(ctx.tenant_id, kb, row)
        _queue_summary_generation(
            ctx, kb, row, text, accept_language=accept_language
        )
        row = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,)) or row
    return row


def _kb_guard(ctx: Any, minimum: str = "viewer") -> None:
    require_role(ctx, minimum)


def _validate_kb_transfer_compatibility(
    source: dict[str, Any],
    target: dict[str, Any],
    operation: str,
    mode: str,
    tenant: dict[str, Any],
) -> None:
    if str(source["id"]) == str(target["id"]):
        raise validation("source and target knowledge bases must differ")
    if str(source.get("type") or "") != str(target.get("type") or ""):
        raise validation("source and target knowledge bases must have the same type")
    if str(source.get("embedding_model_id") or "") != str(target.get("embedding_model_id") or ""):
        raise validation("source and target knowledge bases use different embedding models")
    if operation == "move" and mode not in {"reuse_vectors", "reparse"}:
        raise validation(f"unknown move mode: {mode}")
    source_vector_store = str(source.get("vector_store_id") or "").strip()
    target_vector_store = str(target.get("vector_store_id") or "").strip()
    if operation == "clone" or mode == "reuse_vectors":
        if source_vector_store != target_vector_store:
            raise validation(
                "source and target knowledge bases use different vector stores; use reparse mode for moves"
            )

    default_storage_id = str(tenant.get("default_storage_backend_id") or "").strip()
    source_storage_id = str(source.get("storage_backend_id") or "").strip() or default_storage_id
    target_storage_id = str(target.get("storage_backend_id") or "").strip() or default_storage_id
    if source_storage_id or target_storage_id:
        if not source_storage_id or source_storage_id != target_storage_id:
            raise validation("source and target knowledge bases use different storage instances")
    elif _kb_storage_provider(source, tenant) != _kb_storage_provider(target, tenant):
        raise validation("source and target knowledge bases use different storage instances")


def _kb_storage_provider(kb: dict[str, Any], tenant: dict[str, Any]) -> str:
    provider_config = loads(kb.get("storage_provider_config"), {})
    if isinstance(provider_config, dict):
        provider = str(provider_config.get("provider") or "").strip().lower()
        if provider and provider != "__pending_env__":
            return provider
    legacy_config = loads(kb.get("cos_config"), {})
    if isinstance(legacy_config, dict):
        provider = str(legacy_config.get("provider") or "").strip().lower()
        if provider:
            return provider
    tenant_config = loads(tenant.get("storage_engine_config"), {})
    if isinstance(tenant_config, dict):
        return str(tenant_config.get("default_provider") or "").strip().lower()
    return ""


def _create_task(ctx: Any, kind: str, total: int, payload: dict[str, Any]) -> str:
    task_id = db.new_id()
    now = db.utcnow()
    initial = {
        "task_id": task_id,
        "status": "pending",
        "progress": 0,
        "total": total,
        "processed": 0,
        "message": "任务已创建，等待处理",
    }
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_task_results(id, tenant_id, kind, status, total, processed, payload, result, error, max_retry, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, 0, ?, ?, '', ?, ?, ?)",
            (
                task_id,
                ctx.tenant_id,
                kind,
                total,
                dumps(payload),
                dumps(initial),
                TASK_WORKER_MAX_RETRIES,
                now,
                now,
            ),
        )
    return task_id


def _task_row(ctx: Any, task_id: str, kind: str, missing_message: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM py_task_results WHERE id=? AND tenant_id=? AND kind=?",
        (task_id, ctx.tenant_id, kind),
    )
    if not row:
        raise not_found(missing_message)
    return row


def _task_progress(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("status") or "pending")
    total = int(row.get("total") or 0)
    processed = int(row.get("processed") or 0)
    if status == "completed":
        progress = 100
    elif status == "processing" and total > 0:
        progress = min(99, int(processed * 100 / total))
    else:
        progress = 0
    result = loads(row.get("result"), {})
    if not isinstance(result, dict):
        result = {}
    return {
        "task_id": row["id"],
        "status": status,
        "progress": progress,
        "total": total,
        "processed": processed,
        "message": str(result.get("message") or row.get("error") or ""),
        "error": str(row.get("error") or ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _worker_task_stopped(task_id: str) -> bool:
    row = db.fetch_one(
        "SELECT status, worker_token FROM py_task_results WHERE id=?", (task_id,)
    )
    if not row or str(row.get("status") or "") not in {"pending", "processing"}:
        return True
    claim = _task_worker_claims.get(task_id, "")
    return bool(claim and str(row.get("worker_token") or "") != claim)


def _save_worker_progress(
    task_id: str,
    *,
    status: str,
    progress: dict[str, Any],
    error: str = "",
) -> None:
    claim = _task_worker_claims.get(task_id, "")
    assignments = "status=?, total=?, processed=?, result=?, error=?, updated_at=?"
    if status in {"completed", "failed", "cancelled"}:
        assignments += ", worker_token='', lease_until=''"
    query = (
        f"UPDATE py_task_results SET {assignments} "
        "WHERE id=? AND status IN ('pending','processing')"
    )
    params: list[Any] = [
        status,
        int(progress.get("total") or 0),
        int(progress.get("processed") or 0),
        dumps(progress),
        error,
        db.utcnow(),
        task_id,
    ]
    if claim:
        query += " AND worker_token=?"
        params.append(claim)
    with db.transaction() as conn:
        conn.execute(query, params)


def _claim_task_worker(task_id: str) -> str:
    token = db.new_id()
    now = time.time()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT kind, status, retry_after FROM py_task_results WHERE id=?", (task_id,)
        ).fetchone()
        if not row or str(row["status"]) not in {"pending", "processing"}:
            return ""
        if str(row["kind"]) not in TASK_WORKERS:
            return ""
        if str(row["status"]) == "pending" and str(row["retry_after"] or ""):
            try:
                if float(str(row["retry_after"])) > now:
                    return ""
            except ValueError:
                pass
        cursor = conn.execute(
            "UPDATE py_task_results SET worker_token=?, lease_until=?, retry_after='', updated_at=? "
            "WHERE id=? AND status IN ('pending','processing') "
            "AND (worker_token='' OR CAST(lease_until AS REAL)<=?) "
            "AND (status='processing' OR retry_after='' OR CAST(retry_after AS REAL)<=?)",
            (token, f"{now + TASK_WORKER_LEASE_SECONDS:.3f}", db.utcnow(), task_id, now, now),
        )
        return token if int(cursor.rowcount or 0) == 1 else ""


def _mark_task_worker_running(task_id: str, token: str) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE py_task_results SET status='processing', updated_at=? "
            "WHERE id=? AND worker_token=? AND status IN ('pending','processing')",
            (db.utcnow(), task_id, token),
        )
        return int(cursor.rowcount or 0) == 1


def _renew_task_worker_lease(task_id: str, token: str) -> bool:
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE py_task_results SET lease_until=? WHERE id=? AND worker_token=? AND status='processing'",
            (f"{time.time() + TASK_WORKER_LEASE_SECONDS:.3f}", task_id, token),
        )
        return int(cursor.rowcount or 0) == 1


def _release_task_worker_claim(task_id: str, token: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE py_task_results SET worker_token='', lease_until='' "
            "WHERE id=? AND worker_token=?",
            (task_id, token),
        )


def _schedule_task_worker_after(task_id: str, delay: float) -> None:
    async def run_later() -> None:
        await asyncio.sleep(max(0.0, delay))
        schedule_task_worker(task_id)

    task = asyncio.create_task(run_later())
    _knowledge_task_workers.add(task)
    task.add_done_callback(_knowledge_task_workers.discard)


def _save_worker_failure(
    task_id: str,
    *,
    progress: dict[str, Any],
    message: str,
    error: str,
) -> None:
    row = db.fetch_one(
        "SELECT kind, retry_count, max_retry, payload FROM py_task_results WHERE id=?",
        (task_id,),
    )
    if not row:
        return
    retry_count = int(row.get("retry_count") or 0)
    max_retry = int(row.get("max_retry") or 0)
    if max_retry <= 0 and str(row.get("kind") or "") == "faq_import":
        payload = loads(row.get("payload"), {})
        max_retry = 3 if isinstance(payload, dict) and payload.get("dry_run") else 5
    if max_retry <= 0 and str(row.get("kind") or "") in TASK_WORKERS:
        max_retry = TASK_WORKER_MAX_RETRIES
    if retry_count >= max_retry:
        progress.update({"status": "failed", "message": message, "error": error})
        _save_worker_progress(task_id, status="failed", progress=progress, error=error)
        return

    retry_count += 1
    delay = TASK_RETRY_BASE_SECONDS * retry_count * retry_count
    progress.update({
        "status": "pending",
        "message": f"{message}，{int(delay)} 秒后重试",
        "error": error,
    })
    claim = _task_worker_claims.get(task_id, "")
    query = (
        "UPDATE py_task_results SET status='pending', retry_count=?, max_retry=?, "
        "retry_after=?, result=?, error=?, updated_at=? "
        "WHERE id=? AND status IN ('pending','processing')"
    )
    params: list[Any] = [
        retry_count,
        max_retry,
        f"{time.time() + delay:.3f}",
        dumps(progress),
        error,
        db.utcnow(),
        task_id,
    ]
    if claim:
        query += " AND worker_token=?"
        params.append(claim)
    with db.transaction() as conn:
        conn.execute(query, params)


def schedule_task_worker(task_id: str) -> bool:
    token = _claim_task_worker(task_id)
    if not token:
        return False
    row = db.fetch_one("SELECT kind FROM py_task_results WHERE id=?", (task_id,))
    worker = TASK_WORKERS.get(str((row or {}).get("kind") or ""))
    if worker is None:
        _release_task_worker_claim(task_id, token)
        return False
    _task_worker_claims[task_id] = token

    async def run_worker() -> None:
        current = asyncio.current_task()

        async def renew_lease() -> None:
            while True:
                await asyncio.sleep(TASK_WORKER_RENEW_SECONDS)
                if not _renew_task_worker_lease(task_id, token):
                    if current is not None and not current.done():
                        current.cancel()
                    return

        if not _mark_task_worker_running(task_id, token):
            _task_worker_claims.pop(task_id, None)
            _release_task_worker_claim(task_id, token)
            return
        renewal = asyncio.create_task(renew_lease())
        try:
            with model_limiter.background():
                await worker(task_id)
        finally:
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            _task_worker_claims.pop(task_id, None)
            _release_task_worker_claim(task_id, token)
            row = db.fetch_one(
                "SELECT status, retry_after FROM py_task_results WHERE id=?", (task_id,)
            )
            if row and str(row.get("status") or "") == "pending" and str(row.get("retry_after") or ""):
                try:
                    delay = float(str(row["retry_after"])) - time.time()
                except ValueError:
                    delay = 0.0
                _schedule_task_worker_after(task_id, delay)

    task = asyncio.create_task(run_worker())
    _knowledge_task_workers.add(task)
    task.add_done_callback(_knowledge_task_workers.discard)
    return True


async def _run_kb_clone(task_id: str) -> None:
    row = db.fetch_one("SELECT * FROM py_task_results WHERE id=? AND kind='kb_clone'", (task_id,))
    if not row or str(row.get("status") or "") not in {"pending", "processing"}:
        return
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    tenant_id = int(row.get("tenant_id") or 0)
    source_id = str(payload.get("source_id") or "")
    target_id = str(payload.get("target_id") or "")
    create_target = bool(payload.get("create_target"))
    previous = loads(row.get("result"), {})
    if not isinstance(previous, dict):
        previous = {}
    copied_ids = {str(value) for value in previous.get("copied_ids") or []}
    progress: dict[str, Any] = {
        "task_id": task_id,
        "source_id": source_id,
        "target_id": target_id,
        "status": "processing",
        "progress": 0,
        "total": int(row.get("total") or 0),
        "processed": 0,
        "copied_ids": sorted(copied_ids),
        "message": "正在准备复制任务...",
    }
    _save_worker_progress(task_id, status="processing", progress=progress)
    await asyncio.sleep(0)
    if _worker_task_stopped(task_id):
        return
    try:
        source = db.fetch_one(
            "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (source_id, tenant_id),
        )
        if not source:
            raise validation("Knowledge base not found")
        target = db.fetch_one(
            "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (target_id, tenant_id),
        )
        if not target and create_target:
            values = dict(source)
            now = db.utcnow()
            values.update({
                "id": target_id,
                "name": f"{source['name']} 副本",
                "generated_profile": None,
                "is_temporary": 0,
                "is_pinned": 0,
                "pinned_at": None,
                "created_at": now,
                "updated_at": now,
                "creator_id": str(payload.get("creator_id") or ""),
            })
            values.pop("deleted_at", None)
            with db.transaction() as conn:
                db.insert("knowledge_bases", values, conn)
            target = db.fetch_one(
                "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (target_id, tenant_id),
            )
        if not target:
            raise not_found("Target knowledge base not found")
        if not create_target:
            tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
            _validate_kb_transfer_compatibility(source, target, "clone", "", tenant or {})

        knowledges = db.fetch_all(
            "SELECT * FROM knowledges WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL ORDER BY created_at, id",
            (source_id, tenant_id),
        )
        faq_entries = []
        if str(source.get("type") or "") == "faq":
            faq_entries = db.fetch_all(
                "SELECT * FROM py_faq_entries WHERE knowledge_base_id=? AND tenant_id=? "
                "AND deleted_at IS NULL ORDER BY id",
                (source_id, tenant_id),
            )
        progress["total"] = len(knowledges) + len(faq_entries)
        for original in knowledges:
            if _worker_task_stopped(task_id):
                return
            original_id = str(original["id"])
            if original_id not in copied_ids:
                knowledge_id = db.new_id()
                now = db.utcnow()
                values = dict(original)
                values.update({
                    "id": knowledge_id,
                    "knowledge_base_id": target_id,
                    "created_at": now,
                    "updated_at": now,
                })
                file_path, chunks, created = _prepare_cloned_knowledge(
                    original, target_id, knowledge_id
                )
                values["file_path"] = file_path
                if _worker_task_stopped(task_id):
                    _remove_cloned_files(created, tenant_id, [knowledge_id])
                    return
                try:
                    with db.transaction() as conn:
                        storage_quota.reserve_storage(
                            conn, tenant_id, int(values.get("storage_size") or 0)
                        )
                        db.insert("knowledges", values, conn)
                        for chunk in chunks:
                            chunk_values = dict(chunk)
                            chunk_values.update({
                                "knowledge_base_id": target_id,
                                "knowledge_id": knowledge_id,
                                "created_at": now,
                                "updated_at": now,
                            })
                            db.insert("chunks", chunk_values, conn)
                except Exception:
                    _remove_cloned_files(created, tenant_id, [knowledge_id])
                    raise
                _bind_knowledge_resources(
                    tenant_id,
                    knowledge_id,
                    text=_chunk_resource_text(chunks),
                )
                copied_ids.add(original_id)
            progress["processed"] += 1
            progress["copied_ids"] = sorted(copied_ids)
            progress["progress"] = min(99, int(progress["processed"] * 100 / max(1, progress["total"])))
            progress["message"] = "正在复制知识库内容..."
            _save_worker_progress(task_id, status="processing", progress=progress)
            await asyncio.sleep(0)

        if faq_entries:
            tag_map = _clone_faq_tags(source_id, target_id, tenant_id)
            for start in range(0, len(faq_entries), 50):
                if _worker_task_stopped(task_id):
                    return
                batch = faq_entries[start:start + 50]
                _clone_faq_entry_batch(batch, target_id, tenant_id, tag_map)
                progress["processed"] += len(batch)
                progress["progress"] = min(99, int(progress["processed"] * 100 / max(1, progress["total"])))
                progress["message"] = "正在复制 FAQ 条目..."
                _save_worker_progress(task_id, status="processing", progress=progress)
                await asyncio.sleep(0)

        if _worker_task_stopped(task_id):
            return
        progress.update({
            "status": "completed",
            "progress": 100,
            "message": "知识库复制完成",
        })
        _save_worker_progress(task_id, status="completed", progress=progress)
    except Exception as exc:  # noqa: BLE001
        if _worker_task_stopped(task_id):
            return
        _save_worker_failure(
            task_id,
            progress=progress,
            message="知识库复制失败",
            error=str(exc),
        )


async def _run_knowledge_move(task_id: str) -> None:
    row = db.fetch_one("SELECT * FROM py_task_results WHERE id=? AND kind='knowledge_move'", (task_id,))
    if not row or str(row.get("status") or "") not in {"pending", "processing"}:
        return
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    tenant_id = int(row.get("tenant_id") or 0)
    source_id = str(payload.get("source_kb_id") or "")
    target_id = str(payload.get("target_kb_id") or "")
    mode = str(payload.get("mode") or "reuse_vectors")
    ids = [str(value) for value in payload.get("knowledge_ids") or []]
    previous = loads(row.get("result"), {})
    if not isinstance(previous, dict):
        previous = {}
    moved_ids = {str(value) for value in previous.get("moved_ids") or []}
    progress: dict[str, Any] = {
        "task_id": task_id,
        "source_kb_id": source_id,
        "target_kb_id": target_id,
        "status": "processing",
        "progress": 0,
        "total": len(ids),
        "processed": 0,
        "failed": 0,
        "moved_ids": sorted(moved_ids),
        "message": "正在移动知识...",
    }
    _save_worker_progress(task_id, status="processing", progress=progress)
    await asyncio.sleep(0)
    if _worker_task_stopped(task_id):
        return
    try:
        target = db.fetch_one(
            "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (target_id, tenant_id),
        )
        if not target:
            raise not_found("Target knowledge base not found")
        if source_id:
            source = db.fetch_one(
                "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (source_id, tenant_id),
            )
            if not source:
                raise not_found("Source knowledge base not found")
            tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
            _validate_kb_transfer_compatibility(source, target, "move", str(payload.get("mode") or ""), tenant or {})
        for knowledge_id in ids:
            if _worker_task_stopped(task_id):
                return
            knowledge = db.fetch_one(
                "SELECT * FROM knowledges WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (knowledge_id, tenant_id),
            )
            if not knowledge:
                raise not_found(f"Knowledge {knowledge_id} not found")
            if str(knowledge.get("parse_status") or "") != "completed":
                raise validation(
                    f"Knowledge {knowledge_id} is not in completed status "
                    f"(current: {knowledge.get('parse_status') or ''})"
                )
            if source_id and str(knowledge.get("knowledge_base_id") or "") not in {source_id, target_id}:
                raise validation(f"Knowledge {knowledge_id} does not belong to the source knowledge base")
            source_chunk_ids = [
                str(row["id"])
                for row in db.fetch_all(
                    "SELECT id FROM chunks WHERE knowledge_id=? ORDER BY chunk_index",
                    (knowledge_id,),
                )
            ]
            now = db.utcnow()
            refresh_kb = target
            if mode == "reparse":
                effective_target = _merge_process_config(target, _knowledge_process_overrides(knowledge))
                refresh_kb = effective_target
                text, image_reference, image_caption, embedded_images = _knowledge_reparse_content(
                    effective_target, knowledge
                )
                old_extracted = _extracted_image_references(knowledge_id, tenant_id)
                text, extracted_images = _materialize_embedded_images(
                    tenant_id, effective_target, knowledge_id, text, embedded_images
                )
                has_text = bool(text.strip())
                has_content = has_text or bool(image_reference)
                new_storage_size = storage_quota.knowledge_storage_size(
                    int(knowledge.get("file_size") or 0), text
                )
                with db.transaction() as conn:
                    storage_quota.adjust_storage(
                        conn,
                        tenant_id,
                        int(knowledge.get("storage_size") or 0),
                        new_storage_size,
                    )
                    conn.execute(
                        "DELETE FROM knowledge_tag_relations WHERE knowledge_id=?",
                        (knowledge_id,),
                    )
                    conn.execute("DELETE FROM chunks WHERE knowledge_id=?", (knowledge_id,))
                    chunk_count = 0
                    if has_text:
                        chunking = loads(effective_target.get("chunking_config"), {})
                        chunk_size = int(
                            chunking.get("chunk_size") or chunking.get("max_chunk_size") or 512
                        )
                        overlap = int(
                            chunking.get("chunk_overlap") or chunking.get("overlap_size") or 50
                        )
                        chunk_count = _insert_chunks(
                            conn,
                            tenant_id,
                            target_id,
                            knowledge_id,
                            text,
                            chunk_size,
                            overlap,
                            extracted_images,
                        )
                    if image_reference:
                        _insert_image_chunk(
                            conn,
                            tenant_id=tenant_id,
                            kb_id=target_id,
                            knowledge_id=knowledge_id,
                            reference=image_reference,
                            caption=image_caption,
                            chunk_index=chunk_count,
                        )
                    conn.execute(
                        "UPDATE knowledges SET knowledge_base_id=?, embedding_model_id=?, "
                        "parse_status=?, error_message=?, storage_size=?, processed_at=?, updated_at=? "
                        "WHERE id=? AND tenant_id=?",
                        (
                            target_id,
                            str(target.get("embedding_model_id") or ""),
                            "completed" if has_content else "failed",
                            "" if has_content else "No extractable text found",
                            new_storage_size,
                            now,
                            now,
                            knowledge_id,
                            tenant_id,
                        ),
                    )
                if not has_content:
                    progress["failed"] += 1
                if has_content:
                    _bind_knowledge_resources(
                        tenant_id,
                        knowledge_id,
                        text=text,
                        references=(image_reference,),
                        extracted_images=extracted_images,
                    )
                _remove_cloned_files(old_extracted, tenant_id, [knowledge_id])
            else:
                with db.transaction() as conn:
                    conn.execute(
                        "DELETE FROM knowledge_tag_relations WHERE knowledge_id=?",
                        (knowledge_id,),
                    )
                    conn.execute(
                        "UPDATE knowledges SET knowledge_base_id=?, updated_at=? "
                        "WHERE id=? AND tenant_id=?",
                        (target_id, now, knowledge_id, tenant_id),
                    )
                    conn.execute(
                        "UPDATE chunks SET knowledge_base_id=? WHERE knowledge_id=?",
                        (target_id, knowledge_id),
                    )
            if source_id:
                remove_knowledge_source(source_id, tenant_id, knowledge_id, source_chunk_ids)
            updated = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
            if updated:
                _refresh_knowledge_indexes(
                    tenant_id,
                    refresh_kb,
                    updated,
                    multimodal=mode == "reparse",
                )
            moved_ids.add(knowledge_id)
            progress["processed"] += 1
            progress["moved_ids"] = sorted(moved_ids)
            progress["progress"] = min(99, int(progress["processed"] * 100 / max(1, progress["total"])))
            progress["message"] = "正在移动知识..."
            _save_worker_progress(task_id, status="processing", progress=progress)
            await asyncio.sleep(0)

        if _worker_task_stopped(task_id):
            return
        progress.update({
            "status": "completed",
            "progress": 100,
            "message": "知识移动完成",
        })
        _save_worker_progress(task_id, status="completed", progress=progress)
    except Exception as exc:  # noqa: BLE001
        if _worker_task_stopped(task_id):
            return
        _save_worker_failure(
            task_id,
            progress=progress,
            message="知识移动失败",
            error=str(exc),
        )


async def _run_knowledge_reparse(task_id: str) -> None:
    row = db.fetch_one("SELECT * FROM py_task_results WHERE id=? AND kind='knowledge_reparse'", (task_id,))
    if not row or str(row.get("status") or "") not in {"pending", "processing"}:
        return
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    tenant_id = int(row.get("tenant_id") or 0)
    ids = [str(value) for value in payload.get("knowledge_ids") or []]
    progress: dict[str, Any] = {
        "task_id": task_id,
        "kb_id": str(payload.get("kb_id") or ""),
        "status": "processing",
        "progress": 0,
        "total": len(ids),
        "processed": 0,
        "failed": 0,
        "failed_ids": [],
        "message": "正在重新解析知识...",
    }
    _save_worker_progress(task_id, status="processing", progress=progress)
    await asyncio.sleep(0)
    if _worker_task_stopped(task_id):
        return
    try:
        for knowledge_id in ids:
            if _worker_task_stopped(task_id):
                return
            knowledge = db.fetch_one(
                "SELECT * FROM knowledges WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (knowledge_id, tenant_id),
            )
            if not knowledge:
                raise not_found(f"Knowledge {knowledge_id} not found")
            knowledge_kb_id = str(knowledge["knowledge_base_id"])
            if str(payload.get("kb_id") or "") and knowledge_kb_id != str(payload.get("kb_id")):
                raise validation(f"Knowledge {knowledge_id} does not belong to the knowledge base")
            kb = db.fetch_one(
                "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (knowledge_kb_id, tenant_id),
            )
            if not kb:
                raise not_found(f"Knowledge base {knowledge_kb_id} not found")
            effective_kb = _merge_process_config(kb, _knowledge_process_overrides(knowledge))
            text, image_reference, image_caption, embedded_images = _knowledge_reparse_content(
                effective_kb, knowledge
            )
            old_extracted = _extracted_image_references(knowledge_id, tenant_id)
            text, extracted_images = _materialize_embedded_images(
                tenant_id, effective_kb, knowledge_id, text, embedded_images
            )
            now = db.utcnow()
            if not text.strip() and not image_reference:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE knowledges SET parse_status='failed', error_message=?, processed_at=?, updated_at=? "
                        "WHERE id=? AND tenant_id=?",
                        ("No extractable text found", now, now, knowledge_id, tenant_id),
                    )
                progress["failed"] += 1
                progress["failed_ids"] = [*progress["failed_ids"], knowledge_id]
            else:
                new_storage_size = storage_quota.knowledge_storage_size(
                    int(knowledge.get("file_size") or 0), text
                )
                chunking = loads(effective_kb.get("chunking_config"), {})
                chunk_size = int(chunking.get("chunk_size") or chunking.get("max_chunk_size") or 512)
                overlap = int(chunking.get("chunk_overlap") or chunking.get("overlap_size") or 50)
                with db.transaction() as conn:
                    storage_quota.adjust_storage(
                        conn,
                        tenant_id,
                        int(knowledge.get("storage_size") or 0),
                        new_storage_size,
                    )
                    conn.execute("DELETE FROM chunks WHERE knowledge_id=?", (knowledge_id,))
                    chunk_count = 0
                    if text.strip():
                        chunk_count = _insert_chunks(
                            conn,
                            tenant_id,
                            knowledge_kb_id,
                            knowledge_id,
                            text,
                            chunk_size,
                            overlap,
                            extracted_images,
                        )
                    if image_reference:
                        _insert_image_chunk(
                            conn,
                            tenant_id=tenant_id,
                            kb_id=knowledge_kb_id,
                            knowledge_id=knowledge_id,
                            reference=image_reference,
                            caption=image_caption,
                            chunk_index=chunk_count,
                        )
                    conn.execute(
                        "UPDATE knowledges SET parse_status='completed', storage_size=?, processed_at=?, updated_at=?, error_message='' "
                        "WHERE id=? AND tenant_id=?",
                        (new_storage_size, now, now, knowledge_id, tenant_id),
                    )
                updated = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
                if updated:
                    _bind_knowledge_resources(
                        tenant_id,
                        knowledge_id,
                        text=text,
                        references=(image_reference,),
                        extracted_images=extracted_images,
                    )
                    _refresh_knowledge_indexes(tenant_id, effective_kb, updated)
                    _queue_summary_generation(
                        SimpleNamespace(tenant_id=tenant_id), effective_kb, updated, text
                    )
                _remove_cloned_files(old_extracted, tenant_id, [knowledge_id])
            progress["processed"] += 1
            progress["progress"] = min(99, int(progress["processed"] * 100 / max(1, progress["total"])))
            progress["message"] = "正在重新解析知识..."
            _save_worker_progress(task_id, status="processing", progress=progress)
            await asyncio.sleep(0)

        if _worker_task_stopped(task_id):
            return
        failed = int(progress["failed"])
        progress.update({
            "status": "completed",
            "progress": 100,
            "message": f"批量重解析完成，{failed} 个知识处理失败" if failed else "批量重解析完成",
        })
        _save_worker_progress(task_id, status="completed", progress=progress)
    except Exception as exc:  # noqa: BLE001
        if _worker_task_stopped(task_id):
            return
        _save_worker_failure(
            task_id,
            progress=progress,
            message="批量重解析失败",
            error=str(exc),
        )


def _finish_summary_failure(
    task_id: str,
    row: dict[str, Any],
    tenant_id: int,
    knowledge_id: str,
    fallback: str,
    error: str,
    *,
    retry: bool = True,
) -> None:
    retry_count = int(row.get("retry_count") or 0)
    max_retry = int(row.get("max_retry") or 0) or TASK_WORKER_MAX_RETRIES
    will_retry = retry and retry_count < max_retry
    progress = {
        "task_id": task_id,
        "knowledge_id": knowledge_id,
        "status": "pending" if will_retry else "failed",
        "progress": 0,
        "total": 1,
        "processed": 0,
        "message": "摘要生成失败，等待重试" if will_retry else "摘要生成失败",
        "error": error,
    }
    if will_retry:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE knowledges SET summary_status='pending', updated_at=? "
                "WHERE id=? AND tenant_id=? AND summary_status='processing' AND deleted_at IS NULL",
                (db.utcnow(), knowledge_id, tenant_id),
            )
        _save_worker_failure(
            task_id,
            progress=progress,
            message="摘要生成失败",
            error=error,
        )
        return
    description = fallback.strip()[:SUMMARY_FALLBACK_CHARS]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE knowledges SET description=?, summary_status='failed', updated_at=? "
            "WHERE id=? AND tenant_id=? AND summary_status='processing' AND deleted_at IS NULL",
            (description, db.utcnow(), knowledge_id, tenant_id),
        )
    progress.update({"status": "failed", "error": error})
    _save_worker_progress(task_id, status="failed", progress=progress, error=error)


async def _run_summary_generation(task_id: str) -> None:
    row = db.fetch_one(
        "SELECT * FROM py_task_results WHERE id=? AND kind=?",
        (task_id, SUMMARY_TASK_KIND),
    )
    if not row or str(row.get("status") or "") not in {"pending", "processing"}:
        return
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    tenant_id = int(row.get("tenant_id") or 0)
    knowledge_id = str(payload.get("knowledge_id") or "")
    if not tenant_id or not knowledge_id:
        _save_worker_progress(
            task_id,
            status="failed",
            progress={"task_id": task_id, "status": "failed", "progress": 0, "total": 1, "processed": 0, "message": "摘要任务参数无效"},
            error="invalid summary task payload",
        )
        return
    knowledge = db.fetch_one(
        "SELECT * FROM knowledges WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (knowledge_id, tenant_id),
    )
    if not knowledge or str(knowledge.get("parse_status") or "") != "completed":
        _save_worker_progress(
            task_id,
            status="completed",
            progress={"task_id": task_id, "knowledge_id": knowledge_id, "status": "completed", "progress": 100, "total": 1, "processed": 1, "message": "文档已取消或删除，跳过摘要"},
        )
        return
    if str(knowledge.get("summary_status") or "") not in {"pending", "processing"}:
        _save_worker_progress(
            task_id,
            status="completed",
            progress={"task_id": task_id, "knowledge_id": knowledge_id, "status": "completed", "progress": 100, "total": 1, "processed": 1, "message": "摘要已由其他任务完成"},
        )
        return

    signature, source, fallback = _summary_chunks(tenant_id, knowledge_id)
    if not _summary_source_text(source):
        with db.transaction() as conn:
            conn.execute(
                "UPDATE knowledges SET description='', summary_status='failed', updated_at=? "
                "WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (db.utcnow(), knowledge_id, tenant_id),
            )
        _save_worker_progress(
            task_id,
            status="completed",
            progress={"task_id": task_id, "knowledge_id": knowledge_id, "status": "completed", "progress": 100, "total": 1, "processed": 1, "message": "文档没有可摘要的文本"},
        )
        return

    metadata_version = str(knowledge.get("custom_metadata") or "")
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE knowledges SET summary_status='processing', updated_at=? "
            "WHERE id=? AND tenant_id=? AND parse_status='completed' AND deleted_at IS NULL "
            "AND summary_status IN ('pending','processing')",
            (db.utcnow(), knowledge_id, tenant_id),
        )
        claimed = int(cursor.rowcount or 0) == 1
    if not claimed:
        _save_worker_progress(
            task_id,
            status="completed",
            progress={"task_id": task_id, "knowledge_id": knowledge_id, "status": "completed", "progress": 100, "total": 1, "processed": 1, "message": "摘要任务已失效"},
        )
        return

    progress = {
        "task_id": task_id,
        "knowledge_id": knowledge_id,
        "status": "processing",
        "progress": 0,
        "total": 1,
        "processed": 0,
        "message": "正在生成文档摘要...",
    }
    _save_worker_progress(task_id, status="processing", progress=progress)
    await asyncio.sleep(0)
    if _worker_task_stopped(task_id):
        return

    kb = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (str(payload.get("knowledge_base_id") or knowledge["knowledge_base_id"]), tenant_id),
    )
    if not kb:
        _finish_summary_failure(task_id, row, tenant_id, knowledge_id, fallback, "知识库不存在", retry=False)
        return
    language = str(payload.get("language") or _summary_language(kb))
    runtime = model_runtime.chat_runtime(
        tenant_id,
        {"model_id": str(kb.get("summary_model_id") or "")},
    )
    if not model_runtime.available(runtime):
        _finish_summary_failure(task_id, row, tenant_id, knowledge_id, fallback, "summary model is unavailable", retry=False)
        return
    try:
        description = document_summary.generate_summary(
            runtime,
            source,
            language=language,
            custom_metadata=str(knowledge.get("custom_metadata") or ""),
        )
        if not description:
            raise RuntimeError("summary model returned no usable summary")
    except Exception as exc:  # noqa: BLE001 - task retry/fallback is handled below
        _finish_summary_failure(task_id, row, tenant_id, knowledge_id, fallback, str(exc))
        return

    if _worker_task_stopped(task_id):
        return
    latest_signature, _, _ = _summary_chunks(tenant_id, knowledge_id)
    latest = db.fetch_one(
        "SELECT custom_metadata, summary_status, parse_status FROM knowledges "
        "WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (knowledge_id, tenant_id),
    )
    if (
        latest_signature != signature
        or not latest
        or str(latest.get("custom_metadata") or "") != metadata_version
        or str(latest.get("summary_status") or "") != "processing"
        or str(latest.get("parse_status") or "") != "completed"
    ):
        _save_worker_progress(
            task_id,
            status="completed",
            progress={"task_id": task_id, "knowledge_id": knowledge_id, "status": "completed", "progress": 100, "total": 1, "processed": 1, "message": "文档内容已变化，丢弃过期摘要"},
        )
        return

    description = description.strip()[:SUMMARY_MAX_DESCRIPTION_CHARS]
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE knowledges SET description=?, summary_status='completed', updated_at=? "
            "WHERE id=? AND tenant_id=? AND summary_status='processing' AND parse_status='completed' AND deleted_at IS NULL",
            (description, db.utcnow(), knowledge_id, tenant_id),
        )
        saved = int(cursor.rowcount or 0) == 1
    if not saved:
        _save_worker_progress(
            task_id,
            status="completed",
            progress={"task_id": task_id, "knowledge_id": knowledge_id, "status": "completed", "progress": 100, "total": 1, "processed": 1, "message": "摘要任务已失效"},
        )
        return
    progress.update({"status": "completed", "progress": 100, "processed": 1, "message": "文档摘要生成完成"})
    _save_worker_progress(task_id, status="completed", progress=progress)


TASK_WORKERS = {
    "kb_clone": _run_kb_clone,
    "knowledge_move": _run_knowledge_move,
    "knowledge_reparse": _run_knowledge_reparse,
    SUMMARY_TASK_KIND: _run_summary_generation,
}


def recover_pending_task_workers() -> int:
    kinds = tuple(TASK_WORKERS)
    if not kinds:
        return 0
    rows = db.fetch_all(
        "SELECT id, status, retry_after, lease_until FROM py_task_results WHERE kind IN ("
        + ",".join("?" for _ in kinds)
        + ") AND status IN ('pending','processing') ORDER BY created_at, id",
        kinds,
    )
    recovered = 0
    now = time.time()
    for row in rows:
        task_id = str(row["id"])
        delays: list[float] = []
        try:
            delays.append(float(str(row.get("retry_after") or "0")) - now)
        except ValueError:
            pass
        if str(row.get("status") or "") == "processing":
            try:
                delays.append(float(str(row.get("lease_until") or "0")) - now)
            except ValueError:
                pass
        delay = max(delays, default=0.0)
        if delay > 0:
            _schedule_task_worker_after(task_id, delay + 0.05)
            recovered += 1
        elif schedule_task_worker(task_id):
            recovered += 1
    return recovered


@router.get("/knowledge-bases")
def list_knowledge_bases(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    creator = request.query_params.get("creator", "all")
    sql = "SELECT * FROM knowledge_bases WHERE tenant_id=? AND deleted_at IS NULL"
    params: list[Any] = [ctx.tenant_id]
    if creator == "mine":
        sql += " AND creator_id=?"
        params.append(ctx.user_id)
    elif creator == "others":
        sql += " AND COALESCE(creator_id, '')<>'' AND creator_id<>?"
        params.append(ctx.user_id)
    if ctx.api_key is not None and ctx.api_key.is_kb_restricted:
        sql += " AND id IN (" + ",".join("?" for _ in ctx.api_key.knowledge_base_ids) + ")"
        params.extend(ctx.api_key.knowledge_base_ids)
    sql += " ORDER BY is_pinned DESC, updated_at DESC"
    rows = db.fetch_all(sql, params)
    return ok([serialize_kb(row, ctx.user_id) for row in rows])


@router.post("/knowledge-bases", status_code=201)
async def create_knowledge_base(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb_guard(ctx, "contributor")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("Knowledge base name is required")
    kb_type = data.get("type") if data.get("type") in {"document", "faq"} else "document"
    kb_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO knowledge_bases
            (id, name, description, tenant_id, type, chunking_config,
             image_processing_config, embedding_model_id, summary_model_id,
             cos_config, storage_provider_config, vlm_config, extract_config,
             faq_config, question_generation_config, wiki_config,
             indexing_strategy, auto_tag_config, profile_config, asr_config,
             vector_store_id, storage_backend_id, creator_id, created_at,
             updated_at, is_temporary, is_pinned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                kb_id, name, str(data.get("description") or ""), ctx.tenant_id, kb_type,
                dumps(data.get("chunking_config") or {}),
                dumps(data.get("image_processing_config") or {}),
                str(data.get("embedding_model_id") or ""),
                str(data.get("summary_model_id") or ""),
                dumps(data.get("storage_provider_config")) if data.get("storage_provider_config") is not None else None,
                dumps(data.get("vlm_config") or {}),
                dumps(data.get("extract_config")) if data.get("extract_config") is not None else None,
                dumps(data.get("faq_config")) if data.get("faq_config") is not None else None,
                dumps(data.get("question_generation_config")) if data.get("question_generation_config") is not None else None,
                dumps(data.get("wiki_config")) if data.get("wiki_config") is not None else None,
                dumps(data.get("indexing_strategy") or {"vector_enabled": True, "keyword_enabled": True, "wiki_enabled": False, "graph_enabled": False}),
                dumps(data.get("auto_tag_config")) if data.get("auto_tag_config") is not None else None,
                dumps(data.get("profile_config")) if data.get("profile_config") is not None else None,
                dumps(data.get("asr_config")) if data.get("asr_config") is not None else None,
                data.get("vector_store_id") or None,
                data.get("storage_backend_id") or None,
                ctx.user_id, now, now,
            ),
        )
    row = _get_kb(ctx, kb_id)
    return ok(serialize_kb(row, ctx.user_id))


@router.get("/knowledge-bases/move-targets")
def move_targets(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    sql = "SELECT * FROM knowledge_bases WHERE tenant_id=? AND deleted_at IS NULL"
    params: list[Any] = [ctx.tenant_id]
    if ctx.api_key is not None and ctx.api_key.is_kb_restricted:
        sql += " AND id IN (" + ",".join("?" for _ in ctx.api_key.knowledge_base_ids) + ")"
        params.extend(ctx.api_key.knowledge_base_ids)
    rows = db.fetch_all(sql + " ORDER BY name", params)
    return ok([serialize_kb(row, ctx.user_id) for row in rows])


@router.post("/knowledge-bases/copy")
async def copy_knowledge_base(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb_guard(ctx, "contributor")
    data = await body(request)
    source_id = str(data.get("source_id") or "").strip()
    if not source_id:
        raise validation("source_id is required")
    source = _get_kb(ctx, source_id)
    requested_target_id = str(data.get("target_id") or "").strip()
    create_target = not requested_target_id
    if requested_target_id:
        target = _get_writable_kb(ctx, requested_target_id)
        _validate_kb_transfer_compatibility(source, target, "clone", "", ctx.tenant)
        target_id = str(target["id"])
    else:
        target_id = db.new_id()
    knowledges = db.fetch_all(
        "SELECT * FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
        (source["id"],),
    )
    faq_count = 0
    if str(source.get("type") or "") == "faq":
        faq_count = int(db.fetch_one(
            "SELECT COUNT(*) AS n FROM py_faq_entries WHERE knowledge_base_id=? AND tenant_id=? "
            "AND deleted_at IS NULL",
            (source["id"], ctx.tenant_id),
        )["n"])
    task_id = _create_task(
        ctx,
        "kb_clone",
        len(knowledges) + faq_count,
        {
            "source_id": source["id"],
            "target_id": target_id,
            "create_target": create_target,
            "creator_id": ctx.user_id,
        },
    )
    schedule_task_worker(task_id)
    return ok({
        "task_id": task_id,
        "source_id": source["id"],
        "target_id": target_id,
        "message": "Knowledge base copy task started",
    })


def _clone_kb_row(ctx: Any, source: dict[str, Any]) -> dict[str, Any]:
    kb_id = db.new_id()
    now = db.utcnow()
    values = dict(source)
    values.update({
        "id": kb_id,
        "name": f"{source['name']} 副本",
        "generated_profile": None,
        "is_temporary": 0,
        "is_pinned": 0,
        "pinned_at": None,
        "created_at": now,
        "updated_at": now,
        "creator_id": ctx.user_id,
    })
    values.pop("deleted_at", None)
    with db.transaction() as conn:
        db.insert("knowledge_bases", values, conn)
    return _get_kb(ctx, kb_id)


@router.get("/knowledge-bases/{kb_id}")
def get_knowledge_base(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(serialize_kb(_get_readable_kb(ctx, kb_id), ctx.user_id))


@router.get("/knowledge-bases/{kb_id}/gallery-config")
def get_gallery_config(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_readable_kb(ctx, kb_id)
    return ok(_resolve_gallery(ctx, kb_id))


@router.get("/knowledge-bases/{kb_id}/images")
def list_gallery_images(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    config = _resolve_gallery(ctx, kb_id)
    filterable, searchable, sortable = _gallery_eligibility(config)
    page_no, page_size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 1000)

    clauses = [
        "i.knowledge_base_id=?",
        "i.tenant_id=?",
        (
            "NOT EXISTS (SELECT 1 FROM chunk_images o "
            "WHERE o.knowledge_base_id=i.knowledge_base_id AND o.tenant_id=i.tenant_id "
            "AND o.image_key=i.image_key AND (o.updated_at>i.updated_at "
            "OR (o.updated_at=i.updated_at AND (o.chunk_id<i.chunk_id "
            "OR (o.chunk_id=i.chunk_id AND o.image_index<i.image_index)))))"
        ),
    ]
    args: list[Any] = [kb_id, int(kb["tenant_id"])]
    filtered = False

    enabled_raw = request.query_params.get("is_enabled")
    if enabled_raw not in (None, ""):
        clauses.append("i.is_enabled=?")
        args.append(1 if truthy(enabled_raw) else 0)
        filtered = True

    keyword = (request.query_params.get("keyword") or "").strip()
    if keyword:
        fields = [
            value.strip()
            for value in (request.query_params.get("search_in") or "").split(",")
            if value.strip() in searchable
        ]
        if not fields:
            fields = ["builtin:caption", "builtin:ocr_text"]
        matches: list[str] = []
        pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        for field in fields:
            expr, ok_field = _gallery_value_sql(field)
            if ok_field:
                matches.append(expr + " LIKE ? ESCAPE '\\'")
                args.append(pattern)
        clauses.append("(" + (" OR ".join(matches) if matches else "1=0") + ")")
        filtered = True

    raw_filters = request.query_params.get("attr_filters") or ""
    attr_filters = loads(raw_filters, {}) if raw_filters else {}
    if isinstance(attr_filters, dict):
        for attr_id, values in attr_filters.items():
            if attr_id not in filterable or not isinstance(values, list) or not values:
                continue
            expr, ok_field = _gallery_value_sql(attr_id)
            if not ok_field:
                continue
            placeholders = ",".join("?" for _ in values)
            clauses.append(f"({expr} IS NULL OR {expr} IN ({placeholders}))")
            args.extend(str(value) for value in values)
            filtered = True

    raw_rules = request.query_params.get("attr_rules") or ""
    attr_rules = loads(raw_rules, {}) if raw_rules else {}
    off_clauses: list[str] = []
    off_args: list[Any] = []
    on_clauses: list[str] = []
    on_args: list[Any] = []
    if isinstance(attr_rules, dict):
        for attr_id, verdicts in attr_rules.items():
            if attr_id not in filterable or not isinstance(verdicts, dict):
                continue
            expr, ok_field = _gallery_value_sql(attr_id)
            if not ok_field:
                continue
            for verdict, values in (("on", []), ("off", [])):
                values = [value for value, decision in verdicts.items() if decision == verdict]
                if not values:
                    continue
                clause = f"({expr} IS NOT NULL AND {expr} IN ({','.join('?' for _ in values)}))"
                if verdict == "on":
                    on_clauses.append(clause)
                    on_args.extend(str(value) for value in values)
                else:
                    off_clauses.append(clause)
                    off_args.extend(str(value) for value in values)
    if off_clauses:
        on_sql = " OR ".join(on_clauses) if on_clauses else "1=0"
        clauses.append(f"(({on_sql}) OR NOT ({' OR '.join(off_clauses)}))")
        args.extend([*on_args, *off_args])
        filtered = True

    sort_by = (request.query_params.get("sort_by") or "").strip()
    legacy_sort = {
        "created_at": "builtin:created_at",
        "updated_at": "builtin:updated_at",
        "caption": "builtin:caption",
        "ocr_text": "builtin:ocr_text",
        "is_enabled": "builtin:is_enabled",
    }
    sort_by = legacy_sort.get(sort_by, sort_by)
    if sort_by not in sortable:
        sort_by = "builtin:created_at"
    direction = "ASC" if (request.query_params.get("sort_order") or "").lower() == "asc" else "DESC"
    if sort_by in {"builtin:created_at", "builtin:updated_at", "builtin:is_enabled"}:
        sort_expr = "i." + sort_by.split(":", 1)[1]
    else:
        value_expr, ok_field = _gallery_value_sql(sort_by)
        sort_expr = f"LOWER(COALESCE({value_expr}, ''))" if ok_field else "i.created_at"

    where = " AND ".join(clauses)
    total = int(db.fetch_one(f"SELECT COUNT(*) AS n FROM chunk_images i WHERE {where}", args)["n"])
    rows = db.fetch_all(
        f"""
        SELECT i.*, COALESCE(k.title, k.file_name, '') AS source_name
        FROM chunk_images i
        LEFT JOIN knowledges k ON k.id=i.knowledge_id
        WHERE {where}
        ORDER BY {sort_expr} {direction}, i.chunk_id {direction}, i.image_index {direction}
        LIMIT ? OFFSET ?
        """,
        [*args, page_size, offset],
    )
    items = []
    for row in rows:
        items.append(
            {
                "id": f"{row['chunk_id']}#{row['image_index']}",
                "chunk_id": row["chunk_id"],
                "knowledge_id": row["knowledge_id"],
                "source_name": row.get("source_name") or "",
                "chunk_type": row.get("chunk_type") or "",
                "url": row.get("url") or "",
                "original_url": row.get("original_url") or "",
                "caption": row.get("caption") or "",
                "ocr_text": row.get("ocr_text") or "",
                "attrs": loads(row.get("attrs"), {}),
                "is_enabled": bool(row.get("is_enabled")),
                "status": int(row.get("status") or 0),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return {"success": True, "data": items, "total": total, "page": page_no, "page_size": page_size}


def _vlm_config_value(config: dict[str, Any], *names: str) -> str:
    for name in names:
        value = config.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _clear_legacy_vlm_fields(config: dict[str, Any]) -> None:
    for key in ("model_name", "base_url", "api_key", "interface_type"):
        config[key] = ""
    for key in ("modelName", "baseUrl", "apiKey", "interfaceType"):
        config.pop(key, None)


def _resolve_updated_vlm_config(
    tenant_id: int, current: dict[str, Any], requested: dict[str, Any]
) -> dict[str, Any]:
    current_model_id = _vlm_config_value(current, "model_id", "modelId").strip()
    current_model_name = _vlm_config_value(current, "model_name", "modelName")
    current_base_url = _vlm_config_value(current, "base_url", "baseUrl")
    result = {
        "enabled": bool(current.get("enabled")),
        "model_id": current_model_id,
        "description_language": _vlm_config_value(current, "description_language", "descriptionLanguage"),
        "custom_instructions": _vlm_config_value(current, "custom_instructions", "customInstructions"),
        "model_name": current_model_name,
        "base_url": current_base_url,
        "api_key": _vlm_config_value(current, "api_key", "apiKey"),
        "interface_type": _vlm_config_value(current, "interface_type", "interfaceType"),
    }
    result["enabled"] = bool(requested.get("enabled"))
    result["model_id"] = _vlm_config_value(requested, "model_id", "modelId").strip()
    result["description_language"] = _vlm_config_value(
        requested, "description_language", "descriptionLanguage"
    ).strip()
    result["custom_instructions"] = _vlm_config_value(
        requested, "custom_instructions", "customInstructions"
    ).strip()
    if len(result["custom_instructions"]) > 4000:
        raise validation("vlm_config: image instructions exceeds 4000 characters")

    pure_legacy = not current_model_id and bool(current_model_name and current_base_url)
    if not result["enabled"]:
        result["model_id"] = ""
        legacy_echo = (
            pure_legacy
            and _vlm_config_value(requested, "model_name", "modelName") == current_model_name
            and _vlm_config_value(requested, "base_url", "baseUrl") == current_base_url
        )
        if not legacy_echo:
            _clear_legacy_vlm_fields(result)
        return result

    if not result["model_id"]:
        if not pure_legacy:
            raise validation("vlm_config.model_id is required when enabled")
        return result

    model = model_runtime.visible_model(tenant_id, result["model_id"])
    if not model:
        raise validation("vlm_config.model_id: model not found")
    model_type = str(model.get("type") or "")
    if model_type != "VLLM":
        raise validation(f"vlm_config.model_id: model type is {model_type}, want VLLM")
    _clear_legacy_vlm_fields(result)
    return result


@router.put("/knowledge-bases/{kb_id}")
async def update_knowledge_base(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    _require_kb_write_access(ctx, kb)
    data = await body(request)
    if (
        data.get("vlm_config") is not None
        and int(kb.get("tenant_id") or 0) != ctx.tenant_id
        and knowledge_base_share_permission(ctx.tenant_id, kb_id, ctx.role) != "admin"
    ):
        raise forbidden("Knowledge base settings require admin share access")
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise validation("Knowledge base name is required")
        updates["name"] = name
    if "description" in data:
        updates["description"] = str(data.get("description") or "")
    config_data = data.get("config") if isinstance(data.get("config"), dict) else data
    mapping = {
        "chunking_config": "chunking_config",
        "image_processing_config": "image_processing_config",
        "faq_config": "faq_config",
        "wiki_config": "wiki_config",
        "auto_tag_config": "auto_tag_config",
        "profile_config": "profile_config",
        "indexing_strategy": "indexing_strategy",
    }
    for source_key, column in mapping.items():
        if source_key in config_data:
            updates[column] = dumps(config_data[source_key])
    if "embedding_model_id" in data:
        updates["embedding_model_id"] = str(data.get("embedding_model_id") or "")
    if "summary_model_id" in data:
        updates["summary_model_id"] = str(data.get("summary_model_id") or "")
    if data.get("vlm_config") is not None:
        requested = data.get("vlm_config")
        if not isinstance(requested, dict):
            raise validation("vlm_config must be an object")
        current = loads(kb.get("vlm_config"), {})
        updates["vlm_config"] = dumps(
            _resolve_updated_vlm_config(
                ctx.tenant_id,
                current if isinstance(current, dict) else {},
                requested,
            )
        )
    with db.transaction() as conn:
        db.update("knowledge_bases", updates, "id=?", (kb_id,), conn)
    return ok(serialize_kb(_get_readable_kb(ctx, kb_id), ctx.user_id))


@router.delete("/knowledge-bases/{kb_id}")
def delete_knowledge_base(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    removed = db.fetch_all(
        "SELECT * FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
        (kb_id,),
    )
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE knowledge_bases SET deleted_at=?, updated_at=? WHERE id=?", (now, now, kb_id))
        conn.execute("UPDATE knowledges SET deleted_at=?, updated_at=? WHERE knowledge_base_id=?", (now, now, kb_id))
        conn.execute("UPDATE chunks SET deleted_at=?, updated_at=? WHERE knowledge_base_id=?", (now, now, kb_id))
        storage_quota.release_knowledge_rows(conn, removed)
    _delete_knowledge_files(removed)
    return ok(message="Knowledge base deleted")


@router.put("/knowledge-bases/{kb_id}/pin")
def toggle_pin(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_kb(ctx, kb_id)
    existing = db.fetch_one("SELECT 1 FROM user_kb_pins WHERE user_id=? AND kb_id=?", (ctx.user_id, kb_id))
    now = db.utcnow()
    with db.transaction() as conn:
        if existing:
            conn.execute("DELETE FROM user_kb_pins WHERE user_id=? AND kb_id=?", (ctx.user_id, kb_id))
        else:
            conn.execute("INSERT INTO user_kb_pins(tenant_id, user_id, kb_id, pinned_at) VALUES (?, ?, ?, ?)", (ctx.tenant_id, ctx.user_id, kb_id, now))
    return ok(serialize_kb(_get_kb(ctx, kb_id), ctx.user_id))


@router.post("/knowledge-bases/{kb_id}/duplicate", status_code=201)
def duplicate_knowledge_base(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb_guard(ctx, "contributor")
    source = _get_kb(ctx, kb_id)
    target = _clone_kb_row(ctx, source)
    return ok({
        "source_id": source["id"],
        "target_id": target["id"],
        "message": "Knowledge base duplicate created",
        "knowledge_base": serialize_kb(target, ctx.user_id),
    })


@router.post("/knowledge-bases/{kb_id}/profile/generate")
def generate_profile(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_writable_kb(ctx, kb_id)
    docs = db.fetch_all("SELECT title, description, file_type FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL", (kb_id,))
    titles = "、".join(doc["title"] for doc in docs[:8]) or "暂无文档"
    profile = {
        "gist": f"{kb['name']} 当前收录 {len(docs)} 份资料，主题包括 {titles}。",
        "topics": [doc["title"] for doc in docs[:10]],
        "typical_questions": [f"{kb['name']}包含哪些内容？", "这份资料的核心结论是什么？"],
        "stats": {"document_count": len(docs), "profiled_count": len(docs), "file_types": []},
        "status": "ready",
        "generated_at": db.utcnow(),
    }
    with db.transaction() as conn:
        conn.execute("UPDATE knowledge_bases SET generated_profile=?, updated_at=? WHERE id=?", (dumps(profile), db.utcnow(), kb_id))
    return ok(profile)


@router.post("/knowledge-bases/{kb_id}/rebuild-index")
def rebuild_index(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_writable_kb(ctx, kb_id)
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET index_status='indexed', updated_at=? WHERE knowledge_base_id=?", (db.utcnow(), kb_id))
    wiki_pages = ingest_all(kb)
    images = image_vectors.index_knowledge_base_images(int(kb["tenant_id"]), kb)
    return ok({"indexed": True, "wiki_pages": wiki_pages, "images": images}, message="Index rebuilt")


@router.get("/knowledge-bases/{kb_id}/knowledge")
def list_knowledge(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    params = request.query_params
    p, size, offset = pagination(params.get("page"), params.get("page_size"), 200)
    clauses = ["knowledge_base_id=?", "tenant_id=?", "deleted_at IS NULL"]
    args: list[Any] = [kb_id, int(kb["tenant_id"])]
    keyword = (params.get("keyword") or "").strip()
    if keyword:
        clauses.append("(lower(title) LIKE ? OR lower(COALESCE(file_name,'')) LIKE ?)")
        args.extend([f"%{keyword.lower()}%", f"%{keyword.lower()}%"])
    if params.get("file_type"):
        clauses.append("(file_type=? OR type=?)")
        args.extend([params["file_type"], params["file_type"]])
    if params.get("source"):
        clauses.append("(channel=? OR type=?)")
        args.extend([params["source"], params["source"]])
    if params.get("parse_status"):
        clauses.append("parse_status=?")
        args.append(params["parse_status"])
    if params.get("start_time"):
        clauses.append("created_at>=?")
        args.append(params["start_time"])
    if params.get("end_time"):
        clauses.append("created_at<=?")
        args.append(params["end_time"])
    if "folder_path" in params:
        folder = params.get("folder_path") or ""
        if truthy(params.get("folder_recursive")):
            clauses.append("(folder_path=? OR folder_path LIKE ?)")
            args.extend([folder, f"{folder}/%" if folder else "%"])
        else:
            clauses.append("folder_path=?")
            args.append(folder)
    tag_ids = (params.get("tag_ids") or "").split(",")
    tag_ids = [value for value in tag_ids if value]
    if tag_ids:
        clauses.append(
            "EXISTS (SELECT 1 FROM knowledge_tag_relations r WHERE r.knowledge_id=knowledges.id AND r.tag_id IN ("
            + ",".join("?" for _ in tag_ids)
            + "))"
        )
        args.extend(tag_ids)
    where = " AND ".join(clauses)
    total = db.fetch_one(f"SELECT COUNT(*) AS n FROM knowledges WHERE {where}", args)["n"]
    sort_by = params.get("sort_by") if params.get("sort_by") in {"updated_at", "created_at", "file_name"} else "updated_at"
    sort_order = "ASC" if (params.get("sort_order") or "desc").lower() == "asc" else "DESC"
    rows = db.fetch_all(
        f"SELECT * FROM knowledges WHERE {where} ORDER BY {sort_by} {sort_order} LIMIT ? OFFSET ?",
        [*args, size, offset],
    )
    return page([serialize_knowledge(row) for row in rows], int(total), p, size)


@router.get("/knowledge-bases/{kb_id}/knowledge/folders")
def list_folders(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_readable_kb(ctx, kb_id)
    rows = db.fetch_all(
        "SELECT folder_path, COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL GROUP BY folder_path",
        (kb_id,),
    )
    tree: dict[str, dict[str, Any]] = {}
    root_count = 0
    total = 0
    for row in rows:
        total += int(row["n"])
        path = row["folder_path"] or ""
        if not path:
            root_count += int(row["n"])
            continue
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            node_path = "/".join(parts[:index])
            node = tree.setdefault(node_path, {"path": node_path, "name": parts[index - 1], "document_count": 0, "total_count": 0, "children": []})
            node["total_count"] += int(row["n"])
        tree[path]["document_count"] += int(row["n"])
    for path in sorted(tree):
        if "/" in path:
            parent = path.rsplit("/", 1)[0]
            if parent in tree:
                tree[parent]["children"].append(tree[path])
    roots = [tree[path] for path in sorted(tree) if "/" not in path]
    return ok({"root_document_count": root_count, "total_document_count": total, "folders": roots})


@router.put("/knowledge-bases/{kb_id}/knowledge/folders")
async def rename_folder(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    source = str(data.get("from") or "").strip("/")
    target = str(data.get("to") or "").strip("/")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE knowledges SET folder_path=?, updated_at=? WHERE knowledge_base_id=? AND (folder_path=? OR folder_path LIKE ?)",
            (target, db.utcnow(), kb_id, source, f"{source}/%"),
        )
    return ok(message="Folder renamed")


def _store_uploaded_file(
    ctx: Any,
    kb: dict[str, Any],
    filename: str,
    raw: bytes,
    *,
    metadata: dict[str, Any] | None = None,
    process_config: dict[str, Any] | None = None,
    tag_ids: list[str] | None = None,
    channel: str = "web",
    folder_path: str = "",
    content_type: str = "",
    accept_language: str = "",
) -> tuple[dict[str, Any], bool]:
    if str(kb.get("type") or "") == "faq":
        raise validation("FAQ 知识库不支持文件上传，请使用 FAQ 导入功能")
    filename = Path(filename).name
    size_limit_mb = _max_file_size_mb()
    if len(raw) > size_limit_mb * 1024 * 1024:
        raise validation(f"文件大小不能超过{size_limit_mb}MB")
    extension = _validate_import_file_type(filename)
    _validate_json_upload(filename, raw)
    digest = hashlib.sha256(raw).hexdigest()
    kb_id = str(kb["id"])

    existing = db.fetch_one(
        "SELECT * FROM knowledges WHERE tenant_id=? AND knowledge_base_id=? AND type='file' "
        "AND file_name=? AND file_type=? AND file_size=? AND file_hash=? AND deleted_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (ctx.tenant_id, kb_id, filename, extension, len(raw), digest),
    )
    if existing:
        now = db.utcnow()
        with db.transaction() as conn:
            conn.execute("UPDATE knowledges SET created_at=? WHERE id=?", (now, existing["id"]))
        existing = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (existing["id"],)) or existing
        return existing, True

    effective_kb = _merge_process_config(kb, process_config)
    _validate_file_process_admission(effective_kb, filename, extension)
    image_mime = image_vectors.detect_image_mime(raw)

    tenant = db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    storage_backend_id, storage_provider, _config, _bucket, _region = object_storage.resolve_write_target(
        ctx.tenant_id,
        str(kb.get("storage_backend_id") or ""),
        _kb_storage_provider(kb, tenant or {}),
    )
    knowledge_id = db.new_id()
    if storage_provider in object_storage.OBJECT_SCHEMES:
        file_path = object_storage.save_bytes(
            raw,
            ctx.tenant_id,
            filename,
            backend_id=storage_backend_id,
            provider=storage_provider,
            knowledge_id=knowledge_id,
            content_type=content_type,
        )
    else:
        target_dir = config.FILES_DIR / str(ctx.tenant_id) / kb_id
        target_dir.mkdir(parents=True, exist_ok=True)
        stored = target_dir / f"{digest[:12]}-{filename}"
        if not stored.exists():
            stored.write_bytes(raw)
        file_path = str(stored)

    if audio_asr.is_audio_filename(filename):
        extracted_images: set[str] = set()
        try:
            text = audio_asr.transcribe_audio(
                effective_kb,
                {"tenant_id": ctx.tenant_id},
                raw,
                filename,
            )
            if not text:
                text = "[No speech detected in audio file]"
            status = "completed"
            error = ""
        except Exception as exc:  # noqa: BLE001 - provider failures become a failed knowledge row
            text = ""
            status = "failed"
            error = f"audio transcription failed: {exc}"
    elif image_mime:
        text = ""
        extracted_images = set()
        status = "completed"
        error = ""
    else:
        text, embedded_images = extract_document(filename, raw)
        text, extracted_images = _materialize_embedded_images(
            ctx.tenant_id, effective_kb, knowledge_id, text, embedded_images
        )
        status = "completed" if text.strip() else "failed"
        error = "" if status == "completed" else "No extractable text found"

    knowledge_metadata = dict(metadata or {})
    if process_config is not None:
        knowledge_metadata["process_overrides"] = process_config

    knowledge = _create_knowledge(
        ctx,
        effective_kb,
        title=filename,
        text=text,
        knowledge_type="file",
        source=filename,
        channel=channel,
        file_name=filename,
        file_type=extension,
        file_path=file_path,
        file_size=len(raw),
        file_hash=digest,
        tag_ids=tag_ids,
        metadata=knowledge_metadata,
        folder_path=folder_path,
        parse_status=status,
        error_message=error,
        image_reference=file_path if image_mime else "",
        image_caption=Path(filename).stem if image_mime else "",
        knowledge_id=knowledge_id,
        extracted_images=extracted_images,
        accept_language=accept_language,
    )
    return knowledge, False


def persist_im_attachment(
    channel: dict[str, Any],
    file_name: str,
    content: bytes,
) -> dict[str, Any] | None:
    tenant_id = int(channel["tenant_id"])
    kb_id = str(channel.get("knowledge_base_id") or "")
    if not kb_id:
        return None
    kb = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, tenant_id),
    )
    if not kb:
        return None
    platform = str(channel.get("platform") or "").lower()
    channel_name = {"wxwork": "wecom", "lark": "feishu"}.get(platform, platform or "im")
    knowledge, duplicate = _store_uploaded_file(
        SimpleNamespace(tenant_id=tenant_id),
        kb,
        file_name,
        content,
        channel=channel_name,
        content_type=mimetypes.guess_type(file_name)[0] or "",
    )
    return None if duplicate else knowledge


@router.post("/knowledge-bases/{kb_id}/knowledge/file")
async def upload_file(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_writable_kb(ctx, kb_id)
    form = await request.form()
    upload = next(
        (value for key, value in form.multi_items() if key == "file" and hasattr(value, "read")),
        None,
    )
    if upload is None:
        raise validation("At least one file is required")
    if str(kb.get("type") or "") == "faq":
        raise validation("FAQ 知识库不支持文件上传，请使用 FAQ 导入功能")

    custom_name = str(form.get("fileName") or "").strip()
    folder_path = str(form.get("folder_path") or "").strip("/")
    filename = Path(getattr(upload, "filename", "") or "upload.bin").name
    if custom_name:
        custom_folder, custom_file = _split_upload_name(custom_name)
        if custom_file:
            filename = Path(custom_file).name
        if custom_folder:
            folder_path = custom_folder

    metadata: dict[str, Any] = {}
    metadata_raw = str(form.get("metadata") or "")
    if metadata_raw:
        try:
            parsed_metadata = json.loads(metadata_raw)
        except ValueError as exc:
            raise validation("Invalid metadata format") from exc
        if not isinstance(parsed_metadata, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in parsed_metadata.items()
        ):
            raise validation("Invalid metadata format")
        metadata = parsed_metadata

    process_config: dict[str, Any] | None = None
    process_config_raw = str(form.get("process_config") or "")
    if process_config_raw:
        try:
            process_config = json.loads(process_config_raw)
        except ValueError as exc:
            raise validation("Invalid process_config format") from exc
        if not isinstance(process_config, dict):
            raise validation("Invalid process_config format")
        _validate_process_config(process_config)

    enable_multimodel_raw = str(form.get("enable_multimodel") or "").strip()
    if enable_multimodel_raw and enable_multimodel_raw.lower() not in {
        "1", "0", "t", "f", "true", "false",
    }:
        raise validation("Invalid enable_multimodel format")
    enable_multimodel = (
        enable_multimodel_raw.lower() in {"1", "t", "true"}
        if enable_multimodel_raw
        else None
    )
    if enable_multimodel is not None and (
        process_config is None or "enable_multimodel" not in process_config
    ):
        process_config = dict(process_config or {})
        process_config["enable_multimodel"] = enable_multimodel

    tag_ids = [value.strip() for value in str(form.get("tag_ids") or "").split(",") if value.strip()]
    channel = str(form.get("channel") or "web").strip() or "web"
    knowledge, duplicate = _store_uploaded_file(
        ctx,
        kb,
        filename,
        await upload.read(),
        metadata=metadata,
        process_config=process_config,
        tag_ids=tag_ids,
        channel=channel,
        folder_path=folder_path,
        content_type=str(getattr(upload, "content_type", "") or ""),
        accept_language=request.headers.get("accept-language", ""),
    )
    if duplicate:
        return JSONResponse(
            {
                "success": False,
                "message": f"File already exists: {filename}",
                "data": serialize_knowledge(knowledge),
                "code": "duplicate_file",
            },
            status_code=409,
        )
    return ok(serialize_knowledge(knowledge))


@router.post("/knowledge-bases/{kb_id}/knowledge/manual", status_code=201)
async def create_manual(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_writable_kb(ctx, kb_id)
    data = await body(request)
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "")
    if not title or not content.strip():
        raise validation("Title and content are required")
    knowledge = _create_knowledge(
        ctx,
        kb,
        title=title,
        text=content,
        knowledge_type="manual",
        source="manual",
        file_name=title,
        file_type="md",
        tag_ids=[str(v) for v in data.get("tag_ids") or []],
        metadata={"status": data.get("status") or "publish"},
        accept_language=request.headers.get("accept-language", ""),
    )
    return ok(serialize_knowledge(knowledge))


@router.post("/knowledge-bases/{kb_id}/knowledge/url", status_code=201)
async def create_from_url(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_writable_kb(ctx, kb_id)
    data = await body(request)
    url = str(data.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise validation("A valid http(s) URL is required")
    text = ""
    title = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1] or urllib.parse.urlparse(url).netloc
    try:
        raw, headers, _ = url_guard.fetch_public_url(
            url,
            headers={"User-Agent": "PyWeKnora/0.8.2"},
            timeout=30,
            max_bytes=10 * 1024 * 1024,
        )
        content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip()
        text = extract_text(title or "page.html", raw)
        if content_type.startswith("text/") and not Path(title).suffix:
            title = title or urllib.parse.urlparse(url).netloc
    except Exception as exc:  # noqa: BLE001 - network import should surface as parse failure
        knowledge = _create_knowledge(
            ctx, kb, title=title or url, text="", knowledge_type="url", source=url,
            file_name=title or url, file_type="url", tag_ids=[str(v) for v in data.get("tag_ids") or []],
            metadata={"url": url}, parse_status="failed", error_message=str(exc),
        )
        return ok(serialize_knowledge(knowledge))
    knowledge = _create_knowledge(
        ctx, kb, title=Path(title).stem or title, text=text, knowledge_type="url", source=url,
        file_name=title, file_type="html", tag_ids=[str(v) for v in data.get("tag_ids") or []],
        metadata={"url": url},
        accept_language=request.headers.get("accept-language", ""),
    )
    return ok(serialize_knowledge(knowledge))


@router.get("/knowledge-bases/{kb_id}/move-targets")
def kb_move_targets(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_kb(ctx, kb_id)
    sql = "SELECT * FROM knowledge_bases WHERE tenant_id=? AND id<>? AND deleted_at IS NULL"
    params: list[Any] = [ctx.tenant_id, kb_id]
    if ctx.api_key is not None and ctx.api_key.is_kb_restricted:
        sql += " AND id IN (" + ",".join("?" for _ in ctx.api_key.knowledge_base_ids) + ")"
        params.extend(ctx.api_key.knowledge_base_ids)
    rows = db.fetch_all(sql + " ORDER BY name", params)
    return ok([serialize_kb(row, ctx.user_id) for row in rows])


@router.post("/knowledge-bases/{kb_id}/knowledge/batch-download")
async def batch_download(kb_id: str, request: Request) -> StreamingResponse:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb_guard(ctx, "contributor")
    kb = _get_readable_kb(ctx, kb_id)
    _require_kb_write_access(ctx, kb)
    data = await body(request)
    ids = [str(value) for value in data.get("ids") or []]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for knowledge in _knowledge_rows(ctx, ids=ids, kb_id=kb_id):
            path = knowledge.get("file_path")
            if isinstance(path, str) and object_storage.is_object_reference(path):
                raw = _knowledge_file_bytes(knowledge)
                if raw is not None:
                    archive.writestr(knowledge.get("file_name") or knowledge["id"], raw)
                else:
                    archive.writestr(f"{(knowledge.get('title') or knowledge['id'])}.md", _knowledge_text(knowledge["id"]))
            elif path and Path(path).exists():
                archive.write(path, arcname=knowledge.get("file_name") or Path(path).name)
            else:
                archive.writestr(f"{(knowledge.get('title') or knowledge['id'])}.md", _knowledge_text(knowledge["id"]))
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=knowledge.zip"})


def _knowledge_rows(ctx: Any, *, ids: list[str] | None = None, kb_id: str | None = None, knowledge_id: str | None = None) -> list[dict[str, Any]]:
    shared = sorted(shared_knowledge_base_ids(ctx.tenant_id, ctx.role))
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if shared:
        clauses.append(
            "(tenant_id=? OR knowledge_base_id IN ("
            + ",".join("?" for _ in shared)
            + "))"
        )
        params.extend([ctx.tenant_id, *shared])
    else:
        clauses.append("tenant_id=?")
        params.append(ctx.tenant_id)
    if ctx.api_key is not None and ctx.api_key.is_kb_restricted:
        clauses.append("knowledge_base_id IN (" + ",".join("?" for _ in ctx.api_key.knowledge_base_ids) + ")")
        params.extend(ctx.api_key.knowledge_base_ids)
    if knowledge_id:
        clauses.append("id=?")
        params.append(knowledge_id)
    if kb_id:
        clauses.append("knowledge_base_id=?")
        params.append(kb_id)
    if ids:
        clauses.append("id IN (" + ",".join("?" for _ in ids) + ")")
        params.extend(ids)
    return db.fetch_all("SELECT * FROM knowledges WHERE " + " AND ".join(clauses), params)


def _knowledge_text(knowledge_id: str) -> str:
    rows = db.fetch_all("SELECT content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index", (knowledge_id,))
    return "\n\n".join(row["content"] for row in rows)


def _knowledge_reparse_content(
    kb: dict[str, Any],
    knowledge: dict[str, Any],
) -> tuple[str, str, str, list[dict[str, Any]]]:
    file_path = str(knowledge.get("file_path") or "")
    raw = _knowledge_file_bytes(knowledge)
    if raw is not None:
        filename = str(knowledge.get("file_name") or Path(file_path).name)
        if image_vectors.detect_image_mime(raw):
            return "", file_path, Path(filename).stem or str(knowledge.get("title") or ""), []
        if audio_asr.is_video_filename(filename):
            return "", "", "", []
        if audio_asr.is_audio_filename(filename):
            try:
                text = audio_asr.transcribe_audio(kb, knowledge, raw, filename)
            except Exception:  # noqa: BLE001 - ASR failure leaves the knowledge unparsed
                text = ""
            return text, "", "", []
        text, assets = extract_document(filename, raw)
        return text, "", "", assets
    return _knowledge_text(str(knowledge["id"])), "", "", []


def _refresh_image_vectors(knowledge_id: str, tenant_id: int) -> None:
    knowledge = db.fetch_one(
        "SELECT * FROM knowledges WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (knowledge_id, tenant_id),
    )
    if not knowledge:
        return
    kb = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (knowledge["knowledge_base_id"], tenant_id),
    )
    if kb:
        image_vectors.index_knowledge_images(tenant_id, kb, knowledge)


def _knowledge_file_bytes(knowledge: dict[str, Any]) -> bytes | None:
    file_path = str(knowledge.get("file_path") or "")
    if not file_path:
        return None
    if object_storage.is_object_reference(file_path):
        try:
            return object_storage.read_object(file_path, int(knowledge.get("tenant_id") or 0))
        except object_storage.ObjectStorageError:
            return None
    candidate = Path(file_path)
    return candidate.read_bytes() if candidate.is_file() else None


def _clone_knowledge_file(
    knowledge: dict[str, Any], target_kb_id: str, knowledge_id: str
) -> str:
    source = str(knowledge.get("file_path") or "")
    if not source:
        return ""
    tenant_id = int(knowledge.get("tenant_id") or 0)
    if object_storage.is_object_reference(source):
        try:
            return object_storage.copy_object(
                source,
                tenant_id,
                knowledge_id,
                filename=str(knowledge.get("file_name") or ""),
            )
        except object_storage.ObjectStorageError as exc:
            raise validation(f"failed to copy knowledge file: {exc}") from exc
    candidate = Path(source)
    if not candidate.is_file():
        raise validation("source knowledge file not found")
    target_dir = config.FILES_DIR / str(tenant_id) / target_kb_id
    target_dir.mkdir(parents=True, exist_ok=True)
    extension = candidate.suffix
    target = target_dir / f"{knowledge_id}{extension}"
    target.write_bytes(candidate.read_bytes())
    return str(target)


def _faq_question_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _clone_faq_tags(source_kb_id: str, target_kb_id: str, tenant_id: int) -> dict[int, int]:
    sources = db.fetch_all(
        "SELECT * FROM knowledge_tags WHERE tenant_id=? AND knowledge_base_id=? "
        "AND seq_id IS NOT NULL AND deleted_at IS NULL ORDER BY sort_order, seq_id",
        (tenant_id, source_kb_id),
    )
    mapping: dict[int, int] = {}
    if not sources:
        return mapping
    with db.transaction() as conn:
        for source in sources:
            name = str(source.get("name") or "")
            target = conn.execute(
                "SELECT * FROM knowledge_tags WHERE tenant_id=? AND knowledge_base_id=? AND name=?",
                (tenant_id, target_kb_id, name),
            ).fetchone()
            sort_order = -1 if name == "未分类" else int(source.get("sort_order") or 0)
            if target:
                target_seq = target["seq_id"]
                if target_seq is None:
                    target_seq = conn.execute(
                        "SELECT COALESCE(MAX(seq_id), 0)+1 AS n FROM knowledge_tags"
                    ).fetchone()["n"]
                if target["deleted_at"] is not None:
                    conn.execute(
                        "UPDATE knowledge_tags SET seq_id=?, color=?, sort_order=?, deleted_at=NULL, updated_at=? "
                        "WHERE id=?",
                        (target_seq, source.get("color"), sort_order, db.utcnow(), target["id"]),
                    )
                elif target["seq_id"] is None:
                    conn.execute(
                        "UPDATE knowledge_tags SET seq_id=?, updated_at=? WHERE id=?",
                        (target_seq, db.utcnow(), target["id"]),
                    )
            else:
                target_seq = conn.execute(
                    "SELECT COALESCE(MAX(seq_id), 0)+1 AS n FROM knowledge_tags"
                ).fetchone()["n"]
                conn.execute(
                    "INSERT INTO knowledge_tags(id, tenant_id, knowledge_base_id, name, color, sort_order, seq_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        db.new_id(), tenant_id, target_kb_id, name, source.get("color"),
                        sort_order, target_seq, db.utcnow(), db.utcnow(),
                    ),
                )
            mapping[int(source["seq_id"])] = int(target_seq)
    return mapping


def _clone_faq_entry_batch(
    entries: list[dict[str, Any]],
    target_kb_id: str,
    tenant_id: int,
    tag_map: dict[int, int],
) -> None:
    existing = {
        _faq_question_key(row.get("standard_question")): row
        for row in db.fetch_all(
            "SELECT * FROM py_faq_entries WHERE tenant_id=? AND knowledge_base_id=? "
            "AND deleted_at IS NULL ORDER BY id",
            (tenant_id, target_kb_id),
        )
    }
    now = db.utcnow()
    with db.transaction() as conn:
        for entry in entries:
            question = str(entry.get("standard_question") or "")
            tag_id = entry.get("tag_id")
            target_tag = tag_map.get(int(tag_id)) if tag_id not in (None, "") else None
            values = (
                question,
                str(entry.get("answer") or ""),
                str(entry.get("similar_questions") or "[]"),
                target_tag,
                1 if entry.get("is_enabled") else 0,
                1 if entry.get("is_recommended") else 0,
                now,
            )
            current = existing.get(_faq_question_key(question))
            if current:
                conn.execute(
                    "UPDATE py_faq_entries SET standard_question=?, answer=?, similar_questions=?, "
                    "tag_id=?, is_enabled=?, is_recommended=?, updated_at=? WHERE id=?",
                    (*values, current["id"]),
                )
                continue
            cursor = conn.execute(
                "INSERT INTO py_faq_entries(standard_question, answer, similar_questions, tag_id, "
                "is_enabled, is_recommended, updated_at, tenant_id, knowledge_base_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, tenant_id, target_kb_id, now),
            )
            existing[_faq_question_key(question)] = {"id": cursor.lastrowid}


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def _image_extension(reference: str, data: bytes) -> str:
    extension = Path(urllib.parse.urlsplit(reference).path).suffix.lower()
    if extension in _IMAGE_EXTENSIONS:
        return extension
    signatures = (
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"\xff\xd8\xff", ".jpg"),
        (b"GIF8", ".gif"),
        (b"BM", ".bmp"),
    )
    for signature, detected in signatures:
        if data.startswith(signature):
            return detected
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def _copy_owned_image(reference: str, tenant_id: int, knowledge_id: str) -> str:
    if object_storage.is_object_reference(reference):
        try:
            return object_storage.copy_object(reference, tenant_id, knowledge_id)
        except object_storage.ObjectStorageError as exc:
            raise validation(f"failed to copy chunk image: {exc}") from exc
    try:
        source, _name = file_proxy.resolve_file(reference, tenant_id)
        data = source.read_bytes()
    except Exception as exc:  # noqa: BLE001 - surface a clone validation error
        raise validation(f"failed to read chunk image: {reference}") from exc
    target_dir = config.FILES_DIR / str(tenant_id) / knowledge_id
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{db.new_id()}{_image_extension(reference, data)}"
    target = target_dir / filename
    target.write_bytes(data)
    copied = resource_catalog.register_path(
        tenant_id,
        str(target),
        original_name=filename,
        size=len(data),
        kind="image",
        mime_type=mimetypes.guess_type(filename)[0] or "",
    )
    try:
        resource_catalog.bind_references(
            tenant_id,
            knowledge_id,
            [copied],
            relation=resource_catalog.RESOURCE_RELATION_EXTRACTED_IMAGE,
        )
    except Exception:  # noqa: BLE001 - a missed clone claim must keep the copied bytes
        pass
    return copied


def _clone_chunk_image_info(
    raw: Any, tenant_id: int, knowledge_id: str, url_cache: dict[str, str]
) -> tuple[str, list[str]]:
    if not raw:
        return "", []
    try:
        images = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        raise validation("failed to parse chunk image_info JSON") from exc
    if not isinstance(images, list):
        raise validation("failed to parse chunk image_info JSON")
    copied: list[str] = []
    for image in images:
        if not isinstance(image, dict):
            continue
        url = image.get("url")
        if not isinstance(url, str) or not url:
            continue
        original_matched = image.get("original_url") == url
        new_url = url_cache.get(url)
        if new_url is None:
            new_url = _copy_owned_image(url, tenant_id, knowledge_id)
            url_cache[url] = new_url
            copied.append(new_url)
        if original_matched:
            image["original_url"] = new_url
        image["url"] = new_url
    return dumps(images), copied


def _rewrite_content_image_urls(content: str, url_cache: dict[str, str]) -> str:
    if not content or not url_cache:
        return content
    for old_url in sorted(url_cache, key=len, reverse=True):
        new_url = url_cache[old_url]
        if old_url and old_url != new_url:
            content = content.replace(old_url, new_url)
    return content


def _delete_reference_bytes(reference: str, tenant_id: int) -> bool:
    value = str(reference or "").strip()
    if not value:
        return True
    if value.startswith(resource_catalog.RESOURCE_PREFIX):
        handle = resource_catalog.parse_handle(value)
        row = db.fetch_one(
            "SELECT physical_path FROM resources WHERE handle=? AND tenant_id=? "
            "AND state='active' AND deleted_at IS NULL",
            (handle, tenant_id),
        )
        if not row:
            return False
        value = str(row.get("physical_path") or "")
    if object_storage.is_object_reference(value):
        try:
            object_storage.delete_object(value, tenant_id)
            return True
        except Exception:  # noqa: BLE001 - cleanup is best effort
            return False
    try:
        path, _name = file_proxy.resolve_file(value, tenant_id)
        path.unlink(missing_ok=True)
        return True
    except Exception:  # noqa: BLE001 - cleanup is best effort
        return False


def _remove_cloned_files(
    references: list[str],
    tenant_id: int,
    owner_ids: list[str] | tuple[str, ...] | None = None,
) -> None:
    for reference in references:
        if not reference:
            continue
        is_resource = reference.startswith(resource_catalog.RESOURCE_PREFIX)
        if is_resource and owner_ids:
            try:
                remaining = -1
                for owner_id in owner_ids:
                    count = resource_catalog.release_reference(
                        reference, tenant_id, [owner_id]
                    )
                    if count >= 0:
                        remaining = count
            except Exception:  # noqa: BLE001 - cleanup is best effort
                continue
            if remaining > 0:
                continue
        if not _delete_reference_bytes(reference, tenant_id):
            continue
        if is_resource:
            resource_catalog.mark_deleted(reference, tenant_id)


def _prepare_cloned_chunks(
    chunks: list[dict[str, Any]], tenant_id: int, knowledge_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    url_cache: dict[str, str] = {}
    chunk_ids: dict[str, str] = {}
    prepared: list[dict[str, Any]] = []
    copied: list[str] = []
    try:
        for chunk in chunks:
            values = dict(chunk)
            source_id = str(chunk.get("id") or "")
            target_id = db.new_id()
            if source_id:
                chunk_ids[source_id] = target_id
            values["id"] = target_id
            image_info, created = _clone_chunk_image_info(
                values.get("image_info"), tenant_id, knowledge_id, url_cache
            )
            values["image_info"] = image_info
            copied.extend(created)
            prepared.append(values)
        for values in prepared:
            values["content"] = _rewrite_content_image_urls(str(values.get("content") or ""), url_cache)
            values["source_content"] = _rewrite_content_image_urls(
                str(values.get("source_content") or ""), url_cache
            )
            for field in ("pre_chunk_id", "next_chunk_id", "parent_chunk_id"):
                values[field] = chunk_ids.get(str(values.get(field) or ""), "")
        return prepared, copied
    except Exception:
        _remove_cloned_files(copied, tenant_id, [knowledge_id])
        raise


def _prepare_cloned_knowledge(
    knowledge: dict[str, Any], target_kb_id: str, knowledge_id: str
) -> tuple[str, list[dict[str, Any]], list[str]]:
    tenant_id = int(knowledge.get("tenant_id") or 0)
    cloned_file = _clone_knowledge_file(knowledge, target_kb_id, knowledge_id)
    created = [cloned_file] if cloned_file else []
    try:
        chunks = db.fetch_all(
            "SELECT * FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL",
            (knowledge["id"],),
        )
        prepared, image_files = _prepare_cloned_chunks(chunks, tenant_id, knowledge_id)
    except Exception:
        _remove_cloned_files(created, tenant_id, [knowledge_id])
        raise
    created.extend(image_files)
    return cloned_file, prepared, created


def _delete_knowledge_files(rows: list[dict[str, Any]]) -> None:
    grouped: dict[int, dict[str, list[str]]] = {}
    for row in rows:
        tenant_id = int(row.get("tenant_id") or 0)
        knowledge_id = str(row.get("id") or "")
        if tenant_id <= 0 or not knowledge_id:
            continue
        group = grouped.setdefault(tenant_id, {"owners": [], "references": []})
        group["owners"].append(knowledge_id)
        group["references"].extend(
            _extracted_image_references(
                knowledge_id,
                tenant_id,
                include_deleted=True,
                all_images=True,
            )
        )
    for tenant_id, group in grouped.items():
        owners = sorted(set(group["owners"]))
        references = list(dict.fromkeys(group["references"]))
        try:
            references.extend(
                resource_catalog.list_references_by_owner(tenant_id, owners)
            )
        except Exception:  # noqa: BLE001 - deleting ImageInfo files still proceeds
            pass
        _remove_cloned_files(
            list(dict.fromkeys(references)), tenant_id, owner_ids=owners
        )
    for row in rows:
        reference = str(row.get("file_path") or "")
        tenant_id = int(row.get("tenant_id") or 0)
        if reference.startswith(resource_catalog.RESOURCE_PREFIX):
            if db.fetch_one(
                "SELECT 1 FROM knowledges WHERE file_path=? AND deleted_at IS NULL LIMIT 1",
                (reference,),
            ):
                continue
            _remove_cloned_files([reference], tenant_id)
            continue
        if not object_storage.is_object_reference(reference):
            continue
        if db.fetch_one(
            "SELECT 1 FROM knowledges WHERE file_path=? AND deleted_at IS NULL LIMIT 1",
            (reference,),
        ):
            continue
        try:
            object_storage.delete_object(reference, tenant_id)
        except Exception:  # noqa: BLE001 - cleanup is best effort after the DB commit
            continue


@router.get("/knowledge/search")
def search_knowledge(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    keyword = (request.query_params.get("keyword") or "").strip()
    offset = int(request.query_params.get("offset") or 0)
    limit = min(200, max(1, int(request.query_params.get("limit") or 20)))
    shared = sorted(shared_knowledge_base_ids(ctx.tenant_id, ctx.role))
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if shared:
        clauses.append(
            "(tenant_id=? OR knowledge_base_id IN ("
            + ",".join("?" for _ in shared)
            + "))"
        )
        params.extend([ctx.tenant_id, *shared])
    else:
        clauses.append("tenant_id=?")
        params.append(ctx.tenant_id)
    if ctx.api_key is not None and ctx.api_key.is_kb_restricted:
        clauses.append("knowledge_base_id IN (" + ",".join("?" for _ in ctx.api_key.knowledge_base_ids) + ")")
        params.extend(ctx.api_key.knowledge_base_ids)
    if keyword:
        clauses.append("(lower(title) LIKE ? OR lower(COALESCE(file_name,'')) LIKE ? OR lower(COALESCE(description,'')) LIKE ?)")
        needle = f"%{keyword.lower()}%"
        params.extend([needle, needle, needle])
    total = db.fetch_one("SELECT COUNT(*) AS n FROM knowledges WHERE " + " AND ".join(clauses), params)["n"]
    rows = db.fetch_all(
        "SELECT * FROM knowledges WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return page([serialize_knowledge(row) for row in rows], int(total), offset // limit + 1, limit)


@router.post("/knowledge-search")
async def semantic_search(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    query = str(data.get("query") or "").strip()
    if not query:
        raise validation("query is required")
    requested = [str(v) for v in data.get("knowledge_base_ids") or [] if v]
    if ctx.api_key is not None and ctx.api_key.is_kb_restricted:
        if requested:
            authorize_knowledge_bases(ctx.api_key, requested)
        else:
            requested = list(ctx.api_key.knowledge_base_ids)
    rows = retrieve(
        ctx.tenant_id,
        query,
        knowledge_base_ids=requested or None,
        limit=int(data.get("limit") or 10),
        embedding_config=model_runtime.embedding_runtime(ctx.tenant_id, requested),
    )
    return ok(references(rows))


@router.post("/knowledge-bases/{kb_id}/hybrid-search")
async def hybrid_search(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    data = await body(request)
    query = str(data.get("query") or data.get("query_text") or "").strip()
    rows = retrieve(
        int(kb["tenant_id"]),
        query,
        knowledge_base_ids=[kb_id],
        limit=int(data.get("limit") or 10),
        embedding_config=model_runtime.embedding_runtime(int(kb["tenant_id"]), [kb_id]),
    ) if query else []
    return ok(references(rows))


@router.get("/knowledge-bases/{kb_id}/hybrid-search")
def hybrid_search_get(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    query = (request.query_params.get("query") or request.query_params.get("query_text") or "").strip()
    limit = min(100, max(1, int(request.query_params.get("limit") or 10)))
    rows = retrieve(
        int(kb["tenant_id"]),
        query,
        knowledge_base_ids=[kb_id],
        limit=limit,
        embedding_config=model_runtime.embedding_runtime(int(kb["tenant_id"]), [kb_id]),
    ) if query else []
    return ok(references(rows))


@router.get("/knowledge-bases/{kb_id}/activity")
def knowledge_base_activity(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    limit = min(200, max(1, int(request.query_params.get("limit") or 50)))
    rows = db.fetch_all(
        "SELECT * FROM audit_logs WHERE tenant_id=? AND (target_id=? OR details LIKE ?) ORDER BY created_at DESC LIMIT ?",
        (int(kb["tenant_id"]), kb_id, f"%{kb_id}%", limit),
    )
    return ok({"logs": rows, "total": len(rows)})


@router.get("/knowledge-bases/copy/progress/{task_id}")
def copy_progress(task_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _task_row(ctx, task_id, "kb_clone", "KB clone task not found")
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    data = _task_progress(row)
    data.update({
        "source_id": str(payload.get("source_id") or ""),
        "target_id": str(payload.get("target_id") or ""),
    })
    return ok(data)


@router.delete("/knowledge-bases/{kb_id}/knowledge")
async def delete_knowledge_collection(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    ids = [str(value) for value in data.get("ids") or []]
    if ids:
        marks = ",".join("?" for _ in ids)
        removed = db.fetch_all(
            f"SELECT * FROM knowledges WHERE knowledge_base_id=? AND tenant_id=? "
            f"AND deleted_at IS NULL AND id IN ({marks})",
            (kb_id, ctx.tenant_id, *ids),
        )
    else:
        removed = db.fetch_all(
            "SELECT * FROM knowledges WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
            (kb_id, ctx.tenant_id),
        )
    now = db.utcnow()
    with db.transaction() as conn:
        storage_quota.release_knowledge_rows(conn, removed)
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(f"UPDATE knowledges SET deleted_at=?, updated_at=? WHERE knowledge_base_id=? AND tenant_id=? AND id IN ({marks})", (now, now, kb_id, ctx.tenant_id, *ids))
            conn.execute(f"UPDATE chunks SET deleted_at=?, updated_at=? WHERE knowledge_base_id=? AND tenant_id=? AND knowledge_id IN ({marks})", (now, now, kb_id, ctx.tenant_id, *ids))
        else:
            conn.execute("UPDATE knowledges SET deleted_at=?, updated_at=? WHERE knowledge_base_id=? AND tenant_id=?", (now, now, kb_id, ctx.tenant_id))
            conn.execute("UPDATE chunks SET deleted_at=?, updated_at=? WHERE knowledge_base_id=? AND tenant_id=?", (now, now, kb_id, ctx.tenant_id))
    _delete_knowledge_files(removed)
    return ok({"deleted": len(ids) if ids else "all"})


@router.get("/knowledge/batch")
def batch_query(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    ids = request.query_params.getlist("ids")
    rows = _knowledge_rows(ctx, ids=ids, kb_id=request.query_params.get("kb_id"))
    return ok([serialize_knowledge(row) for row in rows])


@router.post("/knowledge/batch-delete")
async def batch_delete(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    ids = [str(value) for value in data.get("ids") or []]
    removed = [_get_writable_knowledge(ctx, knowledge_id) for knowledge_id in ids]
    now = db.utcnow()
    with db.transaction() as conn:
        storage_quota.release_knowledge_rows(conn, removed)
        for knowledge_id in ids:
            conn.execute("UPDATE knowledges SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (now, now, knowledge_id, ctx.tenant_id))
            conn.execute("UPDATE chunks SET deleted_at=?, updated_at=? WHERE knowledge_id=?", (now, now, knowledge_id))
    _delete_knowledge_files(removed)
    return ok({"deleted": len(ids)})


@router.post("/knowledge/batch-reparse")
async def batch_reparse(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    ids = [str(value) for value in data.get("ids") or []]
    if not ids:
        raise validation("no knowledge IDs provided for batch reparse")
    kb_id = str(data.get("kb_id") or "").strip()
    if kb_id:
        _get_writable_kb(ctx, kb_id)
    for knowledge_id in ids:
        knowledge = _get_writable_knowledge(ctx, knowledge_id, kb_id or None)
        kb_id = kb_id or str(knowledge["knowledge_base_id"])
    task_id = _create_task(
        ctx,
        "knowledge_reparse",
        len(ids),
        {"kb_id": kb_id, "knowledge_ids": ids},
    )
    schedule_task_worker(task_id)
    return ok(
        {"task_id": task_id, "reparse_count": len(ids)},
        message="Batch reparse task submitted",
    )


@router.post("/knowledge/folder")
async def move_to_folder(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    ids = [str(value) for value in data.get("knowledge_ids") or data.get("ids") or []]
    for knowledge_id in ids:
        _get_writable_knowledge(ctx, knowledge_id)
    folder = str(data.get("folder_path") or "").strip("/")
    with db.transaction() as conn:
        for knowledge_id in ids:
            conn.execute("UPDATE knowledges SET folder_path=?, updated_at=? WHERE id=? AND tenant_id=?", (folder, db.utcnow(), knowledge_id, ctx.tenant_id))
    return ok({"updated": len(ids)})


@router.post("/knowledge/move")
async def move_knowledge(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    raw_ids = [str(value) for value in data.get("knowledge_ids") or data.get("ids") or []]
    if any(not value.strip() for value in raw_ids):
        raise validation("Knowledge ID cannot be empty")
    ids = list(dict.fromkeys(value.strip() for value in raw_ids if value.strip()))
    if not ids:
        raise validation("knowledge_ids is required")
    source = str(data.get("source_kb_id") or "").strip()
    target = str(data.get("target_kb_id") or data.get("knowledge_base_id") or "").strip()
    if not source:
        raise validation("source_kb_id is required")
    if not target:
        raise validation("target_kb_id is required")
    if source == target:
        raise validation("Source and target knowledge base cannot be the same")
    mode = str(data.get("mode") or "").strip()
    if not mode:
        raise validation("mode is required")
    if mode not in {"reuse_vectors", "reparse"}:
        raise validation("mode must be reuse_vectors or reparse")
    source_kb = _get_writable_kb(ctx, source)
    target_kb = _get_writable_kb(ctx, target)
    _validate_kb_transfer_compatibility(source_kb, target_kb, "move", mode, ctx.tenant)
    for knowledge_id in ids:
        knowledge = db.fetch_one(
            "SELECT * FROM knowledges WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (knowledge_id, ctx.tenant_id),
        )
        if not knowledge:
            raise validation(f"Knowledge item {knowledge_id} not found")
        if str(knowledge.get("knowledge_base_id") or "") != source:
            raise validation(f"Knowledge item {knowledge_id} does not belong to the source knowledge base")
        if str(knowledge.get("parse_status") or "") != "completed":
            raise validation(
                f"Knowledge item {knowledge_id} is not in completed status "
                f"(current: {knowledge.get('parse_status') or ''})"
            )
    task_id = _create_task(
        ctx,
        "knowledge_move",
        len(ids),
        {
            "source_kb_id": source,
            "target_kb_id": target,
            "knowledge_ids": ids,
            "mode": mode,
        },
    )
    schedule_task_worker(task_id)
    return ok({
        "task_id": task_id,
        "source_kb_id": source,
        "target_kb_id": target,
        "knowledge_count": len(ids),
        "message": "Knowledge move task started",
    })


@router.get("/knowledge/move/progress/{task_id}")
def move_progress(task_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _task_row(ctx, task_id, "knowledge_move", "Knowledge move task not found")
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    result = loads(row.get("result"), {})
    if not isinstance(result, dict):
        result = {}
    data = _task_progress(row)
    data.update({
        "source_kb_id": str(payload.get("source_kb_id") or ""),
        "target_kb_id": str(payload.get("target_kb_id") or ""),
        "failed": int(result.get("failed") or 0),
    })
    return ok(data)


@router.post("/knowledge/tags")
@router.put("/knowledge/tags")
async def update_knowledge_tag_batch(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    updates = data.get("updates") if isinstance(data.get("updates"), dict) else {}
    knowledge_rows: dict[str, dict[str, Any]] = {}
    for knowledge_id in updates:
        knowledge_rows[str(knowledge_id)] = _get_writable_knowledge(ctx, str(knowledge_id))
    with db.transaction() as conn:
        for knowledge_id, tags in updates.items():
            knowledge = knowledge_rows[str(knowledge_id)]
            _set_tags(
                conn,
                str(knowledge_id),
                [str(value) for value in tags or []],
                tenant_id=ctx.tenant_id,
                kb_id=str(knowledge["knowledge_base_id"]),
            )
    return ok({"updated": len(updates)})


@router.get("/knowledge/{knowledge_id}")
def get_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(serialize_knowledge(_get_readable_knowledge(ctx, knowledge_id)))


@router.put("/knowledge/{knowledge_id}")
async def update_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    _get_writable_knowledge(ctx, knowledge_id)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    if "title" in data:
        updates["title"] = str(data["title"])
    if "description" in data:
        updates["description"] = str(data["description"])
    if "custom_metadata" in data:
        updates["custom_metadata"] = dumps(data["custom_metadata"])
    with db.transaction() as conn:
        db.update("knowledges", updates, "id=?", (knowledge_id,), conn)
    return get_knowledge(knowledge_id, request)


@router.post("/knowledge/{knowledge_id}/regenerate-summary")
def regenerate_knowledge_summary(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _get_writable_knowledge(ctx, knowledge_id)
    chunks = db.fetch_all(
        "SELECT content FROM chunks WHERE knowledge_id=? AND tenant_id=? AND deleted_at IS NULL AND is_enabled=1 ORDER BY chunk_index",
        (knowledge_id, ctx.tenant_id),
    )
    source = " ".join(str(chunk.get("content") or "").strip() for chunk in chunks).strip()
    if not source:
        source = str(row.get("description") or "").strip()
    if not source:
        source = str(row.get("title") or row.get("file_name") or "该文档").strip()
    source = re.sub(r"\s+", " ", source)
    kb = _get_writable_kb(ctx, str(row["knowledge_base_id"]))
    vlm_config = loads(kb.get("vlm_config"), {})
    language = (
        str(vlm_config.get("description_language") or "").strip()
        if isinstance(vlm_config, dict)
        else ""
    )
    if not language:
        locale = resolve_locale(request.headers.get("accept-language", ""))
        language = {
            "zh": "Chinese",
            "en": "English",
            "ja": "Japanese",
            "ko": "Korean",
            "ru": "Russian",
        }.get(locale.split("-", 1)[0].lower(), "English")
    runtime = model_runtime.chat_runtime(
        ctx.tenant_id,
        {"model_id": str(kb.get("summary_model_id") or "")},
    )
    try:
        description = document_summary.generate_summary(
            runtime,
            source,
            language=language,
            custom_metadata=str(row.get("custom_metadata") or ""),
        )
    except Exception:  # noqa: BLE001 - summary generation must keep a usable fallback
        description = None
    if description:
        description = description.strip()[:4000]
    if not description:
        sentences = [
            value.strip()
            for value in re.split(r"(?<=[。！？.!?])\s+", source)
            if value.strip()
        ]
        description = (sentences[0] if sentences else source)[:240]
        if len(source) > len(description):
            description = description.rstrip("。.!！?？") + "…"
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE knowledges SET description=?, summary_status='completed', updated_at=? WHERE id=?",
            (description, now, knowledge_id),
        )
    updated = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
    return ok(serialize_knowledge(updated))


@router.delete("/knowledge/{knowledge_id}")
def delete_knowledge(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _get_writable_knowledge(ctx, knowledge_id)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE knowledges SET deleted_at=?, parse_status='deleting', updated_at=? WHERE id=?", (now, now, knowledge_id))
        conn.execute("UPDATE chunks SET deleted_at=?, updated_at=? WHERE knowledge_id=?", (now, now, knowledge_id))
        storage_quota.release_knowledge_rows(conn, [row])
    _delete_knowledge_files([row])
    return ok(message="Knowledge deleted")


@router.put("/knowledge/manual/{knowledge_id}")
async def update_manual(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _get_writable_knowledge(ctx, knowledge_id)
    data = await body(request)
    title = str(data.get("title") or row["title"])
    content = str(data.get("content") or "")
    new_storage_size = storage_quota.knowledge_storage_size(
        int(row.get("file_size") or 0), content
    )
    now = db.utcnow()
    with db.transaction() as conn:
        storage_quota.adjust_storage(
            conn,
            ctx.tenant_id,
            int(row.get("storage_size") or 0),
            new_storage_size,
        )
        conn.execute(
            "UPDATE knowledges SET title=?, storage_size=?, updated_at=?, processed_at=?, parse_status='completed' WHERE id=?",
            (title, new_storage_size, now, now, knowledge_id),
        )
        conn.execute("DELETE FROM chunks WHERE knowledge_id=?", (knowledge_id,))
        _insert_chunks(conn, ctx.tenant_id, row["knowledge_base_id"], knowledge_id, content, 512, 50)
    _bind_knowledge_resources(ctx.tenant_id, knowledge_id, text=content)
    updated = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
    ingest_knowledge(_get_kb(ctx, row["knowledge_base_id"]), updated)
    return ok(serialize_knowledge(updated))


@router.post("/knowledge/{knowledge_id}/reparse")
async def reparse(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = _get_writable_knowledge(ctx, knowledge_id)
    kb = _get_kb(ctx, row["knowledge_base_id"])
    data = await body(request)
    process_config = data.get("process_config")
    if process_config is not None:
        if not isinstance(process_config, dict):
            raise validation("Invalid process_config format")
        _validate_process_config(process_config)
        _validate_file_process_admission(
            _merge_process_config(kb, process_config),
            str(row.get("file_name") or ""),
            str(row.get("file_type") or ""),
        )
        metadata = loads(row.get("metadata"), {})
        metadata = dict(metadata) if isinstance(metadata, dict) else {}
        metadata["process_overrides"] = process_config
        with db.transaction() as conn:
            conn.execute(
                "UPDATE knowledges SET metadata=?, updated_at=? WHERE id=?",
                (dumps(metadata), db.utcnow(), knowledge_id),
            )
        row = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,)) or row

    overrides = (
        process_config
        if process_config is not None
        else _knowledge_process_overrides(row)
    )
    effective_kb = _merge_process_config(kb, overrides)
    text, image_reference, image_caption, embedded_images = _knowledge_reparse_content(
        effective_kb, row
    )
    old_extracted = _extracted_image_references(knowledge_id, ctx.tenant_id)
    text, extracted_images = _materialize_embedded_images(
        ctx.tenant_id, effective_kb, knowledge_id, text, embedded_images
    )
    has_content = bool(text.strip() or image_reference)
    new_storage_size = storage_quota.knowledge_storage_size(
        int(row.get("file_size") or 0), text
    )
    chunking = loads(effective_kb.get("chunking_config"), {})
    chunk_size = int(chunking.get("chunk_size") or chunking.get("max_chunk_size") or 512)
    overlap = int(chunking.get("chunk_overlap") or chunking.get("overlap_size") or 50)
    with db.transaction() as conn:
        storage_quota.adjust_storage(
            conn,
            ctx.tenant_id,
            int(row.get("storage_size") or 0),
            new_storage_size,
        )
        conn.execute("DELETE FROM chunks WHERE knowledge_id=?", (knowledge_id,))
        chunk_count = 0
        if text.strip():
            chunk_count = _insert_chunks(
                conn,
                ctx.tenant_id,
                row["knowledge_base_id"],
                knowledge_id,
                text,
                chunk_size,
                overlap,
                extracted_images,
            )
        if image_reference:
            _insert_image_chunk(
                conn,
                tenant_id=ctx.tenant_id,
                kb_id=str(row["knowledge_base_id"]),
                knowledge_id=knowledge_id,
                reference=image_reference,
                caption=image_caption,
                chunk_index=chunk_count,
            )
        now = db.utcnow()
        conn.execute(
            "UPDATE knowledges SET parse_status=?, storage_size=?, processed_at=?, updated_at=?, error_message=? WHERE id=?",
            (
                "completed" if has_content else "failed",
                new_storage_size,
                now,
                now,
                "" if has_content else "No extractable text found",
                knowledge_id,
            ),
        )
    updated = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
    if updated:
        _bind_knowledge_resources(
            ctx.tenant_id,
            knowledge_id,
            text=text,
            references=(image_reference,),
            extracted_images=extracted_images,
        )
        _refresh_knowledge_indexes(ctx.tenant_id, effective_kb, updated)
        _queue_summary_generation(
            ctx,
            effective_kb,
            updated,
            text,
            accept_language=request.headers.get("accept-language", ""),
        )
    _remove_cloned_files(old_extracted, ctx.tenant_id, [knowledge_id])
    return ok({"knowledge_id": knowledge_id, "status": "completed" if has_content else "failed"})


@router.post("/knowledge/{knowledge_id}/cancel-parse")
def cancel_parse(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_knowledge(ctx, knowledge_id)
    with db.transaction() as conn:
        conn.execute("UPDATE knowledges SET parse_status='cancelled', updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), knowledge_id, ctx.tenant_id))
    return ok({"status": "cancelled"})


@router.get("/knowledge/{knowledge_id}/download")
def download_knowledge(knowledge_id: str, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    _kb_guard(ctx, "contributor")
    row = _get_readable_knowledge(ctx, knowledge_id)
    kb = _get_readable_kb(ctx, str(row["knowledge_base_id"]))
    _require_kb_write_access(ctx, kb)
    if object_storage.is_object_reference(str(row.get("file_path") or "")):
        return file_proxy.file_response(
            str(row["file_path"]), int(row["tenant_id"]), filename=str(row.get("file_name") or "")
        )
    if row.get("file_path") and Path(row["file_path"]).exists():
        return FileResponse(row["file_path"], filename=row.get("file_name") or Path(row["file_path"]).name)
    return Response(_knowledge_text(knowledge_id), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{knowledge_id}.md"'})


@router.get("/knowledge-bases/{kb_id}/files")
def knowledge_base_file(kb_id: str, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    reference = str(request.query_params.get("file_path") or "").strip()
    if not reference:
        raise validation("missing required parameter: file_path")
    if not _kb_references_file(kb_id, int(kb["tenant_id"]), reference):
        raise forbidden("file not referenced by knowledge base")
    return file_proxy.file_response(reference, int(kb["tenant_id"]))


@router.get("/knowledge/{knowledge_id}/preview")
def preview_knowledge(knowledge_id: str, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    row = _get_readable_knowledge(ctx, knowledge_id)
    if object_storage.is_object_reference(str(row.get("file_path") or "")):
        return file_proxy.file_response(
            str(row["file_path"]), int(row["tenant_id"]), filename=str(row.get("file_name") or "")
        )
    if row.get("file_path") and Path(row["file_path"]).exists():
        media_type = mimetypes.guess_type(row["file_path"])[0] or "application/octet-stream"
        return FileResponse(row["file_path"], media_type=media_type, filename=row.get("file_name") or None)
    return Response(_knowledge_text(knowledge_id), media_type="text/markdown")


@router.get("/knowledge/{knowledge_id}/spans")
def spans(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    knowledge = _get_readable_knowledge(ctx, knowledge_id)
    chunks = db.fetch_all(
        "SELECT * FROM chunks WHERE knowledge_id=? AND tenant_id=? AND deleted_at IS NULL ORDER BY chunk_index",
        (knowledge_id, int(knowledge["tenant_id"])),
    )
    return ok(
        [
            {"id": chunk["id"], "name": f"chunk-{chunk['chunk_index']}", "status": "completed", "started_at": chunk["created_at"], "finished_at": chunk["updated_at"]}
            for chunk in chunks
        ]
    )


@router.put("/knowledge/image/{knowledge_id}/{chunk_id}")
async def update_image(knowledge_id: str, chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_chunk(ctx, chunk_id, knowledge_id)
    data = await body(request)
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET image_info=?, updated_at=? WHERE id=? AND knowledge_id=? AND tenant_id=?", (dumps(data), db.utcnow(), chunk_id, knowledge_id, ctx.tenant_id))
    _refresh_image_vectors(knowledge_id, ctx.tenant_id)
    return ok(message="Image information updated")


@router.get("/chunks/{knowledge_id}")
def list_chunks(knowledge_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    knowledge = _get_readable_knowledge(ctx, knowledge_id)
    tenant_id = int(knowledge["tenant_id"])
    p, size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 200)
    total = db.fetch_one("SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND tenant_id=? AND deleted_at IS NULL", (knowledge_id, tenant_id))["n"]
    rows = db.fetch_all(
        "SELECT * FROM chunks WHERE knowledge_id=? AND tenant_id=? AND deleted_at IS NULL ORDER BY chunk_index LIMIT ? OFFSET ?",
        (knowledge_id, tenant_id, size, offset),
    )
    return page([serialize_chunk(row) for row in rows], int(total), p, size)


@router.get("/chunks/by-id/{chunk_id}")
def get_chunk(chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(serialize_chunk(_get_readable_chunk(ctx, chunk_id)))


@router.put("/chunks/by-id/{chunk_id}/questions")
async def upsert_chunk_question(chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_chunk(ctx, chunk_id)
    data = await body(request)
    question_text = str(data.get("question") or "").strip()
    if not question_text:
        raise validation("question is required")
    question_id = str(data.get("question_id") or "").strip()
    chunk, questions = _chunk_questions(chunk_id, ctx.tenant_id)
    revision = int(chunk.get("content_revision") or 1)
    if question_id:
        item = next((value for value in questions if value.get("id") == question_id), None)
        if not item:
            raise validation("question not found")
        item["question"] = question_text
        item["content_revision"] = revision
    else:
        item = {
            "id": db.new_id(),
            "question": question_text,
            "content_revision": revision,
        }
        questions.append(item)
    with db.transaction() as conn:
        _save_chunk_questions(conn, chunk, questions)
    return ok(item)


@router.delete("/chunks/by-id/{chunk_id}/questions")
async def delete_chunk_question(chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    question_id = str(request.query_params.get("question_id") or "").strip()
    if not question_id:
        data = await body(request)
        question_id = str(data.get("question_id") or "").strip()
    if not question_id:
        raise validation("question_id is required")
    _get_writable_chunk(ctx, chunk_id)
    chunk, questions = _chunk_questions(chunk_id, ctx.tenant_id)
    remaining = [value for value in questions if value.get("id") != question_id]
    if len(remaining) == len(questions):
        raise validation("question not found")
    with db.transaction() as conn:
        _save_chunk_questions(conn, chunk, remaining)
    return ok(message="Generated question deleted")


@router.post("/chunks/by-id/{chunk_id}/questions/regenerate")
def regenerate_chunk_questions(chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_chunk(ctx, chunk_id)
    chunk, _ = _chunk_questions(chunk_id, ctx.tenant_id)
    content = str(chunk.get("content") or "").strip()
    if not content:
        raise validation("chunk content is empty")
    sentences = [
        value.strip()
        for value in re.split(r"(?<=[。！？.!?])\s+|\n+", content)
        if value.strip()
    ]
    focus = (sentences[0] if sentences else content)[:80]
    revision = int(chunk.get("content_revision") or 1)
    questions = [
        {"id": db.new_id(), "question": f"{focus}的核心内容是什么？", "content_revision": revision},
        {"id": db.new_id(), "question": "这段内容可以解决哪些问题？", "content_revision": revision},
        {"id": db.new_id(), "question": f"如何理解「{focus}」？", "content_revision": revision},
    ]
    with db.transaction() as conn:
        _save_chunk_questions(conn, chunk, questions)
    return ok(questions)


@router.put("/chunks/{knowledge_id}/{chunk_id}")
async def update_chunk(knowledge_id: str, chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    data = await body(request)
    row = _get_writable_chunk(ctx, chunk_id, knowledge_id)
    expected = data.get("expected_revision")
    if expected is not None and int(expected) != int(row.get("content_revision") or 1):
        raise validation("Chunk has changed; reload before saving")
    content = str(data.get("content") or row["content"])
    enabled = bool(data.get("is_enabled", row.get("is_enabled")))
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO chunk_revisions
            (id, tenant_id, knowledge_base_id, knowledge_id, chunk_id, revision,
             content, is_enabled, editor_id, edit_source, edited_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'edit', ?, ?)
            """,
            (
                db.new_id(), ctx.tenant_id, row["knowledge_base_id"], knowledge_id,
                chunk_id, row.get("content_revision") or 1, row["content"],
                1 if row.get("is_enabled") else 0, ctx.user_id, now, now,
            ),
        )
        conn.execute(
            "UPDATE chunks SET content=?, is_enabled=?, content_revision=content_revision+1, updated_at=? WHERE id=?",
            (content, 1 if enabled else 0, now, chunk_id),
        )
    updated = db.fetch_one("SELECT * FROM chunks WHERE id=?", (chunk_id,))
    return ok(serialize_chunk(updated))


@router.delete("/chunks/{knowledge_id}/{chunk_id}")
def delete_chunk(knowledge_id: str, chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_chunk(ctx, chunk_id, knowledge_id)
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET deleted_at=?, updated_at=? WHERE id=? AND knowledge_id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), chunk_id, knowledge_id, ctx.tenant_id))
    _refresh_image_vectors(knowledge_id, ctx.tenant_id)
    return ok(message="Chunk deleted")


@router.delete("/chunks/{chunk_id}")
def delete_chunk_by_id(chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_chunk(ctx, chunk_id)
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), chunk_id, ctx.tenant_id))
    row = db.fetch_one("SELECT knowledge_id FROM chunks WHERE id=?", (chunk_id,))
    if row:
        _refresh_image_vectors(str(row["knowledge_id"]), ctx.tenant_id)
    return ok(message="Chunk deleted")


@router.get("/chunks/{knowledge_id}/{chunk_id}/revisions")
def chunk_revisions(knowledge_id: str, chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_readable_chunk(ctx, chunk_id, knowledge_id)
    rows = db.fetch_all(
        "SELECT * FROM chunk_revisions WHERE chunk_id=? ORDER BY revision DESC",
        (chunk_id,),
    )
    return ok(rows)


@router.post("/chunks/{knowledge_id}/{chunk_id}/revert")
async def revert_chunk(knowledge_id: str, chunk_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_chunk(ctx, chunk_id, knowledge_id)
    data = await body(request)
    revision = db.fetch_one(
        "SELECT * FROM chunk_revisions WHERE chunk_id=? AND revision=?",
        (chunk_id, int(data.get("revision") or 0)),
    )
    if not revision:
        raise not_found("Revision not found")
    with db.transaction() as conn:
        conn.execute("UPDATE chunks SET content=?, content_revision=content_revision+1, updated_at=? WHERE id=?", (revision["content"], db.utcnow(), chunk_id))
    return get_chunk(chunk_id, request)


@router.get("/knowledge-bases/{kb_id}/tags")
def list_tags(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    rows = db.fetch_all("SELECT * FROM knowledge_tags WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL ORDER BY sort_order, seq_id", (kb_id, int(kb["tenant_id"])))
    return ok(rows)


@router.post("/knowledge-bases/{kb_id}/tags", status_code=201)
async def create_tag(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("Tag name is required")
    now = db.utcnow()
    tag_id = db.new_id()
    seq = db.fetch_one("SELECT COALESCE(MAX(seq_id), 0)+1 AS n FROM knowledge_tags")["n"]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO knowledge_tags(id, tenant_id, knowledge_base_id, name, color, sort_order, seq_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tag_id, ctx.tenant_id, kb_id, name, str(data.get("color") or "#07C05F"), int(data.get("sort_order") or 0), seq, now, now),
        )
    return ok(db.fetch_one("SELECT * FROM knowledge_tags WHERE id=?", (tag_id,)))


@router.put("/knowledge-bases/{kb_id}/tags/{tag_id}")
async def update_tag(kb_id: str, tag_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "color", "sort_order"):
        if key in data:
            updates[key] = data[key]
    with db.transaction() as conn:
        db.update("knowledge_tags", updates, "id=? AND knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL", (tag_id, kb_id, ctx.tenant_id), conn)
    row = db.fetch_one(
        "SELECT * FROM knowledge_tags WHERE id=? AND knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (tag_id, kb_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Tag not found")
    return ok(row)


@router.delete("/knowledge-bases/{kb_id}/tags/{tag_id}")
def delete_tag(kb_id: str, tag_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute("UPDATE knowledge_tags SET deleted_at=?, updated_at=? WHERE id=? AND knowledge_base_id=? AND tenant_id=?", (now, now, tag_id, kb_id, ctx.tenant_id))
        conn.execute("DELETE FROM knowledge_tag_relations WHERE tag_id=?", (tag_id,))
    return ok(message="Tag deleted")


@router.get("/knowledge-bases/{kb_id}/faq/entries")
def faq_list(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    p, size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 500)
    keyword = (request.query_params.get("keyword") or "").strip()
    clauses = ["tenant_id=?", "knowledge_base_id=?", "deleted_at IS NULL"]
    args: list[Any] = [int(kb["tenant_id"]), kb_id]
    if keyword:
        clauses.append("(lower(standard_question) LIKE ? OR lower(answer) LIKE ?)")
        args.extend([f"%{keyword.lower()}%", f"%{keyword.lower()}%"])
    if request.query_params.get("is_enabled") is not None:
        clauses.append("is_enabled=?")
        args.append(1 if truthy(request.query_params["is_enabled"]) else 0)
    if request.query_params.get("tag_id"):
        clauses.append("tag_id=?")
        args.append(int(request.query_params["tag_id"]))
    where = " AND ".join(clauses)
    total = db.fetch_one(f"SELECT COUNT(*) AS n FROM py_faq_entries WHERE {where}", args)["n"]
    rows = db.fetch_all(f"SELECT * FROM py_faq_entries WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?", [*args, size, offset])
    for row in rows:
        row["similar_questions"] = loads(row.get("similar_questions"), [])
        row["is_enabled"] = bool(row.get("is_enabled"))
        row["is_recommended"] = bool(row.get("is_recommended"))
    return page(rows, int(total), p, size)


def _faq_insert(conn: Any, tenant_id: int, kb_id: str, entry: dict[str, Any]) -> int:
    now = db.utcnow()
    cur = conn.execute(
        """
        INSERT INTO py_faq_entries(tenant_id, knowledge_base_id, standard_question, answer,
          similar_questions, tag_id, is_enabled, is_recommended, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id, kb_id, str(entry.get("standard_question") or entry.get("question") or ""),
            str(entry.get("answer") or ""), dumps(entry.get("similar_questions") or []),
            entry.get("tag_id"), 1 if entry.get("is_enabled", True) else 0,
            1 if entry.get("is_recommended") else 0, now, now,
        ),
    )
    return int(cur.lastrowid)


def _faq_tag_seq_id(tenant_id: int, kb_id: str, raw: Any) -> int | None:
    if raw in (None, "", 0):
        return None
    try:
        seq_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise validation("Tag ID is invalid") from exc
    row = db.fetch_one(
        "SELECT 1 FROM knowledge_tags WHERE tenant_id=? AND knowledge_base_id=? "
        "AND seq_id=? AND deleted_at IS NULL",
        (tenant_id, kb_id, seq_id),
    )
    if not row:
        raise validation("Tag does not belong to the knowledge base")
    return seq_id


def _faq_failure(index: int, entry: Any, reason: str) -> dict[str, Any]:
    item = entry if isinstance(entry, dict) else {}
    return {
        "index": index,
        "reason": reason,
        "standard_question": str(item.get("standard_question") or item.get("question") or ""),
        "similar_questions": item.get("similar_questions") if isinstance(item.get("similar_questions"), list) else [],
        "answers": [str(item.get("answer") or "")] if item.get("answer") else [],
    }


def _faq_clean_entries(
    tenant_id: int,
    kb_id: str,
    entries: list[Any],
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tags = {
        int(row["seq_id"]): str(row.get("name") or "")
        for row in db.fetch_all(
            "SELECT seq_id, name FROM knowledge_tags WHERE tenant_id=? AND knowledge_base_id=? "
            "AND seq_id IS NOT NULL AND deleted_at IS NULL",
            (tenant_id, kb_id),
        )
    }
    existing: set[str] = set()
    if mode == "append":
        for row in db.fetch_all(
            "SELECT standard_question FROM py_faq_entries WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL",
            (tenant_id, kb_id),
        ):
            existing.add(re.sub(r"\s+", " ", str(row.get("standard_question") or "")).strip().casefold())

    valid: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            failed.append(_faq_failure(index, raw, "条目必须是对象"))
            continue
        question = re.sub(r"\s+", " ", str(raw.get("standard_question") or raw.get("question") or "")).strip()
        answer = str(raw.get("answer") or "").strip()
        if not answer and isinstance(raw.get("answers"), list):
            answer = next((str(value).strip() for value in raw["answers"] if str(value).strip()), "")
        if not question:
            failed.append(_faq_failure(index, raw, "标准问题不能为空"))
            continue
        if not answer:
            failed.append(_faq_failure(index, raw, "答案不能为空"))
            continue
        key = question.casefold()
        if key in seen:
            failed.append(_faq_failure(index, raw, "批次内标准问题重复"))
            continue
        if key in existing:
            failed.append(_faq_failure(index, raw, "知识库中已存在相同标准问题"))
            continue
        tag_id = raw.get("tag_id")
        if tag_id not in (None, "", 0):
            try:
                tag_id = int(tag_id)
            except (TypeError, ValueError):
                failed.append(_faq_failure(index, raw, "标签 ID 无效"))
                continue
            if tag_id not in tags:
                failed.append(_faq_failure(index, raw, "标签不属于当前知识库"))
                continue
        else:
            tag_id = None
        seen.add(key)
        valid.append(
            {
                "standard_question": question,
                "answer": answer,
                "similar_questions": [
                    str(value).strip()
                    for value in raw.get("similar_questions") or []
                    if str(value).strip()
                ],
                "tag_id": tag_id,
                "is_enabled": truthy(raw.get("is_enabled", True)),
                "is_recommended": truthy(raw.get("is_recommended", False)),
            }
        )
    return valid, failed


def _faq_save_progress(
    task_id: str,
    *,
    status: str,
    progress: dict[str, Any],
    error: str = "",
) -> None:
    _save_worker_progress(task_id, status=status, progress=progress, error=error)


def _faq_task_stopped(task_id: str) -> bool:
    return _worker_task_stopped(task_id)


async def _run_faq_import(task_id: str) -> None:
    row = db.fetch_one("SELECT * FROM py_task_results WHERE id=? AND kind='faq_import'", (task_id,))
    if not row:
        return
    if str(row.get("status") or "") not in {"pending", "processing"}:
        return
    payload = loads(row.get("payload"), {})
    if not isinstance(payload, dict):
        payload = {}
    tenant_id = int(payload.get("tenant_id") or row.get("tenant_id") or 0)
    kb_id = str(payload.get("kb_id") or "")
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    mode = str(payload.get("mode") or "append")
    dry_run = bool(payload.get("dry_run"))
    started = time.monotonic()
    progress: dict[str, Any] = {
        "task_id": task_id,
        "kb_id": kb_id,
        "status": "processing",
        "progress": 0,
        "total": len(entries),
        "processed": 0,
        "success_count": 0,
        "failed_count": 0,
        "failed_entries": [],
        "message": "正在验证条目...",
        "dry_run": dry_run,
        "import_mode": mode,
    }
    _faq_save_progress(task_id, status="processing", progress=progress)
    await asyncio.sleep(0)
    if _faq_task_stopped(task_id):
        return
    try:
        valid, failed = _faq_clean_entries(tenant_id, kb_id, entries, mode)
        if _faq_task_stopped(task_id):
            return
        progress.update(
            {
                "processed": len(entries),
                "success_count": len(valid),
                "failed_count": len(failed),
                "failed_entries": failed,
            }
        )
        if dry_run:
            if _faq_task_stopped(task_id):
                return
            progress.update(
                {
                    "status": "completed",
                    "progress": 100,
                    "message": "验证完成",
                    "imported_at": db.utcnow(),
                    "processing_time": int((time.monotonic() - started) * 1000),
                }
            )
            _faq_save_progress(task_id, status="completed", progress=progress)
            return

        if mode == "replace" and valid:
            with db.transaction() as conn:
                current = conn.execute(
                    "SELECT status FROM py_task_results WHERE id=?", (task_id,)
                ).fetchone()
                if not current or str(current["status"]) not in {"pending", "processing"}:
                    return
                conn.execute(
                    "UPDATE py_faq_entries SET deleted_at=?, updated_at=? "
                    "WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL",
                    (db.utcnow(), db.utcnow(), tenant_id, kb_id),
                )

        ids: list[int] = []
        for offset in range(0, len(valid), 50):
            if _faq_task_stopped(task_id):
                return
            batch = valid[offset:offset + 50]
            with db.transaction() as conn:
                current = conn.execute(
                    "SELECT status FROM py_task_results WHERE id=?", (task_id,)
                ).fetchone()
                if not current or str(current["status"]) not in {"pending", "processing"}:
                    return
                ids.extend(_faq_insert(conn, tenant_id, kb_id, entry) for entry in batch)
            progress["processed"] = len(failed) + offset + len(batch)
            progress["progress"] = min(99, int(progress["processed"] * 100 / max(1, len(entries))))
            progress["message"] = "正在导入条目..."
            _faq_save_progress(task_id, status="processing", progress=progress)
            await asyncio.sleep(0)
            if _faq_task_stopped(task_id):
                return

        if _faq_task_stopped(task_id):
            return
        progress.update(
            {
                "status": "completed",
                "progress": 100,
                "processed": len(entries),
                "success_count": len(ids),
                "imported_at": db.utcnow(),
                "processing_time": int((time.monotonic() - started) * 1000),
                "message": f"导入完成，成功 {len(ids)} 条，失败 {len(failed)} 条",
            }
        )
        _faq_save_progress(task_id, status="completed", progress=progress)
    except Exception as exc:  # noqa: BLE001
        if _faq_task_stopped(task_id):
            return
        _save_worker_failure(
            task_id,
            progress=progress,
            message="导入失败",
            error=str(exc),
        )


async def _dispatch_faq_import(task_id: str) -> None:
    await _run_faq_import(task_id)


TASK_WORKERS["faq_import"] = _dispatch_faq_import


@router.post("/knowledge-bases/{kb_id}/faq/entries")
async def faq_upsert(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    if not entries:
        raise validation("FAQ 条目不能为空")
    mode = str(data.get("mode") or "append").strip().lower()
    if mode not in {"append", "replace"}:
        raise validation("模式仅支持 append 或 replace")
    task_id = str(data.get("task_id") or "").strip() or db.new_id()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", task_id):
        raise validation("task_id 格式不合法")
    if db.fetch_one("SELECT 1 FROM py_task_results WHERE id=?", (task_id,)):
        raise validation("task_id 已存在")
    running = db.fetch_all(
        "SELECT payload FROM py_task_results WHERE tenant_id=? AND kind='faq_import' "
        "AND status IN ('pending','processing')",
        (ctx.tenant_id,),
    )
    for row in running:
        payload = loads(row.get("payload"), {})
        if isinstance(payload, dict) and str(payload.get("kb_id") or "") == kb_id:
            raise validation("该知识库已有导入任务正在进行中，请等待完成后再试")

    now = db.utcnow()
    payload = {
        "tenant_id": ctx.tenant_id,
        "kb_id": kb_id,
        "entries": entries,
        "mode": mode,
        "dry_run": bool(data.get("dry_run")),
    }
    result = {
        "task_id": task_id,
        "kb_id": kb_id,
        "status": "pending",
        "progress": 0,
        "total": len(entries),
        "processed": 0,
        "success_count": 0,
        "failed_count": 0,
        "message": "任务已创建，等待处理",
        "dry_run": payload["dry_run"],
        "import_mode": mode,
    }
    max_retry = 3 if payload["dry_run"] else 5
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_task_results(id, tenant_id, kind, status, total, processed, payload, result, error, max_retry, created_at, updated_at) "
            "VALUES (?, ?, 'faq_import', 'pending', ?, 0, ?, ?, '', ?, ?, ?)",
            (
                task_id,
                ctx.tenant_id,
                len(entries),
                dumps(payload),
                dumps(result),
                max_retry,
                now,
                now,
            ),
        )
    schedule_task_worker(task_id)
    return ok({"task_id": task_id})


@router.get("/knowledge-bases/{kb_id}/faq/entries/{entry_id:int}")
def faq_get(kb_id: str, entry_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    row = db.fetch_one(
        "SELECT * FROM py_faq_entries WHERE id=? AND knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (entry_id, kb_id, int(kb["tenant_id"])),
    )
    if not row:
        raise not_found("FAQ entry not found")
    row["similar_questions"] = loads(row.get("similar_questions"), [])
    row["is_enabled"] = bool(row.get("is_enabled"))
    row["is_recommended"] = bool(row.get("is_recommended"))
    return ok(row)


@router.post("/knowledge-bases/{kb_id}/faq/entry", status_code=201)
async def faq_create(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    data["tag_id"] = _faq_tag_seq_id(ctx.tenant_id, kb_id, data.get("tag_id"))
    with db.transaction() as conn:
        entry_id = _faq_insert(conn, ctx.tenant_id, kb_id, data)
    row = db.fetch_one("SELECT * FROM py_faq_entries WHERE id=?", (entry_id,))
    row["similar_questions"] = loads(row.get("similar_questions"), [])
    return ok(row)


@router.put("/knowledge-bases/{kb_id}/faq/entries/{entry_id:int}")
async def faq_update(kb_id: str, entry_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    if "standard_question" in data or "question" in data:
        updates["standard_question"] = str(data.get("standard_question") or data.get("question") or "")
    if "answer" in data:
        updates["answer"] = str(data["answer"])
    if "similar_questions" in data:
        updates["similar_questions"] = dumps(data["similar_questions"])
    if "tag_id" in data:
        updates["tag_id"] = _faq_tag_seq_id(ctx.tenant_id, kb_id, data["tag_id"])
    if "is_enabled" in data:
        updates["is_enabled"] = 1 if data["is_enabled"] else 0
    if "is_recommended" in data:
        updates["is_recommended"] = 1 if data["is_recommended"] else 0
    with db.transaction() as conn:
        db.update("py_faq_entries", updates, "id=? AND tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL", (entry_id, ctx.tenant_id, kb_id), conn)
    row = db.fetch_one(
        "SELECT * FROM py_faq_entries WHERE id=? AND tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL",
        (entry_id, ctx.tenant_id, kb_id),
    )
    if not row:
        raise not_found("FAQ entry not found")
    row["similar_questions"] = loads(row.get("similar_questions"), [])
    return ok(row)


@router.delete("/knowledge-bases/{kb_id}/faq/entries")
async def faq_delete(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    ids = [int(value) for value in data.get("ids") or []]
    with db.transaction() as conn:
        for entry_id in ids:
            conn.execute("UPDATE py_faq_entries SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=? AND knowledge_base_id=?", (db.utcnow(), db.utcnow(), entry_id, ctx.tenant_id, kb_id))
    return ok({"deleted": len(ids)})


@router.put("/knowledge-bases/{kb_id}/faq/entries/fields")
async def faq_update_fields(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    with db.transaction() as conn:
        for raw_id, changes in by_id.items():
            updates: dict[str, Any] = {"updated_at": db.utcnow()}
            for key in ("is_enabled", "is_recommended"):
                if key in changes:
                    updates[key] = 1 if changes[key] else 0
            if "tag_id" in changes:
                updates["tag_id"] = _faq_tag_seq_id(ctx.tenant_id, kb_id, changes["tag_id"])
            db.update("py_faq_entries", updates, "id=? AND tenant_id=? AND knowledge_base_id=?", (int(raw_id), ctx.tenant_id, kb_id), conn)
    return ok({"updated": len(by_id)})


@router.put("/knowledge-bases/{kb_id}/faq/entries/tags")
async def faq_update_tags(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    updates = data.get("updates") if isinstance(data.get("updates"), dict) else {}
    with db.transaction() as conn:
        for raw_id, tag_id in updates.items():
            seq_id = _faq_tag_seq_id(ctx.tenant_id, kb_id, tag_id)
            conn.execute("UPDATE py_faq_entries SET tag_id=?, updated_at=? WHERE id=? AND tenant_id=? AND knowledge_base_id=?", (seq_id, db.utcnow(), int(raw_id), ctx.tenant_id, kb_id))
    return ok({"updated": len(updates)})


@router.post("/knowledge-bases/{kb_id}/faq/entries/{entry_id}/similar-questions")
async def faq_similar(kb_id: str, entry_id: int, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    row = db.fetch_one("SELECT similar_questions FROM py_faq_entries WHERE id=? AND tenant_id=? AND knowledge_base_id=?", (entry_id, ctx.tenant_id, kb_id))
    if not row:
        raise not_found("FAQ entry not found")
    questions = loads(row.get("similar_questions"), [])
    questions.extend(data.get("questions") or [data.get("question")] if data.get("question") else [])
    questions = list(dict.fromkeys(str(value) for value in questions if value))
    with db.transaction() as conn:
        conn.execute("UPDATE py_faq_entries SET similar_questions=?, updated_at=? WHERE id=?", (dumps(questions), db.utcnow(), entry_id))
    return ok({"similar_questions": questions})


@router.post("/knowledge-bases/{kb_id}/faq/search")
async def faq_search(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    data = await body(request)
    query = str(data.get("query_text") or data.get("query") or "").strip().lower()
    rows = db.fetch_all("SELECT * FROM py_faq_entries WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL AND is_enabled=1", (int(kb["tenant_id"]), kb_id))
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        haystack = (row["standard_question"] + " " + row["answer"] + " " + row["similar_questions"]).lower()
        score = sum(1 for term in re.findall(r"[\w\u3400-\u9fff]{2,}", query) if term in haystack)
        if score:
            row["similar_questions"] = loads(row.get("similar_questions"), [])
            scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return ok([row for _, row in scored[: int(data.get("match_count") or 5)]])


@router.get("/knowledge-bases/{kb_id}/faq/entries/export")
def faq_export(kb_id: str, request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _get_readable_kb(ctx, kb_id)
    rows = db.fetch_all("SELECT * FROM py_faq_entries WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL ORDER BY id", (int(kb["tenant_id"]), kb_id))
    if request.query_params.get("format") == "json":
        for row in rows:
            row["similar_questions"] = loads(row.get("similar_questions"), [])
        return Response(dumps(rows), media_type="application/json", headers={"Content-Disposition": "attachment; filename=faq.json"})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["standard_question", "answer", "similar_questions", "tag_id", "is_enabled"])
    for row in rows:
        writer.writerow([row["standard_question"], row["answer"], row["similar_questions"], row.get("tag_id") or "", row["is_enabled"]])
    return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=faq.csv"})


@router.get("/faq/import/progress/{task_id}")
def faq_progress(task_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one(
        "SELECT * FROM py_task_results WHERE id=? AND tenant_id=? AND kind='faq_import'",
        (task_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("FAQ import task not found")
    result = loads(row.get("result"), {})
    if not isinstance(result, dict):
        result = {}
    result.setdefault("task_id", task_id)
    result.setdefault("status", row.get("status") or "completed")
    result.setdefault("progress", 100 if row.get("status") == "completed" else 0)
    result.setdefault("total", int(row.get("total") or 0))
    result.setdefault("processed", int(row.get("processed") or 0))
    if row.get("error"):
        result.setdefault("message", row["error"])
    return ok(result)


@router.put("/knowledge-bases/{kb_id}/faq/import/last-result/display")
async def faq_import_display(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _get_writable_kb(ctx, kb_id)
    data = await body(request)
    display_status = str(data.get("display_status") or "").strip().lower()
    if display_status not in {"open", "close"}:
        raise validation("invalid display status, must be 'open' or 'close'")
    rows = db.fetch_all(
        "SELECT * FROM py_task_results WHERE tenant_id=? AND kind='faq_import' ORDER BY created_at DESC, rowid DESC",
        (ctx.tenant_id,),
    )
    for row in rows:
        payload = loads(row.get("payload"), {})
        result = loads(row.get("result"), {})
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(result, dict):
            result = {}
        if str(payload.get("kb_id") or result.get("kb_id") or "") != kb_id:
            continue
        result["display_status"] = display_status
        with db.transaction() as conn:
            conn.execute(
                "UPDATE py_task_results SET result=?, updated_at=? WHERE id=?",
                (dumps(result), db.utcnow(), row["id"]),
            )
        return ok(message="Updated")
    knowledge = db.fetch_one(
        "SELECT * FROM knowledges WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL "
        "AND last_faq_import_result IS NOT NULL ORDER BY updated_at DESC, rowid DESC LIMIT 1",
        (ctx.tenant_id, kb_id),
    )
    if knowledge:
        result = loads(knowledge.get("last_faq_import_result"), {})
        if isinstance(result, dict) and result:
            result["display_status"] = display_status
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledges SET last_faq_import_result=?, updated_at=? WHERE id=?",
                    (dumps(result), db.utcnow(), knowledge["id"]),
                )
            return ok(message="Updated")
    raise not_found("FAQ import result not found")


@router.get("/chunker/preview")
def chunker_preview(request: Request) -> dict[str, Any]:
    parse_auth(request)
    config: dict[str, Any] = {}
    raw_config = str(request.query_params.get("chunking_config") or "").strip()
    if raw_config:
        try:
            value = json.loads(raw_config)
            if isinstance(value, dict):
                config = value
        except ValueError:
            raise validation("invalid chunking_config")
    text = chunker_adaptive.normalize_line_endings(str(request.query_params.get("text") or ""))
    if len(text) > chunker_adaptive.MAX_PREVIEW_CHARS:
        return JSONResponse(
            {"success": False, "error": "text exceeds preview limit", "limit": chunker_adaptive.MAX_PREVIEW_CHARS},
            status_code=413,
        )
    return ok(chunker_adaptive.preview(text, config))


@router.post("/chunker/preview")
async def chunker_preview_post(request: Request) -> Any:
    parse_auth(request)
    data = await body(request)
    text = chunker_adaptive.normalize_line_endings(str(data.get("text") or ""))
    if not text.strip():
        raise validation("text is empty — paste a sample to preview chunking")
    if len(text) > chunker_adaptive.MAX_PREVIEW_CHARS:
        return JSONResponse(
            {"success": False, "error": "text exceeds preview limit", "limit": chunker_adaptive.MAX_PREVIEW_CHARS},
            status_code=413,
        )
    config = data.get("chunking_config") if isinstance(data.get("chunking_config"), dict) else {}
    return ok(chunker_adaptive.preview(text, config))


@router.get("/image-attrs/schema")
def image_attrs(request: Request) -> dict[str, Any]:
    parse_auth(request)
    return ok(
        {
            "version": "attrs/2",
            "prompt": "observe/1",
            "attributes": [
                {"name": "contain.text", "type": "extent", "label": "Text in the image", "description": "How much body text the picture carries."},
                {"name": "contain.data_visual", "type": "presence", "label": "Data visual", "description": "Whether the picture conveys data as a chart or diagram."},
            ],
            "default_actions": {"ocr": {"on": [{"prop": "contain.text", "is": "block"}], "on_unobserved": True}},
        }
    )

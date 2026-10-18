"""Models, connectors, MCP, storage, vector/search providers and embed channels."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import mimetypes
import os
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import anyio.to_thread
from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

from .. import config, db
from ..api_keys import CHAT, INGEST, READ_AGENTS, RETRIEVE
from ..deps import parse_auth, require_role
from ..errors import conflict, forbidden, not_found, validation
from ..services import connectivity
from ..services import agents as agent_service
from ..services import datasource as datasource_connector
from ..services import docker_templates
from ..services import image_multimodal
from ..services import mcp as mcp_client
from ..services import mcp_oauth
from ..services import model_runtime
from ..services import ollama as ollama_service
from ..services import remote_sandbox
from ..services import storage_quota
from ..services import url_guard
from ..services import vector_store_catalog
from ..services import web_search_catalog
from ..services import web_search as web_search_service
from ..services import wechat as wechat_service
from ..services import weknoracloud as weknoracloud_service
from ..services.access import readable_knowledge_base
from ..utils import dumps, loads, ok


router = APIRouter(tags=["infrastructure"])


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


_STORAGE_SECRET_FIELDS = ("access_key_id", "secret_access_key", "access_key", "secret_key")
_VECTOR_SECRET_FIELDS = ("password", "api_key")
_ENV_VECTOR_STORE_READONLY = (
    "environment-configured vector stores cannot be modified via API"
)
_STORAGE_PROVIDERS = ("local", "minio", "cos", "tos", "s3", "oss", "ks3", "obs")


def _is_redacted_secret(value: Any) -> bool:
    if not value:
        return True
    text = str(value).strip()
    return not text or not text.strip("*•·● ")


def _masked_config(config_data: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    result = dict(config_data) if isinstance(config_data, dict) else {}
    for field in fields:
        if result.get(field):
            result[field] = "***"
    return result


def _merge_secrets(incoming: dict[str, Any], stored: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    merged = dict(incoming)
    for field in fields:
        if _is_redacted_secret(merged.get(field)) and stored.get(field):
            merged[field] = stored[field]
    return merged


def _storage_response(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["config"] = _masked_config(loads(row.get("config"), {}), _STORAGE_SECRET_FIELDS)
    result["source"] = row.get("source") or "user"
    result["legacy_alias"] = bool(row.get("legacy_alias"))
    return result


def _vector_store_response(row: dict[str, Any], *, mask: bool = True) -> dict[str, Any]:
    result = dict(row)
    config_data = loads(row.get("connection_config"), {})
    result["connection_config"] = _masked_config(config_data, _VECTOR_SECRET_FIELDS) if mask else config_data
    result["index_config"] = loads(row.get("index_config"), {})
    result["source"] = row.get("source") or "user"
    result["readonly"] = bool(row.get("readonly"))
    return result


def _validate_vector_store_ssrf(
    engine_type: str, connection: dict[str, Any]
) -> None:
    for raw_address in vector_store_catalog.vector_store_addresses(
        engine_type, connection
    ):
        address = str(raw_address or "").strip()
        if not address:
            continue
        normalized = address if "://" in address else f"https://{address}"
        try:
            url_guard.validate_url_for_ssrf(normalized)
        except url_guard.URLGuardError as exc:
            raise validation(
                f"vector store address {address} failed SSRF validation: {exc}"
            ) from exc


def _storage_error_message(exc: Exception) -> str:
    message = str(exc)
    checks = (
        ("bucket name is required", "Bucket 名称不能为空"),
        ("endpoint is required", "Endpoint 不能为空"),
        ("no such host", "DNS 解析失败，请检查地址是否正确"),
        ("connection refused", "连接被拒绝，请确认服务已启动且端口正确"),
        ("no route to host", "无法路由到目标地址，请检查网络配置"),
        ("timed out", "连接超时，请检查网络或服务状态"),
        ("timeout", "连接超时，请检查网络或服务状态"),
        ("403", "认证失败，请检查访问凭证是否正确"),
        ("AccessDenied", "认证失败，请检查访问凭证是否正确"),
        ("access denied", "认证失败，请检查访问凭证是否正确"),
        ("certificate", "TLS/SSL 证书错误，请检查 SSL 配置"),
        ("tls", "TLS/SSL 证书错误，请检查 SSL 配置"),
        ("404", "Bucket 不存在，请检查名称和 Region"),
        ("NoSuchBucket", "Bucket 不存在，请检查名称和 Region"),
    )
    for needle, safe in checks:
        if needle.lower() in message.lower():
            return safe
    if message.startswith(("endpoint must be", "storage read-back")):
        return message
    return "连接失败，请检查配置参数是否正确"


@router.get("/models/providers")
def model_providers(request: Request) -> dict[str, Any]:
    parse_auth(request)
    return ok(model_runtime.providers_for(request.query_params.get("model_type")))


@router.get("/models/catalog/resolve")
@router.post("/models/catalog/resolve")
async def resolve_catalog(request: Request) -> dict[str, Any]:
    parse_auth(request)
    data = await body(request) if request.method == "POST" else dict(request.query_params)
    return ok(model_runtime.catalog_resolution(data))


@router.get("/models")
def list_models(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT * FROM models WHERE (tenant_id=? OR is_builtin=1) AND deleted_at IS NULL ORDER BY tenant_id=? DESC, is_default DESC, name",
        (ctx.tenant_id, ctx.tenant_id),
    )
    return ok([model_runtime.serialize_model(row) for row in rows])


@router.post("/models", status_code=201)
async def create_model(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("Model name is required")
    model_type = str(data.get("type") or "KnowledgeQA")
    if model_type not in model_runtime.MODEL_TYPES:
        raise validation("Unsupported model type")
    source = str(data.get("source") or "remote")
    parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
    model_id = str(data.get("id") or db.new_id())
    now = db.utcnow()
    with db.transaction() as conn:
        if data.get("is_default"):
            conn.execute(
                "UPDATE models SET is_default=0, updated_at=? WHERE tenant_id=? AND type=? AND deleted_at IS NULL",
                (now, ctx.tenant_id, model_type),
            )
        conn.execute(
            """
            INSERT INTO models(id, tenant_id, name, display_name, type, source,
              description, parameters, is_default, is_builtin, managed_by, status,
              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', 'active', ?, ?)
            """,
            (
                model_id, ctx.tenant_id, name, str(data.get("display_name") or name),
                model_type, source,
                str(data.get("description") or ""), dumps(parameters),
                1 if data.get("is_default") else 0, now, now,
            ),
        )
    return ok(_get_model(ctx, model_id))


def _raw_model(ctx: Any, model_id: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM models WHERE id=? AND (tenant_id=? OR is_builtin=1) AND deleted_at IS NULL",
        (model_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Model not found")
    row["parameters"] = loads(row.get("parameters"), {})
    row["is_default"] = bool(row.get("is_default"))
    row["is_builtin"] = bool(row.get("is_builtin"))
    return row


def _get_model(ctx: Any, model_id: str) -> dict[str, Any]:
    return model_runtime.serialize_model(_raw_model(ctx, model_id))


def _writable_model(ctx: Any, model_id: str) -> dict[str, Any]:
    row = _raw_model(ctx, model_id)
    if int(row.get("tenant_id") or 0) != int(ctx.tenant_id) and not ctx.user.get("is_system_admin"):
        raise forbidden("Only a system administrator can modify a builtin model")
    return row


@router.get("/models/{model_id}")
def get_model(model_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(_get_model(ctx, model_id))


@router.put("/models/{model_id}")
async def update_model(model_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    existing = _writable_model(ctx, model_id)
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    for key in ("name", "display_name", "type", "source", "description", "status"):
        if key in data:
            updates[key] = str(data.get(key) or "")
    if "parameters" in data:
        incoming = data["parameters"] if isinstance(data["parameters"], dict) else {}
        stored = loads(existing.get("parameters"), {})
        if not isinstance(stored, dict):
            stored = {}
        for field in ("api_key", "app_secret"):
            value = incoming.get(field)
            if not value or (isinstance(value, str) and not value.strip("*•·● ")):
                if stored.get(field):
                    incoming[field] = stored[field]
        updates["parameters"] = dumps(incoming)
    if "is_default" in data:
        if data["is_default"]:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE models SET is_default=0, updated_at=? WHERE tenant_id=? AND type=? AND id<>? AND deleted_at IS NULL",
                    (db.utcnow(), ctx.tenant_id, str(data.get("type") or existing.get("type") or ""), model_id),
                )
        updates["is_default"] = 1 if data["is_default"] else 0
    with db.transaction() as conn:
        db.update("models", updates, "id=?", (model_id,), conn)
    return ok(_get_model(ctx, model_id))


@router.delete("/models/{model_id}")
def delete_model(model_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _writable_model(ctx, model_id)
    pattern = f'%"{model_id}"%'
    usage = db.fetch_one(
        """
        SELECT
          (SELECT COUNT(*) FROM knowledge_bases
           WHERE tenant_id=? AND deleted_at IS NULL
             AND (embedding_model_id=? OR summary_model_id=?
                  OR COALESCE(vlm_config,'') LIKE ? OR COALESCE(wiki_config,'') LIKE ?
                  OR COALESCE(auto_tag_config,'') LIKE ? OR COALESCE(profile_config,'') LIKE ?)) AS kb_count,
          (SELECT COUNT(*) FROM custom_agents
           WHERE tenant_id=? AND deleted_at IS NULL AND config LIKE ?) AS agent_count
        """,
        (ctx.tenant_id, model_id, model_id, pattern, pattern, pattern, pattern,
         ctx.tenant_id, pattern),
    )
    if usage and (int(usage["kb_count"] or 0) or int(usage["agent_count"] or 0)):
        raise conflict("Model is in use by an active knowledge base or agent")
    with db.transaction() as conn:
        conn.execute("UPDATE models SET deleted_at=?, updated_at=? WHERE id=?", (db.utcnow(), db.utcnow(), model_id))
    return ok(message="Model deleted")


@router.post("/models/{model_id}/debug")
async def debug_model(model_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    model = _raw_model(ctx, model_id)
    form = await request.form()
    input_text = str(form.get("input") or "")
    if len(input_text.encode("utf-8")) > 64 * 1024:
        raise validation("input is too long")
    options_raw = form.get("options") or "{}"
    try:
        options = json.loads(str(options_raw))
    except ValueError as exc:
        raise validation("invalid options") from exc
    if not isinstance(options, dict):
        options = {}
    documents_raw = form.get("documents") or "[]"
    try:
        documents = json.loads(str(documents_raw)) if str(documents_raw).strip() else []
    except ValueError as exc:
        raise validation("documents must be a JSON string array") from exc
    if not isinstance(documents, list) or any(not isinstance(item, str) for item in documents):
        raise validation("documents must be a JSON string array")
    if len(documents) > 100:
        raise validation("documents cannot exceed 100 items")
    for field, low, high in (("max_tokens", 1, 8192), ("temperature", 0, 2)):
        value = options.get(field)
        if value is not None and (not isinstance(value, (int, float)) or not low <= float(value) <= high):
            raise validation(f"{field} must be between {low} and {high}")
    top_p = options.get("top_p")
    if top_p is not None and (not isinstance(top_p, (int, float)) or not 0 < float(top_p) <= 1):
        raise validation("top_p must be greater than 0 and at most 1")

    uploaded = form.get("file")
    file_data = b""
    filename = ""
    if uploaded is not None and hasattr(uploaded, "read"):
        file_data = await uploaded.read()
        filename = str(getattr(uploaded, "filename", "") or "upload")

    parameters = model.get("parameters") if isinstance(model.get("parameters"), dict) else {}
    preview = {
        "model_id": model["id"],
        "model_name": model["name"],
        "model_type": model["type"],
        "source": model["source"],
        "provider": parameters.get("provider") or "",
        "input": input_text,
        "options": options,
    }
    if documents:
        preview["documents"] = documents
    if filename:
        preview["file"] = {"name": filename, "size": len(file_data)}
    if parameters.get("extra_config"):
        preview["model_extra_config"] = model_runtime.safe_parameters(parameters).get("extra_config")
    if parameters.get("custom_headers"):
        preview["custom_header_names"] = sorted(str(key) for key in parameters["custom_headers"])

    started = time.monotonic()
    observations: dict[str, Any] = {}
    raw_response: Any = None
    error_text = ""
    try:
        runtime = model_runtime.runtime_config(model, str(model["type"]))
        if not model_runtime.available(runtime):
            raise RuntimeError("model base URL is not configured")
        model_type = str(model["type"])
        if model_type == "KnowledgeQA":
            if not input_text.strip() and not file_data:
                raise validation("query cannot be empty")
            messages: list[dict[str, Any]] = []
            if options.get("system_prompt"):
                messages.append({"role": "system", "content": str(options["system_prompt"])})
            if file_data:
                content_type = mimetypes.guess_type(filename)[0] or "image/png"
                data_uri = f"data:{content_type};base64,{base64.b64encode(file_data).decode()}"
                content: Any = [{"type": "image_url", "image_url": {"url": data_uri}}]
                if input_text.strip():
                    content.insert(0, {"type": "text", "text": input_text})
                messages.append({"role": "user", "content": content})
            else:
                messages.append({"role": "user", "content": input_text})
            raw_response = model_runtime.chat_completion(
                runtime,
                messages,
                temperature=options.get("temperature"),
                top_p=options.get("top_p"),
                max_tokens=options.get("max_tokens"),
            )
            content, _message = model_runtime.text_from_chat_response(raw_response)
            observations.update({"stream": False, "answer_characters": len(content), "api": "openai-completions"})
        elif model_type == "Embedding":
            if options.get("similarity"):
                if not input_text.strip() or (not documents and not file_data):
                    raise validation("query and at least one candidate are required")
                if file_data and not model_runtime.embedding_accepts_images(runtime):
                    raise validation("embedding model does not accept images")
                query_vectors = model_runtime.embedding(runtime, [input_text])
                if len(query_vectors) != 1 or not query_vectors[0]:
                    raise RuntimeError("upstream returned an unexpected embedding count")
                query_vector = query_vectors[0]
                observations["dimension"] = len(query_vector)
                scores: list[dict[str, Any]] = []

                def score_candidate(index: int, kind: str, content: str, vector: list[float]) -> None:
                    if len(vector) != len(query_vector):
                        raise RuntimeError(
                            f"{kind} candidate {index} has {len(vector)} dimensions, "
                            f"the query {len(query_vector)}"
                        )
                    item: dict[str, Any] = {
                        "index": index,
                        "kind": kind,
                        "similarity": _cosine(query_vector, vector),
                    }
                    if content:
                        item["content"] = content
                    scores.append(item)

                if documents:
                    vectors = model_runtime.embedding(runtime, documents)
                    if len(vectors) != len(documents):
                        raise RuntimeError("upstream returned an unexpected embedding count")
                    for index, vector in enumerate(vectors):
                        score_candidate(index, "text", documents[index], vector)
                if file_data:
                    vectors = model_runtime.image_embedding(runtime, file_data)
                    if len(vectors) != 1:
                        raise RuntimeError("upstream returned an unexpected embedding count")
                    score_candidate(len(documents), "image", "", vectors[0])
                raw_response = {"results": sorted(scores, key=lambda item: item["similarity"], reverse=True)}
                observations["result_count"] = len(scores)
            else:
                if not input_text.strip() and not file_data:
                    raise validation("input cannot be empty")
                if file_data:
                    if input_text.strip():
                        raise validation("test either text or an image for embedding")
                    vectors = model_runtime.image_embedding(runtime, file_data)
                    raw_response = vectors[0]
                    observations.update({"result_count": len(vectors), "dimension": len(raw_response)})
                else:
                    vectors = model_runtime.embedding(runtime, [input_text])
                    raw_response = vectors[0] if vectors else []
                    observations.update({"result_count": len(vectors), "dimension": len(raw_response)})
        elif model_type == "Rerank":
            if not input_text.strip() or (not documents and not file_data):
                raise validation("query and documents cannot be empty")
            if file_data:
                if documents:
                    raise validation("test either text documents or an image for reranking")
                raw_response = model_runtime.image_rerank(runtime, input_text, file_data)
            else:
                raw_response = model_runtime.rerank(runtime, input_text, documents)
            observations["result_count"] = len(raw_response)
        elif model_type == "VLLM":
            if not file_data:
                raise validation("image file is required")
            content_type = mimetypes.guess_type(filename)[0] or "image/png"
            data_uri = f"data:{content_type};base64,{base64.b64encode(file_data).decode()}"
            messages = [{
                "role": "user",
                "content": [
                    *([{"type": "text", "text": input_text}] if input_text.strip() else []),
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }]
            response = model_runtime.chat_completion(runtime, messages)
            raw_response, _message = model_runtime.text_from_chat_response(response)
            observations["answer_characters"] = len(raw_response)
        elif model_type == "ASR":
            if not file_data:
                raise validation("audio file is required")
            raw_response = model_runtime.transcribe(runtime, file_data, filename)
            observations["text_characters"] = len(str(raw_response.get("text") or ""))
            observations["segment_count"] = len(raw_response.get("segments") or [])
        else:
            raise validation("unsupported model type")
    except Exception as exc:  # noqa: BLE001
        error_text = str(exc)
    result = {
        "ok": not error_text,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "request": preview,
        "raw_response": raw_response,
        "observations": observations,
    }
    if error_text:
        result["error"] = error_text
    return ok(result)


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    length = min(len(left), len(right))
    dot = sum(left[index] * right[index] for index in range(length))
    left_norm = math.sqrt(sum(value * value for value in left[:length])) or 1.0
    right_norm = math.sqrt(sum(value * value for value in right[:length])) or 1.0
    return dot / (left_norm * right_norm)


@router.post("/models/{model_id}/copy", status_code=201)
async def copy_model(model_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    display_name = str(data.get("display_name") or "").strip()
    if not display_name:
        raise validation("display name is required")
    if len(display_name) > 255:
        raise validation("display name is too long")
    source = _writable_model(ctx, model_id)
    if source.get("is_builtin"):
        raise validation("builtin models cannot be copied")
    now = db.utcnow()
    values = {
        "id": db.new_id(),
        "tenant_id": ctx.tenant_id,
        "name": source["name"],
        "display_name": display_name,
        "type": source["type"],
        "source": source["source"],
        "description": source.get("description") or "",
        "parameters": dumps(deepcopy(source.get("parameters") or {})),
        "is_default": 0,
        "is_builtin": 0,
        "managed_by": "",
        "status": "active",
        "created_at": now,
        "updated_at": now,
    }
    with db.transaction() as conn:
        db.insert("models", values, conn)
    return ok(_get_model(ctx, values["id"]))


@router.put("/models/{model_id}/credentials")
async def put_model_credentials(model_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    model = _writable_model(ctx, model_id)
    parameters = model.get("parameters") or {}
    for field in ("api_key", "app_secret"):
        if field in data:
            value = str(data[field] or "")
            if value:
                parameters[field] = value
            else:
                parameters.pop(field, None)
    with db.transaction() as conn:
        conn.execute("UPDATE models SET parameters=?, updated_at=? WHERE id=?", (dumps(parameters), db.utcnow(), model_id))
    return {"fields": {field: {"configured": bool(parameters.get(field))} for field in ("api_key", "app_secret")}}


@router.delete("/models/{model_id}/credentials/{field}")
def delete_model_credential(model_id: str, field: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    if field not in {"api_key", "app_secret"}:
        raise validation("unknown credential field")
    model = _writable_model(ctx, model_id)
    parameters = model.get("parameters") or {}
    parameters.pop(field, None)
    with db.transaction() as conn:
        conn.execute("UPDATE models SET parameters=?, updated_at=? WHERE id=?", (dumps(parameters), db.utcnow(), model_id))
    return {"fields": {name: {"configured": bool(parameters.get(name))} for name in ("api_key", "app_secret")}}


@router.get("/models/weknoracloud/status")
def cloud_status(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    tenant = db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    if not tenant:
        raise not_found("Workspace not found")
    return weknoracloud_service.status(tenant)


@router.post("/weknoracloud/credentials")
async def save_cloud_credentials(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    app_id = str(data.get("app_id") or "").strip()
    app_secret = str(data.get("app_secret") or "")
    if not app_id:
        raise validation("app_id is required")
    if not app_secret:
        raise validation("app_secret is required")
    try:
        weknoracloud_service.verify_credentials(app_id, app_secret)
    except weknoracloud_service.CloudError as exc:
        raise validation(f"credential verification failed: {exc}") from exc
    tenant = db.fetch_one("SELECT * FROM tenants WHERE id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    if not tenant:
        raise not_found("Workspace not found")
    credentials = weknoracloud_service.merge_credentials(tenant, app_id, app_secret)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tenants SET credentials=?, updated_at=? WHERE id=?",
            (credentials, db.utcnow(), ctx.tenant_id),
        )
    return {"success": True, "message": "凭证保存成功"}


@router.get("/storage-backends/types")
def storage_types(request: Request) -> dict[str, Any]:
    parse_auth(request)
    return ok(_allowed_storage_providers())


def _allowed_storage_providers() -> list[str]:
    raw = str(os.getenv("STORAGE_ALLOW_LIST") or os.getenv("WEKNORA_STORAGE_ALLOW_LIST") or "").strip()
    if not raw:
        return list(_STORAGE_PROVIDERS)
    allowed = {
        item.strip().lower()
        for item in raw.replace(";", ",").replace("|", ",").replace("\n", ",").split(",")
        if item.strip()
    }
    return [provider for provider in _STORAGE_PROVIDERS if provider in allowed]


@router.get("/storage-backends")
def list_storage_backends(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all("SELECT * FROM storage_backends WHERE tenant_id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    tenant = db.fetch_one("SELECT default_storage_backend_id FROM tenants WHERE id=?", (ctx.tenant_id,))
    return ok(
        [_storage_response(row) for row in rows],
        default_storage_backend_id=(tenant or {}).get("default_storage_backend_id"),
    )


@router.post("/storage-backends", status_code=201)
async def create_storage_backend(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    backend_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO storage_backends(id, tenant_id, name, provider, config, source, status, legacy_alias, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'user', 'ready', 0, ?, ?)",
            (backend_id, ctx.tenant_id, str(data.get("name") or "Local storage"), str(data.get("provider") or "local"), dumps(data.get("config") or {}), now, now),
        )
    row = db.fetch_one("SELECT * FROM storage_backends WHERE id=?", (backend_id,))
    return ok(_storage_response(row))


@router.get("/storage-backends/{backend_id}")
def get_storage_backend(backend_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one("SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (backend_id, ctx.tenant_id))
    if not row:
        raise not_found("Storage backend not found")
    return ok(_storage_response(row))


@router.put("/storage-backends/{backend_id}")
async def update_storage_backend(backend_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    existing = db.fetch_one(
        "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (backend_id, ctx.tenant_id),
    )
    if not existing:
        raise not_found("Storage backend not found")
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "provider", "status"):
        if key in data:
            updates[key] = str(data[key])
    if isinstance(data.get("config"), dict):
        stored = loads(existing.get("config"), {})
        merged = _merge_secrets(data["config"], stored if isinstance(stored, dict) else {}, _STORAGE_SECRET_FIELDS)
        updates["config"] = dumps(merged)
    with db.transaction() as conn:
        db.update("storage_backends", updates, "id=? AND tenant_id=?", (backend_id, ctx.tenant_id), conn)
    return get_storage_backend(backend_id, request)


@router.delete("/storage-backends/{backend_id}")
def delete_storage_backend(backend_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    row = db.fetch_one(
        "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (backend_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Storage backend not found")
    if row.get("source") == "env":
        raise conflict("Environment storage backends are read-only")
    tenant = db.fetch_one("SELECT default_storage_backend_id FROM tenants WHERE id=?", (ctx.tenant_id,))
    if tenant and tenant.get("default_storage_backend_id") == backend_id:
        raise conflict("The default storage backend cannot be deleted")
    bound = db.fetch_one(
        "SELECT COUNT(*) AS count FROM knowledge_bases WHERE tenant_id=? AND storage_backend_id=? AND deleted_at IS NULL",
        (ctx.tenant_id, backend_id),
    )
    if bound and int(bound.get("count") or 0):
        raise conflict("Storage backend is still used by a knowledge base")
    with db.transaction() as conn:
        conn.execute("UPDATE storage_backends SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), backend_id, ctx.tenant_id))
    return ok(message="Storage backend deleted")


@router.post("/storage-backends/test")
async def test_storage(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    provider = str(data.get("provider") or "").strip().lower()
    if not provider:
        raise validation("provider is required")
    if provider not in _STORAGE_PROVIDERS:
        raise validation("unsupported storage provider")
    config_data = data.get("config") if isinstance(data.get("config"), dict) else {}
    try:
        connectivity.test_storage(provider, config_data)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": _storage_error_message(exc)}
    return {"success": True}


@router.post("/storage-backends/{backend_id}/test")
def test_storage_backend(backend_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    row = db.fetch_one(
        "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (backend_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Storage backend not found")
    config_data = loads(row.get("config"), {})
    try:
        connectivity.test_storage(str(row.get("provider") or "local"), config_data if isinstance(config_data, dict) else {})
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": _storage_error_message(exc)}
    return {"success": True}


@router.put("/storage-backends/{backend_id}/default")
def set_default_storage(backend_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE tenants SET default_storage_backend_id=?, updated_at=? WHERE id=?", (backend_id, db.utcnow(), ctx.tenant_id))
    return ok(message="Default storage backend updated")


@router.get("/vector-stores/types")
def vector_store_types(request: Request) -> dict[str, Any]:
    parse_auth(request)
    return ok(vector_store_catalog.supported_vector_store_types())


@router.get("/vector-stores")
def list_vector_stores(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    env_stores = [
        _vector_store_response(store)
        for store in vector_store_catalog.environment_vector_stores()
    ]
    rows = db.fetch_all("SELECT * FROM vector_stores WHERE tenant_id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    return ok(env_stores + [_vector_store_response(row) for row in rows])


@router.post("/vector-stores", status_code=201)
async def create_vector_store(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("name is required")
    engine_type = str(data.get("engine_type") or "").strip().lower()
    if not vector_store_catalog.is_registerable_engine_type(engine_type):
        raise validation(f"unsupported engine type: {engine_type}")
    connection = data.get("connection_config")
    if connection is not None and not isinstance(connection, dict):
        raise validation("connection_config must be an object")
    connection = dict(connection) if isinstance(connection, dict) else {}
    index_config = data.get("index_config")
    if index_config is not None and not isinstance(index_config, dict):
        raise validation("index_config must be an object")
    index_config = dict(index_config) if isinstance(index_config, dict) else {}
    error = vector_store_catalog.validate_connection_config(engine_type, connection)
    if error:
        raise validation(error)
    _validate_vector_store_ssrf(engine_type, connection)
    error = vector_store_catalog.validate_index_config(engine_type, index_config)
    if error:
        raise validation(error)

    endpoint = vector_store_catalog.vector_store_endpoint(engine_type, connection)
    index_name = vector_store_catalog.vector_store_index_name(engine_type, index_config)
    rows = db.fetch_all(
        "SELECT * FROM vector_stores WHERE tenant_id=? AND deleted_at IS NULL",
        (ctx.tenant_id,),
    )
    for row in rows:
        if str(row.get("engine_type") or "") != engine_type:
            continue
        stored_connection = loads(row.get("connection_config"), {})
        stored_index = loads(row.get("index_config"), {})
        if not isinstance(stored_connection, dict):
            stored_connection = {}
        if not isinstance(stored_index, dict):
            stored_index = {}
        if (
            vector_store_catalog.vector_store_endpoint(
                engine_type, stored_connection
            )
            == endpoint
            and vector_store_catalog.vector_store_index_name(
                engine_type, stored_index
            )
            == index_name
        ):
            raise conflict(
                "a vector store with the same endpoint and index already exists"
            )
    for env_store in vector_store_catalog.environment_vector_stores():
        env_connection = env_store.get("connection_config")
        env_index = env_store.get("index_config")
        if not isinstance(env_connection, dict):
            env_connection = {}
        if not isinstance(env_index, dict):
            env_index = {}
        if (
            str(env_store.get("engine_type") or "") == engine_type
            and vector_store_catalog.vector_store_endpoint(
                engine_type, env_connection
            )
            == endpoint
            and vector_store_catalog.vector_store_index_name(
                engine_type, env_index
            )
            == index_name
        ):
            raise conflict(
                "a vector store with the same endpoint and index is already "
                "configured via environment variables"
            )

    try:
        result = connectivity.test_vector_store(engine_type, connection)
    except Exception as exc:  # noqa: BLE001
        raise validation(
            "connection test failed: "
            f"{exc}. Ensure the server is reachable before saving."
        ) from exc
    version = str(result.get("version") or "")
    if version:
        connection["version"] = version

    store_id = db.new_id()
    now = db.utcnow()
    try:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO vector_stores
                (id, name, engine_type, connection_config, index_config,
                 tenant_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    store_id,
                    name,
                    engine_type,
                    dumps(connection),
                    dumps(index_config),
                    ctx.tenant_id,
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise conflict("a vector store with the same name already exists") from exc
    row = db.fetch_one(
        "SELECT * FROM vector_stores WHERE id=? AND tenant_id=?",
        (store_id, ctx.tenant_id),
    )
    return ok(_vector_store_response(row))


@router.get("/vector-stores/{store_id}")
def get_vector_store(store_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    if vector_store_catalog.is_environment_store_id(store_id):
        store = vector_store_catalog.find_environment_vector_store(store_id)
        if not store:
            raise not_found("Vector store not found")
        return ok(_vector_store_response(store))
    row = db.fetch_one("SELECT * FROM vector_stores WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (store_id, ctx.tenant_id))
    if not row:
        raise not_found("Vector store not found")
    return ok(_vector_store_response(row))


@router.put("/vector-stores/{store_id}")
async def update_vector_store(store_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    if vector_store_catalog.is_environment_store_id(store_id):
        raise validation(_ENV_VECTOR_STORE_READONLY)
    existing = db.fetch_one(
        "SELECT * FROM vector_stores WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (store_id, ctx.tenant_id),
    )
    if not existing:
        raise not_found("Vector store not found")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("name is required")
    try:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE vector_stores SET name=?, updated_at=? "
                "WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
                (name, db.utcnow(), store_id, ctx.tenant_id),
            )
    except sqlite3.IntegrityError as exc:
        raise conflict("a vector store with the same name already exists") from exc
    row = db.fetch_one(
        "SELECT * FROM vector_stores WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (store_id, ctx.tenant_id),
    )
    return ok(_vector_store_response(row))


@router.delete("/vector-stores/{store_id}")
def delete_vector_store(store_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    if vector_store_catalog.is_environment_store_id(store_id):
        raise validation(_ENV_VECTOR_STORE_READONLY)
    row = db.fetch_one(
        "SELECT * FROM vector_stores WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (store_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Vector store not found")
    bound = db.fetch_one(
        "SELECT COUNT(*) AS count FROM knowledge_bases WHERE tenant_id=? AND vector_store_id=? AND deleted_at IS NULL",
        (ctx.tenant_id, store_id),
    )
    count = int(bound.get("count") or 0) if bound else 0
    if count:
        raise validation(
            f"vector store still has {count} knowledge base(s) bound to it; "
            "unbind or delete them before removing the store"
        )
    with db.transaction() as conn:
        conn.execute("UPDATE vector_stores SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), store_id, ctx.tenant_id))
    return ok()


@router.post("/vector-stores/test")
async def test_vector_store(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    engine_type = str(data.get("engine_type") or "").strip().lower()
    connection = data.get("connection_config") if isinstance(data.get("connection_config"), dict) else {}
    if not vector_store_catalog.is_registerable_engine_type(engine_type):
        return {
            "success": False,
            "error": f"connection test is not supported for engine type: {engine_type}",
        }
    error = vector_store_catalog.validate_connection_config(engine_type, connection)
    if error:
        return {"success": False, "error": error}
    try:
        _validate_vector_store_ssrf(engine_type, connection)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}
    try:
        result = connectivity.test_vector_store(engine_type, connection)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": _storage_error_message(exc)}
    return {"success": True, "version": result.get("version") or ""}


@router.post("/vector-stores/{store_id}/test")
def test_vector_store_id(store_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    if vector_store_catalog.is_environment_store_id(store_id):
        store = vector_store_catalog.find_environment_vector_store(store_id)
        if not store:
            raise not_found("Vector store not found")
        connection = store.get("connection_config")
        try:
            result = connectivity.test_vector_store(
                str(store.get("engine_type") or ""),
                connection if isinstance(connection, dict) else {},
            )
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": _storage_error_message(exc)}
        return {"success": True, "version": str(result.get("version") or "")}
    row = db.fetch_one(
        "SELECT * FROM vector_stores WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (store_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Vector store not found")
    connection = loads(row.get("connection_config"), {})
    try:
        result = connectivity.test_vector_store(
            str(row.get("engine_type") or "sqlite"),
            connection if isinstance(connection, dict) else {},
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": _storage_error_message(exc)}
    version = str(result.get("version") or "")
    if version and isinstance(connection, dict) and connection.get("version") != version:
        connection["version"] = version
        with db.transaction() as conn:
            conn.execute(
                "UPDATE vector_stores SET connection_config=?, updated_at=? WHERE id=? AND tenant_id=?",
                (dumps(connection), db.utcnow(), store_id, ctx.tenant_id),
            )
    return {"success": True, "version": version}


def _provider_list(ctx: Any) -> list[dict[str, Any]]:
    rows = db.fetch_all("SELECT * FROM web_search_providers WHERE tenant_id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    return [_web_search_response(row) for row in rows]


def _web_search_response(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    parameters = loads(result.get("parameters"), {})
    if not isinstance(parameters, dict):
        parameters = {}
    hidden = parameters.pop("_credentials", {})
    if not isinstance(hidden, dict):
        hidden = {}
    legacy_key = str(parameters.pop("api_key", "") or "")
    parameters.pop("credentials", None)
    result["parameters"] = parameters
    result["is_default"] = bool(result.get("is_default"))
    result["credentials"] = {"api_key": {"configured": bool(hidden.get("api_key") or legacy_key)}}
    return result


@router.get("/web-search-providers/types")
def web_search_types(request: Request) -> dict[str, Any]:
    parse_auth(request)
    return ok(web_search_catalog.provider_types())


@router.get("/web-search-providers")
def list_web_search_providers(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(_provider_list(ctx))


@router.get("/web-search/providers")
def web_search_providers(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(_provider_list(ctx))


@router.post("/web-search-providers", status_code=201)
async def create_web_search_provider(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    provider_id = db.new_id()
    now = db.utcnow()
    parameters = loads(data.get("parameters"), {})
    if not isinstance(parameters, dict):
        parameters = {}
    api_key = str(data.get("api_key") or parameters.pop("api_key", "") or "")
    parameters.pop("credentials", None)
    parameters.pop("_credentials", None)
    if api_key:
        parameters["_credentials"] = {"api_key": api_key}
    provider = str(data.get("provider") or "duckduckgo")
    try:
        web_search_service.validate_parameters(provider, parameters)
    except web_search_service.WebSearchError as exc:
        raise validation(str(exc)) from exc
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO web_search_providers(id, tenant_id, name, provider, description, parameters, is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (provider_id, ctx.tenant_id, str(data.get("name") or "Web search"), provider, str(data.get("description") or ""), dumps(parameters), 1 if data.get("is_default") else 0, now, now),
        )
    return ok(_web_search_response(db.fetch_one("SELECT * FROM web_search_providers WHERE id=?", (provider_id,))))


@router.get("/web-search-providers/{provider_id}")
def get_web_search_provider(provider_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one("SELECT * FROM web_search_providers WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (provider_id, ctx.tenant_id))
    if not row:
        raise not_found("Web search provider not found")
    return ok(_web_search_response(row))


@router.put("/web-search-providers/{provider_id}")
async def update_web_search_provider(provider_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    row = db.fetch_one(
        "SELECT * FROM web_search_providers WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (provider_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Web search provider not found")
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "provider", "description"):
        if key in data:
            updates[key] = str(data[key])
    if "parameters" in data:
        parameters = loads(data.get("parameters"), {})
        if not isinstance(parameters, dict):
            parameters = {}
        current = loads(row.get("parameters"), {})
        hidden = current.get("_credentials") if isinstance(current, dict) else None
        parameters.pop("api_key", None)
        parameters.pop("credentials", None)
        if isinstance(hidden, dict) and hidden:
            parameters["_credentials"] = hidden
        updates["parameters"] = dumps(parameters)
    if "is_default" in data:
        updates["is_default"] = 1 if data["is_default"] else 0
    effective_provider = str(updates.get("provider") or row.get("provider") or "duckduckgo")
    effective_parameters = parameters if "parameters" in data else loads(row.get("parameters"), {})
    try:
        web_search_service.validate_parameters(effective_provider, effective_parameters)
    except web_search_service.WebSearchError as exc:
        raise validation(str(exc)) from exc
    with db.transaction() as conn:
        db.update("web_search_providers", updates, "id=? AND tenant_id=?", (provider_id, ctx.tenant_id), conn)
    return get_web_search_provider(provider_id, request)


@router.put("/web-search-providers/{provider_id}/credentials")
async def put_web_search_credentials(provider_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    row = db.fetch_one(
        "SELECT * FROM web_search_providers WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (provider_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Web search provider not found")
    data = await body(request)
    nested = data.get("credentials") if isinstance(data.get("credentials"), dict) else {}
    if "api_key" not in data and not nested:
        parameters = loads(row.get("parameters"), {})
        hidden = parameters.get("_credentials") if isinstance(parameters, dict) else {}
        return {"success": True, "data": {"fields": {"api_key": {"configured": bool((hidden or {}).get("api_key"))}}}}
    parameters = loads(row.get("parameters"), {})
    if not isinstance(parameters, dict):
        parameters = {}
    hidden = parameters.get("_credentials") if isinstance(parameters.get("_credentials"), dict) else {}
    value = data.get("api_key", nested.get("api_key"))
    if value is None:
        raise validation("api_key is required")
    hidden["api_key"] = str(value)
    parameters["_credentials"] = hidden
    parameters.pop("api_key", None)
    with db.transaction() as conn:
        conn.execute("UPDATE web_search_providers SET parameters=?, updated_at=? WHERE id=?", (dumps(parameters), db.utcnow(), provider_id))
    return {"success": True, "data": {"fields": {"api_key": {"configured": bool(hidden.get("api_key"))}}}}


@router.delete("/web-search-providers/{provider_id}/credentials/{field}")
def delete_web_search_credential(provider_id: str, field: str, request: Request) -> Response:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    if field != "api_key":
        raise validation("unknown credential field")
    row = db.fetch_one(
        "SELECT * FROM web_search_providers WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (provider_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Web search provider not found")
    parameters = loads(row.get("parameters"), {})
    if not isinstance(parameters, dict):
        parameters = {}
    parameters.pop("api_key", None)
    hidden = parameters.get("_credentials") if isinstance(parameters.get("_credentials"), dict) else {}
    hidden.pop("api_key", None)
    if hidden:
        parameters["_credentials"] = hidden
    else:
        parameters.pop("_credentials", None)
    with db.transaction() as conn:
        conn.execute("UPDATE web_search_providers SET parameters=?, updated_at=? WHERE id=?", (dumps(parameters), db.utcnow(), provider_id))
    return Response(status_code=204)


@router.delete("/web-search-providers/{provider_id}")
def delete_web_search_provider(provider_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE web_search_providers SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), provider_id, ctx.tenant_id))
    return ok(message="Web search provider deleted")


@router.post("/web-search-providers/test")
async def test_web_search_raw(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    provider = str(data.get("provider") or "duckduckgo")
    parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
    if data.get("api_key"):
        parameters = {**parameters, "_credentials": {"api_key": str(data["api_key"])}}
    try:
        results = web_search_service.search(provider, parameters, "test", 1)
    except web_search_service.WebSearchError as exc:
        return {"success": False, "error": str(exc)}
    if not results:
        return {"success": False, "error": f"{provider} returned 0 results"}
    return {"success": True}


@router.post("/web-search-providers/{provider_id}/test")
def test_web_search_saved(provider_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    row = db.fetch_one(
        "SELECT * FROM web_search_providers WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (provider_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Web search provider not found")
    try:
        results = web_search_service.search(str(row.get("provider") or ""), row.get("parameters"), "test", 1)
    except web_search_service.WebSearchError as exc:
        return {"success": False, "error": str(exc)}
    if not results:
        return {"success": False, "error": f"{row.get('provider')} returned 0 results"}
    return {"success": True}


@router.get("/datasource/types")
def datasource_types(request: Request) -> dict[str, Any]:
    parse_auth(request)
    return ok(datasource_connector.connector_types())


def _data_source_parts(raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    config = loads(raw, {})
    if not isinstance(config, dict):
        config = {}
    hidden: dict[str, Any] = {}
    for key in ("_credentials", "credentials"):
        value = config.pop(key, None)
        if isinstance(value, dict):
            hidden.update(value)
    return config, hidden


def _data_source_response(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    config, credentials = _data_source_parts(result.get("config"))
    settings = config.get("settings") if isinstance(config.get("settings"), dict) else {}
    settings = dict(settings)
    if result.get("type") == "rss" and not str(settings.get("feed_urls") or "").strip():
        legacy_feed_urls = credentials.get("feed_urls")
        if legacy_feed_urls:
            settings["feed_urls"] = legacy_feed_urls
    config["settings"] = settings
    config["type"] = result.get("type") or ""
    resource_ids = config.get("resource_ids")
    if isinstance(resource_ids, str):
        resource_ids = [value.strip() for value in resource_ids.splitlines() if value.strip()]
    elif isinstance(resource_ids, list):
        resource_ids = [str(value) for value in resource_ids]
    else:
        resource_ids = []
    config["resource_ids"] = resource_ids
    result["config"] = config
    result["last_sync_result"] = loads(result.get("last_sync_result"), {})
    result["last_sync_cursor"] = loads(result.get("last_sync_cursor"), None)
    configured = bool(credentials.get("auth_headers")) if result.get("type") == "rss" else bool(credentials)
    result["credentials"] = {"credentials": {"configured": configured}}
    latest = db.fetch_one(
        "SELECT * FROM sync_logs WHERE data_source_id=? AND tenant_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (result.get("id"), result.get("tenant_id")),
    )
    if latest:
        latest["result"] = loads(latest.get("result"), {})
        result["latest_sync_log"] = latest
    return result


@router.get("/datasource")
def list_data_sources(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb_id = request.query_params.get("kb_id") or request.query_params.get("knowledge_base_id")
    clauses = ["tenant_id=?", "deleted_at IS NULL"]
    params: list[Any] = [ctx.tenant_id]
    if kb_id:
        clauses.append("knowledge_base_id=?")
        params.append(kb_id)
    rows = db.fetch_all("SELECT * FROM data_sources WHERE " + " AND ".join(clauses) + " ORDER BY created_at", params)
    result = []
    for row in rows:
        item = _data_source_response(row)
        item["last_sync_cursor"] = None
        result.append(item)
    return ok(result)


@router.post("/datasource", status_code=201)
async def create_data_source(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    kb_id = str(data.get("knowledge_base_id") or data.get("kb_id") or "")
    if not kb_id:
        raise validation("knowledge_base_id is required")
    kb = db.fetch_one(
        "SELECT id FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, ctx.tenant_id),
    )
    if not kb:
        raise not_found("Knowledge base not found")
    config, credentials = _data_source_parts(data.get("config"))
    if isinstance(data.get("credentials"), dict):
        credentials.update(data["credentials"])
    source_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO data_sources(id, tenant_id, knowledge_base_id, name, type,
              config, sync_schedule, sync_mode, status, conflict_strategy,
              sync_deletions, sync_log_retention_days, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                ctx.tenant_id,
                kb_id,
                str(data.get("name") or "Data source"),
                str(data.get("type") or "rss"),
                dumps({**config, **({"_credentials": credentials} if credentials else {})}),
                str(data.get("sync_schedule") or ""),
                str(data.get("sync_mode") or "incremental"),
                str(data.get("status") or "active"),
                str(data.get("conflict_strategy") or "overwrite"),
                1 if data.get("sync_deletions") else 0,
                int(data.get("sync_log_retention_days") or 30),
                now,
                now,
            ),
        )
    return ok(_data_source_response(db.fetch_one("SELECT * FROM data_sources WHERE id=?", (source_id,))))


@router.get("/datasource/{source_id}")
def get_data_source(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one("SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (source_id, ctx.tenant_id))
    if not row:
        raise not_found("Data source not found")
    return ok(_data_source_response(row))


@router.put("/datasource/{source_id}")
async def update_data_source(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    row = db.fetch_one(
        "SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Data source not found")
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "type", "sync_schedule", "sync_mode", "status", "conflict_strategy"):
        if key in data:
            updates[key] = str(data[key])
    if "sync_deletions" in data:
        updates["sync_deletions"] = 1 if data.get("sync_deletions") else 0
    if "sync_log_retention_days" in data:
        updates["sync_log_retention_days"] = int(data.get("sync_log_retention_days") or 30)
    if "config" in data:
        config, _ = _data_source_parts(data["config"])
        _, current_credentials = _data_source_parts(row.get("config"))
        credentials = current_credentials
        updates["config"] = dumps({**config, **({"_credentials": credentials} if credentials else {})})
    with db.transaction() as conn:
        db.update("data_sources", updates, "id=? AND tenant_id=?", (source_id, ctx.tenant_id), conn)
    return get_data_source(source_id, request)


@router.put("/datasource/{source_id}/credentials")
async def put_data_source_credentials(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    row = db.fetch_one(
        "SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Data source not found")
    data = await body(request)
    credentials = data.get("credentials")
    if not isinstance(credentials, dict) or not credentials:
        raise validation("credentials map must be non-empty")
    config, _ = _data_source_parts(row.get("config"))
    config["_credentials"] = credentials
    with db.transaction() as conn:
        conn.execute("UPDATE data_sources SET config=?, updated_at=? WHERE id=?", (dumps(config), db.utcnow(), source_id))
    return {"success": True, "data": {"fields": {"credentials": {"configured": True}}}}


@router.delete("/datasource/{source_id}/credentials/credentials")
def delete_data_source_credentials(source_id: str, request: Request) -> Response:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    row = db.fetch_one(
        "SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Data source not found")
    config, _ = _data_source_parts(row.get("config"))
    with db.transaction() as conn:
        conn.execute("UPDATE data_sources SET config=?, updated_at=? WHERE id=?", (dumps(config), db.utcnow(), source_id))
    return Response(status_code=204)


@router.delete("/datasource/{source_id}")
def delete_data_source(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE data_sources SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), source_id, ctx.tenant_id))
    return ok(message="Data source deleted")


@router.post("/datasource/validate-credentials")
async def validate_credentials(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    source_type = str(data.get("type") or "").strip()
    credentials = data.get("credentials")
    if not source_type or not isinstance(credentials, dict):
        raise validation("type and credentials are required")
    config = datasource_connector.prepare_config({"settings": credentials, "credentials": credentials})
    try:
        datasource_connector.validate(source_type, config)
    except Exception as exc:  # noqa: BLE001 - normalized into an API validation error
        raise validation(str(exc)) from exc
    return ok({"status": "connected"})


@router.post("/datasource/{source_id}/validate")
def validate_data_source(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    source = db.fetch_one(
        "SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source_id, ctx.tenant_id),
    )
    if not source:
        raise not_found("Data source not found")
    config = datasource_connector.prepare_config(source.get("config"))
    try:
        datasource_connector.validate(str(source.get("type") or ""), config)
    except Exception as exc:  # noqa: BLE001 - persist connector failures on the source
        now = db.utcnow()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE data_sources SET status='error', error_message=?, updated_at=? WHERE id=?",
                (str(exc), now, source_id),
            )
        raise validation(str(exc)) from exc
    if source.get("status") == "error":
        with db.transaction() as conn:
            conn.execute(
                "UPDATE data_sources SET status='active', error_message='', updated_at=? WHERE id=?",
                (db.utcnow(), source_id),
            )
    return ok({"status": "connected"})


@router.post("/datasource/{source_id}/sync")
def sync_data_source(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    source = db.fetch_one(
        "SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source_id, ctx.tenant_id),
    )
    if not source:
        raise not_found("Data source not found")
    kb = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source.get("knowledge_base_id"), ctx.tenant_id),
    )
    if not kb:
        raise not_found("Knowledge base not found")
    source_type = str(source.get("type") or "")
    if not datasource_connector.supports_sync(source_type):
        raise validation(f"connector {source_type!r} is not available in the Python build")

    from .knowledge import _create_knowledge, _insert_chunks
    from ..services.wiki_ingest import ingest_knowledge

    config = datasource_connector.prepare_config(source.get("config"))
    started = db.utcnow()
    log_id = db.new_id()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sync_logs(id, data_source_id, tenant_id, status, started_at,
              items_total, items_created, items_updated, items_deleted, items_skipped,
              items_failed, result, created_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, 0, 0, 0, 0, 0, 0, '{}', ?, ?)
            """,
            (log_id, source_id, ctx.tenant_id, started, started, started),
        )

    result: dict[str, Any] = {
        "total": 0, "created": 0, "updated": 0, "deleted": 0,
        "skipped": 0, "failed": 0, "errors": [],
    }
    cursor_items: dict[str, str] = {}
    cursor_resources: dict[str, str] = {}
    fetch_failed = False
    error_message = ""
    status = "success"
    try:
        previous_cursor = loads(source.get("last_sync_cursor"), {})
        previous_connector_cursor = previous_cursor.get("connector_cursor") if isinstance(previous_cursor, dict) else None
        if isinstance(previous_connector_cursor, dict):
            if isinstance(previous_connector_cursor.get("items"), dict):
                cursor_items.update({str(key): str(value) for key, value in previous_connector_cursor["items"].items()})
            if isinstance(previous_connector_cursor.get("resources"), dict):
                cursor_resources.update({str(key): str(value) for key, value in previous_connector_cursor["resources"].items()})
        cursor_meta: dict[str, Any] = {}
        items = datasource_connector.fetch_items(
            source_type,
            config,
            previous_cursor,
            force_full=source.get("sync_mode") == "full",
            cursor_meta=cursor_meta,
        )
        existing_rows = db.fetch_all(
            "SELECT * FROM knowledges WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL",
            (ctx.tenant_id, kb["id"]),
        )
        existing: dict[str, dict[str, Any]] = {}
        for row in existing_rows:
            metadata = loads(row.get("metadata"), {})
            if not isinstance(metadata, dict) or metadata.get("datasource_id") != source_id:
                continue
            external_id = str(metadata.get("external_id") or "")
            if external_id and external_id not in existing:
                existing[external_id] = row

        chunking = loads(kb.get("chunking_config"), {})
        chunk_size = int(chunking.get("chunk_size") or chunking.get("max_chunk_size") or 512)
        overlap = int(chunking.get("chunk_overlap") or chunking.get("overlap_size") or 50)
        result["total"] = len(items)

        for item in items:
            external_id = str(item.get("external_id") or "").strip()
            title = str(item.get("title") or external_id or "Untitled").strip()[:255]
            if not external_id:
                result["skipped"] += 1
                continue

            if item.get("is_deleted"):
                if not source.get("sync_deletions"):
                    continue
                row = existing.get(external_id)
                if not row:
                    result["skipped"] += 1
                    continue
                now = db.utcnow()
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE knowledges SET deleted_at=?, parse_status='deleting', updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
                    conn.execute(
                        "UPDATE chunks SET deleted_at=?, updated_at=? WHERE knowledge_id=?",
                        (now, now, row["id"]),
                    )
                    storage_quota.release_knowledge_rows(conn, [row])
                from .knowledge import _delete_knowledge_files

                _delete_knowledge_files([row])
                existing.pop(external_id, None)
                cursor_items.pop(external_id, None)
                cursor_resources.pop(external_id, None)
                result["deleted"] += 1
                continue

            item_error = str(item.get("error") or "").strip()
            if item_error:
                if "fingerprint" in item:
                    cursor_items[external_id] = str(item.get("fingerprint") or "")
                    cursor_resources[external_id] = str(item.get("resource_id") or "")
                result["failed"] += 1
                if len(result["errors"]) < 100:
                    error_detail = {"title": title, "message": item_error}
                    item_metadata = item.get("metadata")
                    if isinstance(item_metadata, dict) and item_metadata.get("error_reason_code"):
                        error_detail["reason_code"] = str(item_metadata["error_reason_code"])
                    result["errors"].append(error_detail)
                continue

            content = str(item.get("content") or "").strip()
            if not content:
                result["skipped"] += 1
                continue
            fingerprint = str(item.get("fingerprint") or "")
            cursor_items[external_id] = fingerprint
            cursor_resources[external_id] = str(item.get("resource_id") or "")
            source_url = str(item.get("url") or external_id)[:2048]
            item_metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            metadata = {
                **item_metadata,
                "datasource_id": source_id,
                "external_id": external_id,
                "source_resource_id": str(item.get("resource_id") or ""),
                "source_url": source_url,
                "fingerprint": fingerprint,
            }
            if item.get("updated_at"):
                metadata["source_updated_at"] = str(item["updated_at"])

            row = existing.get(external_id)
            if row:
                previous_metadata = loads(row.get("metadata"), {})
                if not isinstance(previous_metadata, dict):
                    previous_metadata = {}
                if source.get("sync_mode") != "full" and previous_metadata.get("fingerprint") == fingerprint:
                    result["skipped"] += 1
                    continue
                if source.get("conflict_strategy") == "skip":
                    result["skipped"] += 1
                    continue

                now = db.utcnow()
                filename = str(item.get("file_name") or f"{title[:240]}.md")
                file_type = str(item.get("file_type") or "md").lower().lstrip(".")
                merged_metadata = {**previous_metadata, **metadata}
                try:
                    new_storage_size = storage_quota.knowledge_storage_size(
                        len(content.encode()), content
                    )
                    with db.transaction() as conn:
                        storage_quota.adjust_storage(
                            conn,
                            ctx.tenant_id,
                            int(row.get("storage_size") or 0),
                            new_storage_size,
                        )
                        conn.execute("DELETE FROM chunks WHERE knowledge_id=?", (row["id"],))
                        conn.execute(
                            """
                            UPDATE knowledges SET title=?, source=?, file_name=?, file_type=?,
                              file_size=?, file_hash=?, storage_size=?, metadata=?, parse_status='completed',
                              enable_status='enabled', error_message='', updated_at=?, processed_at=?
                            WHERE id=?
                            """,
                            (
                                title, source_url, filename, file_type, len(content.encode()),
                                fingerprint, new_storage_size,
                                dumps(merged_metadata), now, now, row["id"],
                            ),
                        )
                        _insert_chunks(conn, ctx.tenant_id, kb["id"], row["id"], content, chunk_size, overlap)
                    updated = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (row["id"],))
                    if updated:
                        ingest_knowledge(kb, updated)
                    result["updated"] += 1
                except Exception as exc:  # noqa: BLE001 - one bad item must not abort the sync
                    result["failed"] += 1
                    if len(result["errors"]) < 100:
                        result["errors"].append({"title": title, "message": str(exc)})
                continue

            try:
                filename = str(item.get("file_name") or f"{title[:240]}.md")
                file_type = str(item.get("file_type") or "md").lower().lstrip(".")
                created = _create_knowledge(
                    ctx,
                    kb,
                    title=title,
                    text=content,
                    knowledge_type="file",
                    source=source_url,
                    file_name=filename,
                    file_type=file_type,
                    file_size=len(content.encode()),
                    file_hash=fingerprint,
                    metadata=metadata,
                )
                if not created:
                    raise RuntimeError("knowledge row was not created")
                existing[external_id] = created
                result["created"] += 1
            except Exception as exc:  # noqa: BLE001 - one bad item must not abort the sync
                result["failed"] += 1
                if len(result["errors"]) < 100:
                    result["errors"].append({"title": title, "message": str(exc)})
    except Exception as exc:  # noqa: BLE001 - normalize connector failures into the sync log
        error_message = f"Fetch failed: {exc}"
        status = "failed"
        fetch_failed = True
    else:
        if result["failed"]:
            status = "partial" if result["failed"] < result["total"] else "failed"
            error_message = f"{result['failed']} item(s) failed to sync"

    finished = db.utcnow()
    cursor = {
        "last_sync_time": finished,
        "connector_cursor": {
            **cursor_meta,
            "items": cursor_items,
            "resources": cursor_resources,
        },
    }
    source_status = "paused" if source.get("status") == "paused" else ("error" if status == "failed" else "active")
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE sync_logs SET status=?, finished_at=?, items_total=?, items_created=?,
              items_updated=?, items_deleted=?, items_skipped=?, items_failed=?,
              error_message=?, result=?, updated_at=? WHERE id=?
            """,
            (
                status, finished, result["total"], result["created"], result["updated"],
                result["deleted"], result["skipped"], result["failed"], error_message,
                dumps(result), finished, log_id,
            ),
        )
        if fetch_failed:
            conn.execute(
                "UPDATE data_sources SET status=?, error_message=?, updated_at=? WHERE id=?",
                (source_status, error_message, finished, source_id),
            )
        else:
            conn.execute(
                """
                UPDATE data_sources SET last_sync_at=?, last_sync_cursor=?, last_sync_result=?,
                  status=?, error_message=?, updated_at=? WHERE id=?
                """,
                (finished, dumps(cursor), dumps(result), source_status, error_message, finished, source_id),
            )
    log = db.fetch_one("SELECT * FROM sync_logs WHERE id=? AND tenant_id=?", (log_id, ctx.tenant_id))
    if log:
        log["result"] = loads(log.get("result"), {})
    return ok(log)


@router.post("/datasource/{source_id}/pause")
def pause_data_source(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE data_sources SET status='paused', updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), source_id, ctx.tenant_id))
    return ok({"status": "paused"})


@router.post("/datasource/{source_id}/resume")
def resume_data_source(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE data_sources SET status='active', updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), source_id, ctx.tenant_id))
    return ok({"status": "active"})


@router.get("/datasource/{source_id}/logs")
def data_source_logs(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all("SELECT * FROM sync_logs WHERE data_source_id=? AND tenant_id=? ORDER BY created_at DESC, rowid DESC LIMIT 100", (source_id, ctx.tenant_id))
    for row in rows:
        row["result"] = loads(row.get("result"), {})
    return ok(rows)


@router.get("/datasource/logs/{log_id}")
def data_source_log(log_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one("SELECT * FROM sync_logs WHERE id=? AND tenant_id=?", (log_id, ctx.tenant_id))
    if not row:
        raise not_found("Sync log not found")
    row["result"] = loads(row.get("result"), {})
    return ok(row)


@router.get("/datasource/{source_id}/resources")
def data_source_resources(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    source = db.fetch_one(
        "SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source_id, ctx.tenant_id),
    )
    if not source:
        raise not_found("Data source not found")
    config = datasource_connector.prepare_config(source.get("config"))
    try:
        resources = datasource_connector.list_resources(
            str(source.get("type") or ""),
            config,
            request.query_params.get("parent_id") or "",
        )
    except Exception as exc:  # noqa: BLE001 - connector errors are request validation failures
        raise validation(str(exc)) from exc
    return ok(resources)


@router.post("/datasource/{source_id}/resource-ancestors")
async def data_source_resource_ancestors(source_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    source = db.fetch_one(
        "SELECT * FROM data_sources WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (source_id, ctx.tenant_id),
    )
    if not source:
        raise not_found("Data source not found")
    data = await body(request)
    requested = data.get("resource_ids") if isinstance(data.get("resource_ids"), list) else []
    config = datasource_connector.prepare_config(source.get("config"))
    ancestors = datasource_connector.resource_ancestors(
        str(source.get("type") or ""),
        config,
        [str(value) for value in requested],
    )
    return ok({"ancestors": ancestors})


MCP_ENDPOINT_TOOL_GROUPS = ("retrieve", "chat", "wiki", "ingest")
MCP_ENDPOINT_TOOL_CATALOG = (
    {"name": "list_knowledge_bases", "group": "retrieve", "destructive": False},
    {"name": "search_knowledge", "group": "retrieve", "destructive": False},
    {"name": "grep_chunks", "group": "retrieve", "destructive": False},
    {"name": "list_documents", "group": "retrieve", "destructive": False},
    {"name": "read_document", "group": "retrieve", "destructive": False},
    {"name": "ask", "group": "chat", "destructive": False},
    {"name": "wiki_search", "group": "wiki", "destructive": False},
    {"name": "wiki_read_page", "group": "wiki", "destructive": False},
    {"name": "wiki_index", "group": "wiki", "destructive": False},
    {"name": "add_document", "group": "ingest", "destructive": True},
    {"name": "update_document", "group": "ingest", "destructive": True},
    {"name": "delete_document", "group": "ingest", "destructive": True},
)


def _default_mcp_endpoint_tools() -> list[str]:
    return [item["name"] for item in MCP_ENDPOINT_TOOL_CATALOG if not item["destructive"]]


def _normalize_mcp_endpoint_tools(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    wanted = {str(item).strip() for item in value if str(item).strip()}
    return [item["name"] for item in MCP_ENDPOINT_TOOL_CATALOG if item["name"] in wanted]


def _mcp_endpoint_rate(value: Any) -> int:
    try:
        rate = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise validation("rate_limit_per_minute is out of range") from exc
    if rate == 0:
        return 60
    if rate < 0 or rate > 6000:
        raise validation("rate_limit_per_minute is out of range")
    return rate


def _mcp_endpoint_knowledge_bases(ctx: Any, value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        kb_id = str(raw or "").strip()
        if not kb_id or kb_id in seen:
            continue
        if not readable_knowledge_base(ctx.tenant_id, kb_id, ctx.role):
            raise not_found("knowledge base not found: " + kb_id)
        seen.add(kb_id)
        result.append(kb_id)
    return result


def _mcp_endpoint_agent(tenant_id: int, value: Any) -> str:
    agent_id = str(value or "").strip()
    if not agent_id:
        return ""
    if agent_id in agent_service.USER_BUILTIN_AGENT_IDS:
        return agent_id
    row = db.fetch_one(
        "SELECT id FROM custom_agents WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (agent_id, tenant_id),
    )
    if not row:
        raise not_found("agent not found")
    return agent_id


def _mcp_endpoint_capabilities(tools: list[str]) -> list[str]:
    wanted = set(tools)
    groups = {item["group"] for item in MCP_ENDPOINT_TOOL_CATALOG if item["name"] in wanted}
    capabilities: list[str] = []
    if groups.intersection({"retrieve", "wiki", "chat"}):
        capabilities.append(RETRIEVE)
    if "chat" in groups:
        capabilities.extend([CHAT, READ_AGENTS])
    if "ingest" in groups:
        capabilities.append(INGEST)
    return capabilities


def _require_mcp_endpoint_key_scope(ctx: Any, knowledge_base_ids: list[str], tools: list[str]) -> None:
    scope = ctx.api_key
    if scope is None or scope.full_access:
        return
    if scope.is_kb_restricted:
        if not knowledge_base_ids:
            raise forbidden("this API key may only create endpoints limited to its knowledge bases")
        for kb_id in knowledge_base_ids:
            if not scope.allows_kb(kb_id):
                raise forbidden("knowledge base is outside this API key's scope: " + kb_id)
    for capability in _mcp_endpoint_capabilities(tools):
        if not scope.has(capability):
            raise forbidden(
                "this API key lacks the " + capability + " capability the endpoint's tools need"
            )


@router.get("/mcp-endpoints/tools")
def mcp_endpoint_tools(request: Request) -> dict[str, Any]:
    parse_auth(request)
    return ok({
        "groups": list(MCP_ENDPOINT_TOOL_GROUPS),
        "tools": [dict(item) for item in MCP_ENDPOINT_TOOL_CATALOG],
        "default_tools": _default_mcp_endpoint_tools(),
    })


def _mcp_endpoint(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["knowledge_base_ids"] = loads(result.get("knowledge_base_ids"), [])
    result["tools"] = loads(result.get("tools"), [])
    result["enabled"] = bool(result.get("enabled"))
    result["rate_limit_per_minute"] = int(result.get("rate_limit_per_minute") or 0)
    result["path"] = f"/mcp/{result['id']}"
    result.pop("token_hash", None)
    result.pop("deleted_at", None)
    if result.get("last_used_at") is None:
        result.pop("last_used_at", None)
    return result


@router.get("/mcp-endpoints")
def list_mcp_endpoints(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all("SELECT * FROM mcp_endpoints WHERE tenant_id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    return ok([_mcp_endpoint(row) for row in rows])


@router.post("/mcp-endpoints", status_code=201)
async def create_mcp_endpoint(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("name is required")
    if len(name) > 255:
        raise validation("name is too long")
    tools = (
        _normalize_mcp_endpoint_tools(data.get("tools"))
        if "tools" in data
        else _default_mcp_endpoint_tools()
    )
    if not tools:
        raise validation("at least one tool must be enabled")
    knowledge_base_ids = _mcp_endpoint_knowledge_bases(ctx, data.get("knowledge_base_ids"))
    agent_id = _mcp_endpoint_agent(ctx.tenant_id, data.get("default_agent_id"))
    rate = _mcp_endpoint_rate(data.get("rate_limit_per_minute"))
    enabled = 1 if bool(data.get("enabled", True)) else 0
    _require_mcp_endpoint_key_scope(ctx, knowledge_base_ids, tools)
    endpoint_id = db.new_id()
    token = "mcp_" + secrets.token_urlsafe(28)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO mcp_endpoints(id, tenant_id, name, description, enabled,
              token_hash, token_hint, knowledge_base_ids, tools, default_agent_id,
              rate_limit_per_minute, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (endpoint_id, ctx.tenant_id, name, str(data.get("description") or "").strip(), enabled, hashlib_token(token), token[-4:], dumps(knowledge_base_ids), dumps(tools), agent_id, rate, now, now),
        )
    result = _mcp_endpoint(db.fetch_one("SELECT * FROM mcp_endpoints WHERE id=?", (endpoint_id,)))
    result["token"] = token
    return ok(result)


def hashlib_token(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


@router.get("/mcp-endpoints/{endpoint_id}")
def get_mcp_endpoint(endpoint_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one("SELECT * FROM mcp_endpoints WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (endpoint_id, ctx.tenant_id))
    if not row:
        raise not_found("MCP endpoint not found")
    return ok(_mcp_endpoint(row))


@router.put("/mcp-endpoints/{endpoint_id}")
async def update_mcp_endpoint(endpoint_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    current = db.fetch_one(
        "SELECT * FROM mcp_endpoints WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (endpoint_id, ctx.tenant_id),
    )
    if not current:
        raise not_found("MCP endpoint not found")
    updates = {"updated_at": db.utcnow()}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise validation("name is required")
        if len(name) > 255:
            raise validation("name is too long")
        updates["name"] = name
    if "description" in data:
        updates["description"] = str(data.get("description") or "").strip()
    if "enabled" in data:
        updates["enabled"] = 1 if data.get("enabled") else 0
    if "rate_limit_per_minute" in data:
        updates["rate_limit_per_minute"] = _mcp_endpoint_rate(data.get("rate_limit_per_minute"))
    if "knowledge_base_ids" in data:
        updates["knowledge_base_ids"] = dumps(_mcp_endpoint_knowledge_bases(ctx, data.get("knowledge_base_ids")))
    if "default_agent_id" in data:
        updates["default_agent_id"] = _mcp_endpoint_agent(ctx.tenant_id, data.get("default_agent_id"))
    if "tools" in data:
        tools = _normalize_mcp_endpoint_tools(data.get("tools"))
        if not tools:
            raise validation("at least one tool must be enabled")
        updates["tools"] = dumps(tools)
    effective_kbs = loads(
        updates.get("knowledge_base_ids", current.get("knowledge_base_ids")), []
    )
    effective_tools = loads(updates.get("tools", current.get("tools")), [])
    _require_mcp_endpoint_key_scope(ctx, effective_kbs, effective_tools)
    with db.transaction() as conn:
        db.update("mcp_endpoints", updates, "id=? AND tenant_id=?", (endpoint_id, ctx.tenant_id), conn)
    return get_mcp_endpoint(endpoint_id, request)


@router.delete("/mcp-endpoints/{endpoint_id}")
def delete_mcp_endpoint(endpoint_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE mcp_endpoints SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), endpoint_id, ctx.tenant_id))
    return ok(message="MCP endpoint deleted")


@router.post("/mcp-endpoints/{endpoint_id}/rotate-token")
def rotate_mcp_token(endpoint_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    current = db.fetch_one(
        "SELECT * FROM mcp_endpoints WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (endpoint_id, ctx.tenant_id),
    )
    if not current:
        raise not_found("MCP endpoint not found")
    _require_mcp_endpoint_key_scope(
        ctx, loads(current.get("knowledge_base_ids"), []), loads(current.get("tools"), [])
    )
    token = "mcp_" + secrets.token_urlsafe(28)
    with db.transaction() as conn:
        conn.execute("UPDATE mcp_endpoints SET token_hash=?, token_hint=?, updated_at=? WHERE id=? AND tenant_id=?", (hashlib_token(token), token[-4:], db.utcnow(), endpoint_id, ctx.tenant_id))
    result = get_mcp_endpoint(endpoint_id, request)
    result["data"]["token"] = token
    return result


def _normalize_mcp_service(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("headers", "auth_config", "advanced_config", "stdio_config", "env_vars"):
        row[key] = loads(row.get(key), {})
    row["enabled"] = bool(row.get("enabled"))
    return row


@router.get("/mcp-services")
def list_mcp_services(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all("SELECT * FROM mcp_services WHERE tenant_id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    for row in rows:
        _normalize_mcp_service(row)
        metadata = db.fetch_one(
            "SELECT tools, synced_at FROM mcp_metadata WHERE tenant_id=? AND service_id=? AND principal=''",
            (ctx.tenant_id, row["id"]),
        ) or {}
        tools = loads(metadata.get("tools"), [])
        row["tool_count"] = len(tools) if isinstance(tools, list) else 0
        row["tools_count"] = row["tool_count"]
        row["metadata_synced_at"] = metadata.get("synced_at")
    return ok(rows)


@router.post("/mcp-services", status_code=201)
async def create_mcp_service(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    service_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO mcp_services(id, tenant_id, name, description, enabled,
              transport_type, url, headers, auth_config, advanced_config,
              stdio_config, env_vars, is_builtin, created_at, updated_at, usage_instructions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (service_id, ctx.tenant_id, str(data.get("name") or "MCP Service"), str(data.get("description") or ""), 1 if data.get("enabled", True) else 0, str(data.get("transport_type") or "streamable-http"), str(data.get("url") or ""), dumps(data.get("headers") or {}), dumps(data.get("auth_config") or {}), dumps(data.get("advanced_config") or {}), dumps(data.get("stdio_config") or {}), dumps(data.get("env_vars") or {}), now, now, str(data.get("usage_instructions") or "")),
        )
    return get_mcp_service(service_id, request)


@router.get("/mcp-services/{service_id}")
def get_mcp_service(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one("SELECT * FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (service_id, ctx.tenant_id))
    if not row:
        raise not_found("MCP service not found")
    return ok(_normalize_mcp_service(row))


@router.put("/mcp-services/{service_id}")
async def update_mcp_service(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "description", "transport_type", "url", "usage_instructions"):
        if key in data:
            updates[key] = str(data[key])
    for key in ("headers", "auth_config", "advanced_config", "stdio_config", "env_vars"):
        if key in data:
            updates[key] = dumps(data[key])
    if "enabled" in data:
        updates["enabled"] = 1 if data["enabled"] else 0
    with db.transaction() as conn:
        db.update("mcp_services", updates, "id=? AND tenant_id=?", (service_id, ctx.tenant_id), conn)
    return get_mcp_service(service_id, request)


@router.delete("/mcp-services/{service_id}")
def delete_mcp_service(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE mcp_services SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), service_id, ctx.tenant_id))
    return ok(message="MCP service deleted")


def _mcp_client_for(service: dict[str, Any], ctx: Any) -> mcp_client.MCPClient:
    return mcp_client.MCPClient(service, tenant_id=ctx.tenant_id, user_id=ctx.user_id)


def _mcp_failure(exc: mcp_client.MCPError, prefix: str) -> Any:
    from ..errors import AppError

    status = 401 if exc.oauth_required else 500
    return AppError(f"{prefix}: {exc}", status=status, code=1001 if status == 401 else 1500)


def _mcp_tools_with_policies(service_id: str, tenant_id: int, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    policies = {
        row["tool_name"]: row
        for row in db.fetch_all(
            "SELECT * FROM mcp_tool_approvals WHERE tenant_id=? AND service_id=?",
            (tenant_id, service_id),
        )
    }
    result: list[dict[str, Any]] = []
    for tool in tools:
        item = dict(tool)
        policy = policies.get(str(item.get("name") or "")) or {}
        item["enabled"] = bool(policy.get("enabled", 1))
        item["require_approval"] = bool(policy.get("require_approval", 0))
        result.append(item)
    return result


@router.get("/mcp-services/{service_id}/tools")
def mcp_tools(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    service = _mcp_service(service_id, ctx)
    client = _mcp_client_for(service, ctx)
    try:
        client.connect()
        client.initialize()
        tools = client.list_tools()
    except mcp_client.MCPError as exc:
        raise _mcp_failure(exc, "Failed to list MCP tools") from exc
    finally:
        client.close()
    return ok(_mcp_tools_with_policies(service_id, ctx.tenant_id, tools))


@router.post("/mcp-services/{service_id}/connect")
def mcp_connect(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    service = _mcp_service(service_id, ctx)
    client = _mcp_client_for(service, ctx)
    try:
        client.connect()
        client.initialize()
        tools = client.list_tools()
    except mcp_client.MCPError as exc:
        raise _mcp_failure(exc, "Failed to connect to MCP service") from exc
    finally:
        client.close()
    return ok({
        "connected": True,
        "server_info": client.server_info,
        "tools": _mcp_tools_with_policies(service_id, ctx.tenant_id, tools),
    })


@router.put("/mcp-services/{service_id}/tools/{tool_name}")
async def mcp_tool_toggle(service_id: str, tool_name: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _mcp_service(service_id, ctx)
    data = await body(request)
    if "enabled" not in data and "require_approval" not in data:
        data = {**data, "enabled": True}
    current = db.fetch_one(
        "SELECT * FROM mcp_tool_approvals WHERE tenant_id=? AND service_id=? AND tool_name=?",
        (ctx.tenant_id, service_id, tool_name),
    ) or {}
    enabled = 1 if data.get("enabled", current.get("enabled", 1)) else 0
    require = 1 if data.get("require_approval", current.get("require_approval", 0)) else 0
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_tool_approvals(id, tenant_id, service_id, tool_name, require_approval, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, service_id, tool_name) DO UPDATE SET require_approval=excluded.require_approval, enabled=excluded.enabled, updated_at=excluded.updated_at",
            (db.new_id(), ctx.tenant_id, service_id, tool_name, require, enabled, db.utcnow(), db.utcnow()),
        )
    return ok({
        "service_id": service_id,
        "tool_name": tool_name,
        "require_approval": bool(require),
        "enabled": bool(enabled),
    })


@router.get("/mcp-services/{service_id}/tool-approvals")
def list_mcp_tool_approvals(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _mcp_service(service_id, ctx)
    rows = db.fetch_all(
        "SELECT * FROM mcp_tool_approvals WHERE tenant_id=? AND service_id=? ORDER BY tool_name",
        (ctx.tenant_id, service_id),
    )
    for row in rows:
        row["require_approval"] = bool(row.get("require_approval"))
        row["enabled"] = bool(row.get("enabled", 1))
    return ok(rows)


@router.put("/mcp-services/{service_id}/tool-approvals/{tool_name}")
async def mcp_tool_approval(service_id: str, tool_name: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    _mcp_service(service_id, ctx)
    data = await body(request)
    if "require_approval" not in data and "enabled" not in data:
        raise validation("require_approval or enabled is required")
    current = db.fetch_one(
        "SELECT * FROM mcp_tool_approvals WHERE tenant_id=? AND service_id=? AND tool_name=?",
        (ctx.tenant_id, service_id, tool_name),
    ) or {}
    require = 1 if data.get("require_approval", current.get("require_approval", 0)) else 0
    enabled = 1 if data.get("enabled", current.get("enabled", 1)) else 0
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_tool_approvals(id, tenant_id, service_id, tool_name, require_approval, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, service_id, tool_name) DO UPDATE SET require_approval=excluded.require_approval, enabled=excluded.enabled, updated_at=excluded.updated_at",
            (db.new_id(), ctx.tenant_id, service_id, tool_name, require, enabled, db.utcnow(), db.utcnow()),
        )
    return ok({"service_id": service_id, "tool_name": tool_name, "require_approval": bool(require), "enabled": bool(enabled)})


@router.post("/mcp-services/{service_id}/usage-instructions/generate")
async def generate_mcp_usage_instructions(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    service = _mcp_service(service_id, ctx)
    data = await body(request)
    language = str(data.get("language") or "zh-CN")
    metadata = db.fetch_one(
        "SELECT * FROM mcp_metadata WHERE tenant_id=? AND service_id=? AND principal=''",
        (ctx.tenant_id, service_id),
    )
    tools = loads(metadata.get("tools"), []) if metadata else []
    disabled = {
        row["tool_name"]
        for row in db.fetch_all(
            "SELECT tool_name FROM mcp_tool_approvals WHERE tenant_id=? AND service_id=? AND enabled=0",
            (ctx.tenant_id, service_id),
        )
    }
    names = [
        str(tool.get("name") or "")
        for tool in tools
        if isinstance(tool, dict) and tool.get("name") and tool.get("name") not in disabled
    ][:8]
    name = str(service.get("name") or "MCP")
    return ok({"usage_instructions": _mcp_usage_instructions(language, name, names)[:500]})


def _mcp_usage_instructions(language: str, name: str, names: list[str]) -> str:
    if language.startswith("en"):
        text = f"Use {name} for tasks that match its connected tools."
        if names:
            text += " Available tools: " + ", ".join(names) + "."
        text += " Prefer these tools over guessing, and summarize the returned evidence clearly."
    elif language.startswith("ja"):
        text = f"{name} は接続されたツールが必要なタスクに使用してください。"
        if names:
            text += "利用可能なツール: " + "、".join(names) + "。"
        text += "推測よりツールの結果を優先し、根拠を明確に示してください。"
    elif language.startswith("ko"):
        text = f"{name}은(는) 연결된 도구가 필요한 작업에 사용하세요."
        if names:
            text += " 사용 가능한 도구: " + ", ".join(names) + "."
        text += " 추측보다 도구 결과를 우선하고 근거를 명확히 제시하세요."
    elif language in {"zh-TW", "zh-HK", "zh-MO", "zh-Hant"} or language.startswith("zh-Hant"):
        text = f"當使用者的問題適合使用 {name} 時，優先呼叫其已連接的工具。"
        if names:
            text += "可用工具包括：" + "、".join(names) + "。"
        text += "請根據工具返回的結果作答，並清楚說明依據；不要臆造工具未返回的資訊。"
    else:
        text = f"当用户的问题适合使用 {name} 时，优先调用其已连接的工具。"
        if names:
            text += "可用工具包括：" + "、".join(names) + "。"
        text += "请基于工具返回的结果作答，并清楚说明依据；不要臆造工具未返回的信息。"
    return text


def _mcp_service(service_id: str, ctx: Any) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (service_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("MCP service not found")
    return row


@router.post("/mcp-services/{service_id}/test")
def test_mcp_service(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    service = _mcp_service(service_id, ctx)
    client = _mcp_client_for(service, ctx)
    try:
        client.connect()
        initialized = client.initialize()
        capabilities = client.capabilities
        try:
            tools = client.list_tools()
        except mcp_client.MCPError:
            if capabilities.get("tools") is not None:
                raise
            tools = []
        try:
            resources = client.list_resources()
        except mcp_client.MCPError:
            if capabilities.get("resources") is not None:
                raise
            resources = []
    except mcp_client.MCPError as exc:
        return ok({
            "success": False,
            "message": "Test failed: " + str(exc),
            "oauth_required": exc.oauth_required,
            "tools": [],
            "resources": [],
        })
    finally:
        client.close()
    info = initialized.get("serverInfo") if isinstance(initialized, dict) else {}
    return ok({
        "success": True,
        "message": "Connection successful",
        "description": str((info or {}).get("description") or ""),
        "tools": _mcp_tools_with_policies(service_id, ctx.tenant_id, tools),
        "resources": resources,
    })


@router.get("/mcp-services/{service_id}/metadata")
def get_mcp_metadata(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    service = _mcp_service(service_id, ctx)
    row = db.fetch_one(
        "SELECT * FROM mcp_metadata WHERE tenant_id=? AND service_id=? AND principal=''",
        (ctx.tenant_id, service_id),
    )
    if not row:
        return ok(None)
    row["tools"] = loads(row.get("tools"), [])
    row["tool_count"] = len(row["tools"]) if isinstance(row["tools"], list) else 0
    row["stale"] = str(row.get("config_fingerprint") or "") != mcp_client.configuration_fingerprint(service)
    return ok(row)


@router.post("/mcp-services/{service_id}/metadata/refresh")
def refresh_mcp_metadata(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    service = _mcp_service(service_id, ctx)
    fingerprint = mcp_client.configuration_fingerprint(service)
    client = _mcp_client_for(service, ctx)
    try:
        client.connect()
        client.initialize()
        payload = mcp_client.metadata_payload(client)
    except mcp_client.MCPError as exc:
        raise _mcp_failure(exc, "Failed to refresh MCP metadata") from exc
    finally:
        client.close()
    current = _mcp_service(service_id, ctx)
    if mcp_client.configuration_fingerprint(current) != fingerprint:
        from ..errors import AppError

        raise AppError("MCP connection changed during refresh", status=409, code=1005)
    now = db.utcnow()
    metadata = {
        "service_id": service_id, **payload, "synced_at": now, "stale": False,
    }
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_metadata(tenant_id, service_id, principal, config_fingerprint, tools, instructions, server_name, server_version, server_description, synced_at) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, service_id, principal) DO UPDATE SET tools=excluded.tools, instructions=excluded.instructions, server_name=excluded.server_name, server_version=excluded.server_version, server_description=excluded.server_description, synced_at=excluded.synced_at",
            (
                ctx.tenant_id, service_id, fingerprint, dumps(metadata["tools"]), metadata["instructions"],
                metadata["server_name"], metadata["server_version"], metadata["server_description"], now,
            ),
        )
    return ok(metadata)


@router.get("/mcp-services/{service_id}/resources")
def mcp_resources(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    service = _mcp_service(service_id, ctx)
    client = _mcp_client_for(service, ctx)
    try:
        client.connect()
        client.initialize()
        resources = client.list_resources()
    except mcp_client.MCPError as exc:
        raise _mcp_failure(exc, "Failed to list MCP resources") from exc
    finally:
        client.close()
    return ok(resources)


@router.get("/mcp-services/{service_id}/oauth/status")
def mcp_oauth_status(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _mcp_service(service_id, ctx)
    attempt_id = str(request.query_params.get("authorization_attempt") or "").strip()
    if attempt_id:
        authorized = mcp_oauth.attempt_complete(ctx.tenant_id, ctx.user_id, service_id, attempt_id)
        return ok({"authorized": authorized, "state": "authorized" if authorized else "pending"})
    row = mcp_oauth.get_token(ctx.tenant_id, ctx.user_id, service_id)
    return ok(mcp_oauth.token_status(row))


@router.post("/mcp-services/{service_id}/oauth/authorize-url")
async def mcp_oauth_authorize_url(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    service = _mcp_service(service_id, ctx)
    data = await body(request)
    redirect_uri = str(data.get("redirect_uri") or "").strip()
    if not redirect_uri:
        raise validation("redirect_uri is required")
    try:
        authorization_url, attempt = mcp_oauth.start_authorization(
            service,
            ctx.tenant_id,
            ctx.user_id,
            redirect_uri,
            str(data.get("frontend_redirect") or "/"),
        )
    except mcp_oauth.OAuthError as exc:
        raise validation(str(exc)) from exc
    return ok({
        "authorization_url": authorization_url,
        "authorization_attempt": attempt,
        "service": service["name"],
    })


@router.delete("/mcp-services/{service_id}/oauth/token")
def revoke_mcp_oauth_token(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _mcp_service(service_id, ctx)
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM mcp_oauth_tokens WHERE tenant_id=? AND principal_type='web_user' "
            "AND principal_id=? AND service_id=?",
            (ctx.tenant_id, ctx.user_id, service_id),
        )
    return ok(message="OAuth token revoked")


@router.put("/mcp-services/{service_id}/credentials")
async def update_mcp_credentials(service_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    service = _mcp_service(service_id, ctx)
    data = await body(request)
    auth = loads(service.get("auth_config"), {})
    auth.update({key: value for key, value in data.items() if value not in (None, "")})
    with db.transaction() as conn:
        conn.execute("UPDATE mcp_services SET auth_config=?, updated_at=? WHERE id=?", (dumps(auth), db.utcnow(), service_id))
    return ok({"configured": {key: bool(value) for key, value in auth.items()}})


@router.delete("/mcp-services/{service_id}/credentials/{field}")
def delete_mcp_credential(service_id: str, field: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    service = _mcp_service(service_id, ctx)
    auth = loads(service.get("auth_config"), {})
    auth.pop(field, None)
    with db.transaction() as conn:
        conn.execute("UPDATE mcp_services SET auth_config=?, updated_at=? WHERE id=?", (dumps(auth), db.utcnow(), service_id))
    return ok(message="Credential removed")


def _mcp_oauth_callback_response(code: str, state: str, provider_error: str) -> RedirectResponse:
    fallback = "/"
    if provider_error:
        return RedirectResponse(fallback + "#mcp_oauth_error=" + provider_error, status_code=302)
    if not state or not code:
        return RedirectResponse(fallback + "#mcp_oauth_error=missing_code_or_state", status_code=302)
    try:
        result = mcp_oauth.complete_authorization(state, code)
    except mcp_oauth.OAuthError:
        return RedirectResponse(fallback + "#mcp_oauth_error=authorization_failed", status_code=302)
    target = str(result.get("frontend_redirect") or fallback)
    return RedirectResponse(target + "#mcp_oauth_result=success", status_code=302)


@router.get("/mcp-services/oauth/callback")
def mcp_service_oauth_callback(request: Request) -> RedirectResponse:
    return _mcp_oauth_callback_response(
        str(request.query_params.get("code") or ""),
        str(request.query_params.get("state") or ""),
        str(request.query_params.get("error") or ""),
    )


@router.get("/mcp-oauth/callback")
def mcp_oauth_callback(request: Request) -> RedirectResponse:
    return mcp_service_oauth_callback(request)


@router.get("/sandbox-configs")
def list_sandbox_configs(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all("SELECT * FROM tenant_sandbox_configs WHERE tenant_id=? AND deleted_at IS NULL", (ctx.tenant_id,))
    for row in rows:
        row["config"] = loads(row.get("config"), {})
    return {"success": True, "data": rows, "workspace_scripts_disabled": _workspace_scripts_disabled(ctx.tenant_id)}


def _workspace_scripts_disabled(tenant_id: int) -> bool:
    row = db.fetch_one("SELECT scripts_disabled FROM py_workspace_policies WHERE tenant_id=?", (tenant_id,))
    return bool(row and row.get("scripts_disabled"))


@router.get("/sandbox-configs/workspace-policy")
def sandbox_workspace_policy_early(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    disabled = _workspace_scripts_disabled(ctx.tenant_id)
    return {"success": True, "data": {"scripts_disabled": disabled}, "workspace_scripts_disabled": disabled}


@router.put("/sandbox-configs/workspace-policy")
async def set_sandbox_workspace_policy_early(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    disabled = bool(data.get("scripts_disabled"))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_workspace_policies(tenant_id, scripts_disabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET scripts_disabled=excluded.scripts_disabled, updated_at=excluded.updated_at",
            (ctx.tenant_id, 1 if disabled else 0, db.utcnow()),
        )
    return {"success": True, "data": {"scripts_disabled": disabled}, "workspace_scripts_disabled": disabled}


@router.post("/sandbox-configs/templates/query")
async def sandbox_templates_early(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    incoming = data.get("config") if isinstance(data.get("config"), dict) else {}
    stored: dict[str, Any] = {}
    stored_type = ""
    config_id = str(data.get("config_id") or "").strip()
    if config_id:
        row = db.fetch_one(
            "SELECT * FROM tenant_sandbox_configs WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (config_id, ctx.tenant_id),
        )
        if not row:
            raise not_found("Sandbox config not found")
        stored = loads(row.get("config"), {})
        stored_type = str(row.get("sandbox_type") or "")
    effective_config = _merge_sandbox_config(incoming, stored)
    sandbox_type = str(
        data.get("sandbox_type")
        or effective_config.get("sandbox_type")
        or stored_type
        or "docker"
    )
    if sandbox_type not in {"docker", "cube", "e2b"}:
        raise validation("sandbox template catalog only supports cube, e2b and docker backends")
    ensure_standard = bool(data.get("ensure_standard"))
    replace_standard = bool(data.get("replace_standard"))
    ensure_desktop = bool(data.get("ensure_desktop"))
    replace_desktop = bool(data.get("replace_desktop"))
    if (replace_standard or replace_desktop) and not config_id:
        raise validation("config_id is required to rebuild a sandbox template")
    templates: list[dict[str, Any]] = []
    provisioned = False
    if sandbox_type == "docker":
        if replace_desktop:
            raise validation("Docker backend does not expose desktop templates")
        backend = effective_config.get("docker")
        if not isinstance(backend, dict):
            backend = {}
        try:
            client = docker_templates.DockerTemplateClient(backend)
            templates = client.list_templates()
            standard = remote_sandbox.pick_template(templates, "standard")
            desktop = None
            if replace_standard or (ensure_standard and standard is None):
                ensured = client.ensure_template("standard", replace=replace_standard)
                templates = _merge_sandbox_template(templates, ensured)
                status = str(ensured.get("status") or "").lower()
                if status not in remote_sandbox.FAILED_TEMPLATE_STATUSES:
                    provisioned = True
                    ensured_id = str(ensured.get("id") or "").strip()
                    if ensured_id and (standard is None or (replace_standard and status == "ready")):
                        standard = ensured
        except ValueError as exc:
            raise validation(str(exc)) from exc
    else:
        backend = effective_config.get(sandbox_type)
        if not isinstance(backend, dict):
            backend = {}
        try:
            client = remote_sandbox.RemoteSandboxClient(
                sandbox_type, backend, effective_config
            )
            templates = client.list_templates()
            standard = remote_sandbox.pick_template(templates, "standard")
            desktop = remote_sandbox.pick_template(templates, "desktop")
            for kind, ensure, replace in (
                ("standard", ensure_standard, replace_standard),
                ("desktop", ensure_desktop, replace_desktop),
            ):
                if not (replace or (ensure and (standard if kind == "standard" else desktop) is None)):
                    continue
                ensured = client.ensure_template(kind, replace=replace)
                templates = _merge_sandbox_template(templates, ensured)
                status = str(ensured.get("status") or "").lower()
                if status in remote_sandbox.FAILED_TEMPLATE_STATUSES:
                    continue
                provisioned = True
                ensured_id = str(ensured.get("id") or "").strip()
                if kind == "standard":
                    if ensured_id and (standard is None or (replace and status == "ready")):
                        standard = ensured
                elif ensured_id and (desktop is None or (replace and status == "ready")):
                    desktop = ensured
                if replace and status == "ready" and ensured_id:
                    _persist_sandbox_template_id(ctx.tenant_id, config_id, sandbox_type, ensured_id)
                    client.delete_superseded_templates(kind, ensured_id)
        except remote_sandbox.RemoteSandboxError as exc:
            raise validation(exc.reason()) from exc
        except ValueError as exc:
            raise validation(str(exc)) from exc
    return ok({
        "templates": templates,
        "standard_template_id": str(standard["id"]) if standard else "",
        "desktop_template_id": str(desktop["id"]) if desktop else "",
        "provisioned": provisioned,
    })


@router.post("/sandbox-configs", status_code=201)
async def create_sandbox_config(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    config_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tenant_sandbox_configs(id, tenant_id, name, description, sandbox_type, config, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (config_id, ctx.tenant_id, str(data.get("name") or "Local sandbox"), str(data.get("description") or ""), str(data.get("sandbox_type") or "docker"), dumps(data.get("config") or {}), now, now),
        )
    return ok(db.fetch_one("SELECT * FROM tenant_sandbox_configs WHERE id=?", (config_id,)))


@router.get("/sandbox-configs/{config_id}")
def get_sandbox_config(config_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    row = db.fetch_one("SELECT * FROM tenant_sandbox_configs WHERE id=? AND tenant_id=? AND deleted_at IS NULL", (config_id, ctx.tenant_id))
    if not row:
        raise not_found("Sandbox config not found")
    row["config"] = loads(row.get("config"), {})
    return ok(row)


@router.put("/sandbox-configs/{config_id}")
async def update_sandbox_config(config_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    updates = {"updated_at": db.utcnow()}
    for key in ("name", "description", "sandbox_type"):
        if key in data:
            updates[key] = str(data[key])
    if "config" in data:
        updates["config"] = dumps(data["config"])
    with db.transaction() as conn:
        db.update("tenant_sandbox_configs", updates, "id=? AND tenant_id=?", (config_id, ctx.tenant_id), conn)
    return get_sandbox_config(config_id, request)


@router.delete("/sandbox-configs/{config_id}")
def delete_sandbox_config(config_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_sandbox_configs SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?", (db.utcnow(), db.utcnow(), config_id, ctx.tenant_id))
    return ok(message="Sandbox config deleted")


@router.get("/sandbox-configs/{config_id}/sandboxes")
def sandbox_inventory(config_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    sessions = db.fetch_all(
        "SELECT id FROM sessions WHERE tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (ctx.tenant_id, config_id),
    )
    agents = db.fetch_all(
        "SELECT name, config FROM custom_agents WHERE tenant_id=? AND deleted_at IS NULL",
        (ctx.tenant_id,),
    )
    agent_names = []
    for agent in agents:
        config = loads(agent.get("config"), {})
        if isinstance(config, dict) and config.get("sandbox_config_id") == config_id:
            agent_names.append(str(agent.get("name") or ""))
    return ok({
        "sandbox_count": len(sessions),
        "session_ids": [str(row["id"]) for row in sessions],
        "agent_names": [name for name in agent_names if name],
    })


def _sandbox_config(config_id: str, request: Request) -> tuple[Any, dict[str, Any]]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    row = db.fetch_one(
        "SELECT * FROM tenant_sandbox_configs WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (config_id, ctx.tenant_id),
    )
    if not row:
        raise not_found("Sandbox config not found")
    row["config"] = loads(row.get("config"), {})
    return ctx, row


def _skill_json(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["enabled"] = bool(result.get("enabled"))
    result["envs"] = loads(result.get("envs"), [])
    result["served"] = loads(result.get("served"), None)
    return result


def _skill_markdown_metadata(content: str, fallback_name: str) -> tuple[str, str, str, str]:
    """Return name, version, description and the instruction body."""
    text = content.lstrip("\ufeff")
    if not text.lstrip().startswith("---"):
        return fallback_name, "1.0.0", "", text
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "---"), -1)
    if start < 0:
        return fallback_name, "1.0.0", "", text
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].strip() == "---"),
        -1,
    )
    if end < 0:
        return fallback_name, "1.0.0", "", text
    metadata: dict[str, str] = {}
    for line in lines[start + 1:end]:
        key, separator, value = line.partition(":")
        if not separator:
            continue
        metadata[key.strip().lower()] = value.strip().strip("'\"")
    name = metadata.get("name") or metadata.get("slug") or fallback_name
    version = metadata.get("version") or "1.0.0"
    description = metadata.get("description") or ""
    return name, version, description, "\n".join(lines[end + 1:]).strip()


def _skill_install_prompt(name: str, config_id: str, digest: str, instructions: str) -> str:
    state = instructions.strip()
    if len(state) > 4000:
        state = state[:4000] + "\n…"
    return (
        f'Install the skill "{name}" into sandbox configuration {config_id}.\n\n'
        f"Bundle SHA256: {digest}\n\n"
        "SKILL.md:\n" + (state or "(no instructions)")
    )


def _skill_install_summary(name: str, root: Path, digest: str, guidance: str = "") -> str:
    files = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )
    preview = "\n".join(f"- `{path}`" for path in files[:40])
    if len(files) > 40:
        preview += f"\n- … {len(files) - 40} more files"
    result = (
        f'Skill **{name}** is installed and ready.\n\n'
        f"- Files: {len(files)}\n"
        f"- Bundle SHA256: `{digest}`"
    )
    if preview:
        result += "\n\n" + preview
    if guidance.strip():
        result += "\n\nOperator guidance applied:\n\n" + guidance.strip()
    return result


def _create_skill_install_run(
    conn: sqlite3.Connection,
    *,
    tenant_id: int,
    user_id: str,
    config_id: str,
    skill_id: str,
    name: str,
    prompt: str,
    summary: str,
    now: str,
) -> tuple[str, str]:
    session_id = db.new_id()
    run_id = db.new_id()
    conn.execute(
        "INSERT INTO sessions(id, tenant_id, title, user_id, agent_config, sandbox_config_id, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            tenant_id,
            f"Skill install: {name}",
            user_id,
            dumps({"skill_install": True, "sandbox_config_id": config_id, "skill_id": skill_id}),
            config_id,
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT INTO messages(id, request_id, session_id, role, content, is_completed, created_at, updated_at) "
        "VALUES (?, ?, ?, 'user', ?, 1, ?, ?)",
        (db.new_id(), run_id, session_id, prompt, now, now),
    )
    conn.execute(
        "INSERT INTO messages(id, request_id, session_id, role, content, is_completed, created_at, updated_at) "
        "VALUES (?, ?, ?, 'assistant', ?, 1, ?, ?)",
        (run_id, run_id, session_id, summary, now, now),
    )
    return session_id, run_id


_SKILL_BUNDLE_LIMIT = 50 * 1024 * 1024


def _skill_root(tenant_id: int, skill_id: str) -> Path:
    return (config.FILES_DIR / "skills" / str(tenant_id) / skill_id).resolve()


def _safe_archive_member(name: str) -> str:
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if normalized.startswith("/") or any(part == ".." for part in parts):
        raise validation("skill bundle contains an unsafe path")
    return "/".join(parts)


def _extract_skill_bundle(data: bytes, filename: str, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 5000:
                raise validation("skill bundle contains too many files")
            for member in archive.infolist():
                relative = _safe_archive_member(member.filename)
                if not relative or member.is_dir():
                    continue
                mode = (member.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise validation("skill bundle contains a symbolic link")
                target = (destination / relative).resolve()
                if destination not in target.parents:
                    raise validation("skill bundle escapes its destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        return
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise validation("skill bundle contains a symbolic link")
                relative = _safe_archive_member(member.name)
                if not relative or member.isdir():
                    continue
                target = (destination / relative).resolve()
                if destination not in target.parents:
                    raise validation("skill bundle escapes its destination")
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is not None:
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        return
    except tarfile.TarError:
        pass
    name = Path(filename or "SKILL.md").name or "SKILL.md"
    if not name.lower().endswith((".md", ".markdown", ".txt")):
        raise validation("skill must be a zip/tar archive or SKILL.md")
    (destination / name).write_bytes(data)


_SKILL_VERSION = re.compile(r"^v?\d+\.\d+(\.\d+)?([.-][0-9A-Za-z.-]+)?$")


def _split_skill_version(spec: str) -> tuple[str, str]:
    index = spec.rfind("@")
    if index <= 0:
        return spec, ""
    version = spec[index + 1:]
    if version.lower() == "latest" or _SKILL_VERSION.match(version):
        return spec[:index], version
    return spec, ""


def _url_parts(path: str) -> list[str]:
    return [urllib.parse.unquote(part) for part in path.strip("/").split("/") if part not in {"", "."}]


def _clawhub_skill_url(owner: str, slug: str, version: str = "") -> str:
    query: dict[str, str] = {"slug": slug}
    if owner:
        query["ownerHandle"] = owner
    if version and version.lower() != "latest":
        query["version" if _SKILL_VERSION.match(version) else "tag"] = version
    return "https://clawhub.ai/api/v1/download?" + urllib.parse.urlencode(query)


def _resolve_skill_source_url(source: str) -> str:
    return _resolve_skill_source_target(source)[0]


def _resolve_skill_source_target(source: str) -> tuple[str, str]:
    raw = source.strip().rstrip("/")
    if not raw:
        raise validation("source is required")
    if "://" not in raw:
        lower = raw.lower()
        if lower.startswith("skills-sh:"):
            parts = _url_parts(raw.split(":", 1)[1])
            if len(parts) != 3:
                raise validation("skills.sh locator must be owner/repo/slug")
            owner, repo, slug = (part.lower() for part in parts)
            query = urllib.parse.urlencode({"reference": f"skills-sh:{owner}/{repo}/{slug}"})
            return f"https://clawhub.ai/api/v1/skills/{urllib.parse.quote(slug)}/install?{query}", ""
        if "/" in raw and not raw.startswith("@"):
            raise validation(f'"{raw}" is ambiguous; use @owner/slug or a full source URL')
        spec = raw[1:] if raw.startswith("@") else raw
        slug, version = _split_skill_version(spec)
        owner = ""
        if "/" in slug:
            owner, slug = slug.split("/", 1)
        if not slug:
            raise validation("skill slug is required")
        return _clawhub_skill_url(owner, slug, version), ""

    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise validation("skill source must be an http(s) URL")
    host = parsed.hostname.lower() if parsed.hostname else ""
    path = parsed.path.rstrip("/")
    parts = _url_parts(path)
    query = urllib.parse.parse_qs(parsed.query)
    version = str((query.get("version") or [""])[0] or parsed.fragment or "").strip()

    if host in {"github.com", "www.github.com"}:
        if len(parts) < 2:
            raise validation("github URL must be owner/repo")
        owner, repo = parts[0], parts[1].removesuffix(".git")
        rest = parts[2:]
        if rest and rest[0] in {"archive", "releases"}:
            return parsed.geturl(), ""
        ref = "HEAD"
        subdir = ""
        if rest and rest[0] in {"tree", "blob"}:
            if len(rest) < 2:
                raise validation("github tree URL is missing a ref")
            ref = rest[1]
            subdir = "/".join(rest[2:])
            if rest[0] == "blob" and subdir.rsplit("/", 1)[-1].lower() == "skill.md":
                subdir = subdir.rsplit("/", 1)[0] if "/" in subdir else ""
        elif rest:
            subdir = "/".join(rest)
        return "https://codeload.github.com/{}/{}/zip/{}".format(
            urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo, safe=""),
            urllib.parse.quote(ref, safe="/"),
        ), subdir
    if host == "codeload.github.com":
        return parsed.geturl(), ""
    if host in {"gitlab.com", "www.gitlab.com"}:
        project, _, extra = path.strip("/").partition("/-/")
        project_parts = _url_parts(project)
        if len(project_parts) < 2:
            raise validation("gitlab URL must be group/project")
        repo = project_parts[-1].removesuffix(".git")
        owner = "/".join(project_parts[:-1])
        rest = _url_parts(extra)
        if rest and rest[0] == "archive":
            return parsed.geturl(), ""
        ref = rest[1] if len(rest) >= 2 and rest[0] in {"tree", "blob"} else "HEAD"
        subdir = "/".join(rest[2:]) if len(rest) >= 2 and rest[0] in {"tree", "blob"} else ""
        if len(rest) >= 2 and rest[0] == "blob" and subdir.rsplit("/", 1)[-1].lower() == "skill.md":
            subdir = subdir.rsplit("/", 1)[0] if "/" in subdir else ""
        return "https://gitlab.com/{}/{}/-/archive/{}/{}.zip".format(
            urllib.parse.quote(owner, safe="/"), urllib.parse.quote(repo, safe=""),
            urllib.parse.quote(ref, safe="/"), urllib.parse.quote(repo + "-" + ref, safe=""),
        ), subdir
    if host in {"skills.sh", "www.skills.sh"}:
        if len(parts) >= 3:
            owner, repo, slug = (part.lower() for part in parts[:3])
            query_text = urllib.parse.urlencode({"reference": f"skills-sh:{owner}/{repo}/{slug}"})
            return f"https://clawhub.ai/api/v1/skills/{urllib.parse.quote(slug)}/install?{query_text}", ""
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1].removesuffix(".git")
            return f"https://codeload.github.com/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}/zip/HEAD", ""
        raise validation("skills.sh URL must be owner/repo")
    if host in {"clawhub.ai", "www.clawhub.ai", "clawhub.com", "www.clawhub.com"}:
        if path.startswith("/api/v1/download"):
            return parsed.geturl(), ""
        if parts and parts[0] == "skills-sh" and len(parts) >= 4:
            owner, repo, slug = (part.lower() for part in parts[1:4])
            query_text = urllib.parse.urlencode({"reference": f"skills-sh:{owner}/{repo}/{slug}"})
            return f"https://clawhub.ai/api/v1/skills/{urllib.parse.quote(slug)}/install?{query_text}", ""
        if parts and parts[0] == "skills":
            parts = parts[1:]
        if not parts:
            raise validation("skill slug is required")
        owner = parts[0] if len(parts) >= 2 else ""
        slug = parts[1] if len(parts) >= 2 else parts[0]
        slug, inline_version = _split_skill_version(slug)
        return _clawhub_skill_url(owner, slug, inline_version or version), ""
    if host in {"skillhub.cn", "www.skillhub.cn", "api.skillhub.cn"}:
        if path.startswith("/api/v1/download"):
            return "https://api.skillhub.cn/api/v1/download" + (("?" + parsed.query) if parsed.query else ""), ""
        if parts and parts[0] == "skills":
            parts = parts[1:]
        if not parts or len(parts) > 2:
            raise validation("unrecognized SkillHub path")
        slug, inline_version = _split_skill_version(parts[-1])
        query_text = urllib.parse.urlencode({"slug": slug, **({"version": inline_version or version} if inline_version or version else {})})
        return "https://api.skillhub.cn/api/v1/download?" + query_text, ""
    return parsed.geturl(), ""


def _normalize_skill_source_root(root: Path, subdir: str) -> None:
    relative = str(subdir or "").strip().strip("/")
    if not relative:
        return
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_dir():
        raise validation("skill source directory is missing from the downloaded archive")
    staging = root.parent / (".subdir-" + db.new_id())
    try:
        shutil.copytree(candidate, staging)
        shutil.rmtree(root)
        staging.rename(root)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _fetch_skill_source(target: str) -> tuple[bytes, str, str]:
    parsed = urllib.parse.urlparse(target)
    try:
        data, headers, _ = url_guard.fetch_public_url(
            target,
            headers={
                "User-Agent": "PyWeKnora-SkillInstaller",
                "Accept": "application/zip, application/octet-stream, application/json, text/plain;q=0.9, */*;q=0.8",
            },
            timeout=30,
            max_bytes=_SKILL_BUNDLE_LIMIT,
        )
        content_disposition = headers.get("content-disposition", "")
        content_type = headers.get("content-type", "")
    except url_guard.URLGuardError as exc:
        raise validation(f"failed to download skill source: {exc}") from exc
    filename = Path(parsed.path).name or "SKILL.md"
    match = re.search(r"filename=\"?([^\";]+)", content_disposition, re.IGNORECASE)
    if match:
        filename = Path(match.group(1)).name
    return data, filename, content_type


def _download_skill_source(source: str) -> tuple[bytes, str, str]:
    target, subdir = _resolve_skill_source_target(source)
    for _ in range(4):
        data, filename, content_type = _fetch_skill_source(target)
        looks_json = "json" in content_type.lower() or data.lstrip().startswith(b"{")
        if not looks_json:
            return data, filename, subdir
        try:
            handoff = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise validation("skill registry returned invalid JSON") from exc
        if not isinstance(handoff, dict):
            raise validation("skill registry returned an unexpected response")
        if handoff.get("ok") is False:
            message = str(handoff.get("message") or handoff.get("reason") or "skill registry refused the source")
            raise validation(message)
        archive = handoff.get("archive") if isinstance(handoff.get("archive"), dict) else {}
        next_target = str(
            handoff.get("downloadUrl") or handoff.get("archiveUrl") or archive.get("downloadUrl") or ""
        ).strip()
        github = handoff.get("github") if isinstance(handoff.get("github"), dict) else {}
        handoff_path = str(handoff.get("path") or github.get("path") or "").strip().strip("/")
        if next_target:
            target = urllib.parse.urljoin(target, next_target)
        elif github or str(handoff.get("installKind") or "").lower() == "github":
            repo = str(github.get("repo") or handoff.get("repo") or "").strip().removesuffix(".git")
            commit = str(github.get("commit") or handoff.get("commit") or "HEAD").strip()
            if "/" not in repo:
                raise validation("skill registry github handoff is missing repo")
            owner, repo_name = repo.split("/", 1)
            target = "https://codeload.github.com/{}/{}/zip/{}".format(
                urllib.parse.quote(owner, safe=""), urllib.parse.quote(repo_name, safe=""),
                urllib.parse.quote(commit, safe="/"),
            )
        else:
            raise validation("skill registry response has no archive URL")
        if handoff_path:
            subdir = handoff_path
    raise validation("skill source redirected too many times")


def _skill_catalog_root(tenant_id: int, catalog_id: str) -> Path:
    return (config.FILES_DIR / "skills" / "catalog" / str(tenant_id) / catalog_id).resolve()


def _skill_file_payload(root: Path, relative: str) -> dict[str, Any]:
    clean = str(relative or "").strip().lstrip("/")
    if not clean:
        raise validation("path is required")
    candidate = (root / clean).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise not_found("Skill file not found")
    raw = candidate.read_bytes()
    truncated = len(raw) > 2 * 1024 * 1024
    if truncated:
        raw = raw[: 2 * 1024 * 1024]
    media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    payload: dict[str, Any] = {
        "path": candidate.relative_to(root).as_posix(),
        "size": candidate.stat().st_size,
        "media_type": media_type,
        "truncated": truncated,
    }
    try:
        payload.update({"encoding": "utf-8", "content": raw.decode("utf-8"), "binary": False})
    except UnicodeDecodeError:
        if media_type.startswith("image/"):
            payload.update({"encoding": "base64", "content": base64.b64encode(raw).decode(), "binary": True})
        else:
            payload.update({"encoding": "binary", "binary": True})
    return payload


def _register_skill_catalog_data(
    ctx: Any,
    bundle_data: bytes,
    filename: str,
    subdir: str = "",
) -> dict[str, Any]:
    if not bundle_data:
        raise validation("skill bundle is empty")
    if len(bundle_data) > _SKILL_BUNDLE_LIMIT:
        raise validation("skill bundle exceeds the upload limit")
    parent = (config.FILES_DIR / "skills" / "catalog" / str(ctx.tenant_id)).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / (".staging-" + db.new_id())
    try:
        _extract_skill_bundle(bundle_data, filename, staging)
        _normalize_skill_source_root(staging, subdir)
        skill_file = staging / "SKILL.md"
        if not skill_file.exists():
            candidates = list(staging.rglob("SKILL.md"))
            skill_file = candidates[0] if candidates else skill_file
        if not skill_file.exists():
            raise validation("SKILL.md is missing")
        markdown = skill_file.read_text(encoding="utf-8", errors="replace")
        fallback = Path(filename or "skill.zip").stem or "skill"
        name, version, description, instructions = _skill_markdown_metadata(markdown, fallback)
        existing = db.fetch_one(
            "SELECT * FROM tenant_skill_catalog WHERE tenant_id=? AND name=? AND deleted_at IS NULL",
            (ctx.tenant_id, name),
        )
        catalog_id = str(existing["id"]) if existing else db.new_id()
        root = _skill_catalog_root(ctx.tenant_id, catalog_id)
        if root.exists():
            shutil.rmtree(root)
        parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(root))
        now = db.utcnow()
        digest = hashlib.sha256(bundle_data).hexdigest()
        with db.transaction() as conn:
            if existing:
                conn.execute(
                    "UPDATE tenant_skill_catalog SET version=?, description=?, instructions=?, "
                    "bundle_ref=?, bundle_sha256=?, updated_at=? WHERE id=? AND tenant_id=?",
                    (version, description, instructions[:20000], str(root), digest, now, catalog_id, ctx.tenant_id),
                )
            else:
                conn.execute(
                    "INSERT INTO tenant_skill_catalog(id, tenant_id, name, version, description, "
                    "instructions, bundle_ref, bundle_sha256, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (catalog_id, ctx.tenant_id, name, version, description, instructions[:20000], str(root), digest, now, now),
                )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"id": catalog_id, "name": name, "version": version, "description": description}


def _skill_catalog_row(ctx: Any, catalog_id: str) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM tenant_skill_catalog WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (catalog_id, ctx.tenant_id),
    )
    if row:
        return row
    install = db.fetch_one(
        "SELECT * FROM tenant_skills WHERE (id=? OR catalog_id=?) AND tenant_id=? AND deleted_at IS NULL ORDER BY created_at LIMIT 1",
        (catalog_id, catalog_id, ctx.tenant_id),
    )
    if not install:
        raise not_found("Skill not found")
    linked = str(install.get("catalog_id") or "")
    if linked:
        row = db.fetch_one(
            "SELECT * FROM tenant_skill_catalog WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (linked, ctx.tenant_id),
        )
        if row:
            return row
    return {
        "id": str(install["id"]),
        "tenant_id": install["tenant_id"],
        "name": install["name"],
        "version": install.get("version") or "",
        "description": install.get("description") or "",
        "instructions": install.get("instructions") or "",
        "bundle_ref": install.get("bundle_ref") or "",
        "bundle_sha256": install.get("bundle_sha256") or "",
        "created_at": install.get("created_at"),
        "updated_at": install.get("updated_at"),
    }


@router.get("/skills/catalog")
def list_skill_catalog(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    catalogs = db.fetch_all(
        "SELECT * FROM tenant_skill_catalog WHERE tenant_id=? AND deleted_at IS NULL ORDER BY name",
        (ctx.tenant_id,),
    )
    installs = db.fetch_all(
        "SELECT * FROM tenant_skills WHERE tenant_id=? AND deleted_at IS NULL ORDER BY name",
        (ctx.tenant_id,),
    )
    configs = {
        str(row["id"]): row
        for row in db.fetch_all(
            "SELECT id, name, sandbox_type FROM tenant_sandbox_configs WHERE tenant_id=? AND deleted_at IS NULL",
            (ctx.tenant_id,),
        )
    }
    by_catalog: dict[str, list[dict[str, Any]]] = {}
    unattached: list[dict[str, Any]] = []
    for row in installs:
        catalog_id = str(row.get("catalog_id") or "")
        if catalog_id:
            by_catalog.setdefault(catalog_id, []).append(row)
        else:
            unattached.append(row)

    def install_json(row: dict[str, Any]) -> dict[str, Any]:
        config = configs.get(str(row.get("sandbox_config_id") or ""), {})
        payload = {
            "skill_id": str(row["id"]),
            "sandbox_config_id": str(row.get("sandbox_config_id") or ""),
            "sandbox_config_name": str(config.get("name") or ""),
            "sandbox_type": str(config.get("sandbox_type") or ""),
            "status": str(row.get("status") or ""),
            "enabled": bool(row.get("enabled")),
            "version": str(row.get("version") or ""),
            "bundle_sha256": str(row.get("bundle_sha256") or ""),
            "updated_at": row.get("updated_at"),
        }
        if row.get("error"):
            payload["error"] = str(row["error"])
        return payload

    result = []
    seen_catalog: set[str] = set()
    for catalog in catalogs:
        catalog_id = str(catalog["id"])
        seen_catalog.add(catalog_id)
        result.append({
            "id": catalog_id,
            "name": str(catalog.get("name") or ""),
            "version": str(catalog.get("version") or ""),
            "description": str(catalog.get("description") or ""),
            "bundle_sha256": str(catalog.get("bundle_sha256") or ""),
            "created_at": catalog.get("created_at"),
            "updated_at": catalog.get("updated_at"),
            "installations": [install_json(row) for row in by_catalog.get(catalog_id, [])],
        })
    for catalog_id, rows in by_catalog.items():
        if catalog_id not in seen_catalog:
            unattached.extend(rows)
    for row in unattached:
        name = str(row.get("name") or "")
        matching = next((item for item in result if item["name"] == name), None)
        if matching is not None:
            matching["installations"].append(install_json(row))
            continue
        result.append({
            "id": str(row["id"]),
            "name": name,
            "version": str(row.get("version") or ""),
            "description": str(row.get("description") or ""),
            "bundle_sha256": str(row.get("bundle_sha256") or ""),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "installations": [install_json(row)],
        })
    return ok(result)


@router.post("/skills/catalog", status_code=201)
async def register_skill_catalog(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    subdir = ""
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            raise validation("file is required")
        filename = str(getattr(upload, "filename", "") or "skill.zip")
        bundle_data = await upload.read(_SKILL_BUNDLE_LIMIT + 1)
    else:
        data = await body(request)
        source = str(data.get("source") or "").strip()
        if not source:
            raise validation("file or source is required")
        bundle_data, filename, subdir = _download_skill_source(source)
    return ok(_register_skill_catalog_data(ctx, bundle_data, filename, subdir))


@router.post("/skills/catalog/{catalog_id}/install", status_code=202)
async def install_skill_catalog(catalog_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    config_ids = list(dict.fromkeys(str(value) for value in data.get("sandbox_config_ids") or [] if value))
    if not config_ids:
        raise validation("at least one sandbox is required")
    catalog = _skill_catalog_row(ctx, catalog_id)
    catalog_root = Path(str(catalog.get("bundle_ref") or "")).resolve()
    if not str(catalog.get("bundle_ref") or "") or not catalog_root.is_dir():
        raise not_found("Skill bundle not found")
    name = str(catalog.get("name") or "skill")
    version = str(catalog.get("version") or "")
    description = str(catalog.get("description") or "")
    instructions = str(catalog.get("instructions") or "")
    digest = str(catalog.get("bundle_sha256") or "")
    installs: dict[str, str] = {}
    errors: dict[str, str] = {}
    for config_id in config_ids:
        config = db.fetch_one(
            "SELECT id FROM tenant_sandbox_configs WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (config_id, ctx.tenant_id),
        )
        if not config:
            errors[config_id] = "Sandbox config not found"
            continue
        existing = db.fetch_one(
            "SELECT * FROM tenant_skills WHERE tenant_id=? AND sandbox_config_id=? AND name=? AND deleted_at IS NULL",
            (ctx.tenant_id, config_id, name),
        )
        skill_id = str(existing["id"]) if existing else db.new_id()
        root = _skill_root(ctx.tenant_id, skill_id)
        if root.exists():
            shutil.rmtree(root)
        shutil.copytree(catalog_root, root)
        now = db.utcnow()
        prompt = _skill_install_prompt(name, config_id, digest, instructions)
        summary = _skill_install_summary(name, root, digest)
        with db.transaction() as conn:
            session_id, run_id = _create_skill_install_run(
                conn,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                config_id=config_id,
                skill_id=skill_id,
                name=name,
                prompt=prompt,
                summary=summary,
                now=now,
            )
            if existing:
                conn.execute(
                    "UPDATE tenant_skills SET catalog_id=?, version=?, description=?, instructions=?, "
                    "bundle_ref=?, bundle_sha256=?, status='ready', error='', install_session_id=?, "
                    "install_message_id=?, installing_since=NULL, updated_at=? WHERE id=? AND tenant_id=?",
                    (str(catalog["id"]), version, description, instructions, str(root), digest,
                     session_id, run_id, now, skill_id, ctx.tenant_id),
                )
            else:
                conn.execute(
                    "INSERT INTO tenant_skills(id, tenant_id, sandbox_config_id, catalog_id, name, version, "
                    "description, instructions, bundle_ref, bundle_sha256, enabled, status, envs, "
                    "install_session_id, install_message_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'ready', '[]', ?, ?, ?, ?)",
                    (skill_id, ctx.tenant_id, config_id, str(catalog["id"]), name, version, description,
                     instructions, str(root), digest, session_id, run_id, now, now),
                )
        installs[config_id] = skill_id
    if not installs:
        raise not_found(next(iter(errors.values()), "Sandbox config not found"))
    payload: dict[str, Any] = {"installs": installs}
    if errors:
        payload["errors"] = errors
    return {"success": not errors, "data": payload}


@router.delete("/skills/catalog/{catalog_id}")
def delete_skill_catalog(catalog_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    catalog = db.fetch_one(
        "SELECT * FROM tenant_skill_catalog WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (catalog_id, ctx.tenant_id),
    )
    if not catalog:
        raise not_found("Skill not found")
    install = db.fetch_one(
        "SELECT id FROM tenant_skills WHERE catalog_id=? AND tenant_id=? AND deleted_at IS NULL LIMIT 1",
        (catalog_id, ctx.tenant_id),
    )
    if install:
        raise conflict("remove this skill from every sandbox before deleting it from the catalog")
    root = Path(str(catalog.get("bundle_ref") or "")).resolve()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tenant_skill_catalog SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=?",
            (db.utcnow(), db.utcnow(), catalog_id, ctx.tenant_id),
        )
    if str(catalog.get("bundle_ref") or "") and root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    return ok(message="Skill deleted")


@router.get("/skills/catalog/{catalog_id}/files")
def list_skill_catalog_files(catalog_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    catalog = _skill_catalog_row(ctx, catalog_id)
    raw_root = str(catalog.get("bundle_ref") or "")
    root = Path(raw_root).resolve()
    if not raw_root or not root.is_dir():
        return ok([])
    return ok([
        {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size}
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ])


@router.get("/skills/catalog/{catalog_id}/files/content")
def read_skill_catalog_file(catalog_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    catalog = _skill_catalog_row(ctx, catalog_id)
    raw_root = str(catalog.get("bundle_ref") or "")
    root = Path(raw_root).resolve()
    if not raw_root or not root.is_dir():
        raise not_found("Skill file not found")
    return ok(_skill_file_payload(root, str(request.query_params.get("path") or "")))


@router.get("/sandbox-configs/{config_id}/skills")
def list_config_skills(config_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    rows = db.fetch_all(
        "SELECT * FROM tenant_skills WHERE tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL ORDER BY name",
        (ctx.tenant_id, config_id),
    )
    return ok([_skill_json(row) for row in rows])


@router.post("/sandbox-configs/{config_id}/skills", status_code=202)
async def install_config_skill(config_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    source = ""
    filename = ""
    subdir = ""
    bundle_data = b""
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None:
            raise validation("file is required")
        filename = str(getattr(upload, "filename", "") or "skill.zip")
        bundle_data = await upload.read(_SKILL_BUNDLE_LIMIT + 1)
    else:
        data = await body(request)
        source = str(data.get("source") or "")
        if not source:
            raise validation("file or source is required")
        bundle_data, filename, subdir = _download_skill_source(source)
    if not bundle_data:
        raise validation("skill bundle is empty")
    if len(bundle_data) > _SKILL_BUNDLE_LIMIT:
        raise validation("skill bundle exceeds the upload limit")
    fallback_name = filename.rsplit(".", 1)[0] or source.rstrip("/").rsplit("/", 1)[-1] or "skill"
    skill_id, now = db.new_id(), db.utcnow()
    root = _skill_root(ctx.tenant_id, skill_id)
    try:
        _extract_skill_bundle(bundle_data, filename, root)
        _normalize_skill_source_root(root, subdir)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(root, ignore_errors=True)
        raise validation(f"failed to unpack skill bundle: {exc}") from exc
    skill_file = root / "SKILL.md"
    if not skill_file.exists():
        candidates = list(root.rglob("SKILL.md"))
        skill_file = candidates[0] if candidates else skill_file
    instructions = ""
    version = "1.0.0"
    description = ""
    if skill_file.exists():
        markdown = skill_file.read_text(encoding="utf-8", errors="replace")
        name, version, description, instructions = _skill_markdown_metadata(markdown, fallback_name)
        instructions = instructions[:20000]
    else:
        name = fallback_name
    digest = hashlib.sha256(bundle_data).hexdigest()
    prompt = _skill_install_prompt(name, config_id, digest, instructions)
    summary = _skill_install_summary(name, root, digest)
    try:
        with db.transaction() as conn:
            session_id, run_id = _create_skill_install_run(
                conn,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                config_id=config_id,
                skill_id=skill_id,
                name=name,
                prompt=prompt,
                summary=summary,
                now=now,
            )
            conn.execute(
                "INSERT INTO tenant_skills(id, tenant_id, sandbox_config_id, catalog_id, name, version, "
                "description, instructions, bundle_ref, bundle_sha256, enabled, status, envs, "
                "install_session_id, install_message_id, installing_since, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 1, 'ready', '[]', ?, ?, NULL, ?, ?)",
                (
                    skill_id,
                    ctx.tenant_id,
                    config_id,
                    name,
                    version,
                    description,
                    instructions,
                    str(root),
                    digest,
                    session_id,
                    run_id,
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise conflict(f'a skill named "{name}" is already installed on this config') from exc
    return ok({"skill_id": skill_id})


@router.get("/sandbox-configs/{config_id}/skills/{skill_id}")
def get_config_skill(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    row = db.fetch_one(
        "SELECT * FROM tenant_skills WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not row:
        raise not_found("Skill not found")
    return ok(_skill_json(row))


@router.patch("/sandbox-configs/{config_id}/skills/{skill_id}")
async def patch_config_skill(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    if "enabled" in data:
        updates["enabled"] = 1 if data["enabled"] else 0
    if "envs" in data:
        updates["envs"] = dumps(data["envs"])
    with db.transaction() as conn:
        db.update("tenant_skills", updates, "id=? AND tenant_id=? AND sandbox_config_id=?", (skill_id, ctx.tenant_id, config_id), conn)
    return get_config_skill(config_id, skill_id, request)


@router.delete("/sandbox-configs/{config_id}/skills/{skill_id}")
def delete_config_skill(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    with db.transaction() as conn:
        conn.execute("UPDATE tenant_skills SET deleted_at=?, updated_at=? WHERE id=? AND tenant_id=? AND sandbox_config_id=?", (db.utcnow(), db.utcnow(), skill_id, ctx.tenant_id, config_id))
    return ok({"skill_id": skill_id})


@router.post("/sandbox-configs/{config_id}/skills/{skill_id}/reinstall", status_code=202)
async def reinstall_config_skill(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    data = await body(request)
    guidance = str(data.get("instructions") or "").strip()
    if len(guidance) > 10000:
        raise validation("invalid reinstall instructions")
    skill = db.fetch_one(
        "SELECT * FROM tenant_skills WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    root = Path(str(skill.get("bundle_ref") or "")).resolve()
    if not root.is_dir():
        raise validation("the archive of this skill is no longer stored; install it again from the original bundle")
    digest = str(skill.get("bundle_sha256") or "")
    name = str(skill.get("name") or "skill")
    prompt = _skill_install_prompt(name, config_id, digest, str(skill.get("instructions") or ""))
    if guidance:
        prompt += "\n\nOperator guidance:\n\n" + guidance
    summary = _skill_install_summary(name, root, digest, guidance)
    now = db.utcnow()
    with db.transaction() as conn:
        session_id, run_id = _create_skill_install_run(
            conn,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            config_id=config_id,
            skill_id=skill_id,
            name=name,
            prompt=prompt,
            summary=summary,
            now=now,
        )
        conn.execute(
            "UPDATE tenant_skills SET status='ready', error='', install_session_id=?, "
            "install_message_id=?, installing_since=NULL, updated_at=? "
            "WHERE id=? AND tenant_id=? AND sandbox_config_id=?",
            (session_id, run_id, now, skill_id, ctx.tenant_id, config_id),
        )
    return ok({"skill_id": skill_id})


@router.post("/sandbox-configs/{config_id}/skills/{skill_id}/stop")
def stop_config_skill(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    skill = db.fetch_one(
        "SELECT status FROM tenant_skills WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    if str(skill.get("status") or "") == "failed":
        return get_config_skill(config_id, skill_id, request)
    if str(skill.get("status") or "") != "installing":
        raise validation("skill is not installing")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tenant_skills SET status='failed', error='skill install stopped', "
            "installing_since=NULL, updated_at=? WHERE id=? AND tenant_id=? AND sandbox_config_id=?",
            (db.utcnow(), skill_id, ctx.tenant_id, config_id),
        )
    return get_config_skill(config_id, skill_id, request)


@router.get("/sandbox-configs/{config_id}/skills/{skill_id}/install-events")
def config_skill_install_events(config_id: str, skill_id: str, request: Request) -> Response:
    ctx, _ = _sandbox_config(config_id, request)
    skill = db.fetch_one(
        "SELECT status, error FROM tenant_skills WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    done = str(skill.get("status") or "") not in {"installing", "removing"}
    status_value = str(skill.get("status") or "unknown")
    payload = {
        "percent": 100 if done else 0,
        "stage": ("failed" if status_value == "failed" else "done") if done else status_value,
        "status": status_value,
        "done": done,
    }
    if skill.get("error"):
        payload["log"] = str(skill["error"])
    return Response(
        content="data: " + json.dumps(payload, ensure_ascii=False) + "\n\n",
        media_type="text/event-stream",
    )


@router.get("/sandbox-configs/{config_id}/skills/{skill_id}/transcript")
def config_skill_transcript(config_id: str, skill_id: str, request: Request) -> Response:
    ctx, _ = _sandbox_config(config_id, request)
    skill = db.fetch_one(
        "SELECT status, install_session_id, install_message_id FROM tenant_skills WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    if not skill.get("install_session_id") or not skill.get("install_message_id"):
        if str(skill.get("status") or "") == "installing":
            return Response(status_code=204)
        raise not_found("this skill has no install transcript")
    rows = db.fetch_all(
        "SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid",
        (skill["install_session_id"],),
    )
    if not rows:
        raise not_found("this install's event log is no longer available")
    install_message_id = str(skill["install_message_id"])

    def event_stream() -> Any:
        for row in rows:
            role = str(row.get("role") or "")
            frame = {
                "id": str(row.get("id") or ""),
                "response_type": "install_prompt" if role == "user" else "answer",
                "content": str(row.get("content") or ""),
                "done": role != "assistant",
                "session_id": str(skill["install_session_id"]),
                "assistant_message_id": install_message_id,
                "data": {},
            }
            yield "data: " + json.dumps(frame, ensure_ascii=False) + "\n\n"
        complete = {
            "id": install_message_id,
            "response_type": "complete",
            "content": "",
            "done": True,
            "session_id": str(skill["install_session_id"]),
            "assistant_message_id": install_message_id,
            "data": {},
        }
        yield "data: " + json.dumps(complete, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sandbox-configs/{config_id}/skills/{skill_id}/files")
def config_skill_files(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    skill = db.fetch_one(
        "SELECT bundle_ref FROM tenant_skills WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    root = Path(str(skill.get("bundle_ref") or "")).resolve()
    if not str(skill.get("bundle_ref") or "") or not root.is_dir():
        return ok([])
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
    return ok(files)


@router.get("/sandbox-configs/{config_id}/skills/{skill_id}/files/content")
def config_skill_file_content(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    skill = db.fetch_one(
        "SELECT bundle_ref FROM tenant_skills WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    relative = str(request.query_params.get("path") or "").strip()
    if not relative:
        raise validation("path is required")
    root = Path(str(skill.get("bundle_ref") or "")).resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents or not candidate.is_file():
        raise not_found("Skill file not found")
    raw = candidate.read_bytes()
    truncated = len(raw) > 2 * 1024 * 1024
    if truncated:
        raw = raw[: 2 * 1024 * 1024]
    media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    payload: dict[str, Any] = {
        "path": candidate.relative_to(root).as_posix(),
        "size": candidate.stat().st_size,
        "media_type": media_type,
        "truncated": truncated,
    }
    try:
        payload.update({"encoding": "utf-8", "content": raw.decode("utf-8"), "binary": False})
    except UnicodeDecodeError:
        if media_type.startswith("image/"):
            payload.update({"encoding": "base64", "content": base64.b64encode(raw).decode(), "binary": True})
        else:
            payload.update({"encoding": "binary", "binary": True})
    return ok(payload)


@router.get("/sandbox-configs/{config_id}/skills/{skill_id}/guidance")
def config_skill_guidance(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    skill = db.fetch_one(
        "SELECT status, install_message_id FROM tenant_skills "
        "WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    accepting = bool(
        str(skill.get("status") or "") == "installing" and skill.get("install_message_id")
    )
    rows = db.fetch_all(
        "SELECT steer_id, content, consumed FROM tenant_skill_guidance "
        "WHERE tenant_id=? AND sandbox_config_id=? AND skill_id=? AND install_message_id=? "
        "ORDER BY created_at, rowid",
        (ctx.tenant_id, config_id, skill_id, str(skill.get("install_message_id") or "")),
    )
    messages = []
    for row in rows:
        status = "injected" if bool(row.get("consumed")) else ("pending" if accepting else "unprocessed")
        messages.append({"id": str(row["steer_id"]), "content": str(row["content"]), "status": status})
    return ok({"accepting": accepting, "messages": messages})


@router.post("/sandbox-configs/{config_id}/skills/{skill_id}/guidance", status_code=202)
async def update_config_skill_guidance(config_id: str, skill_id: str, request: Request) -> dict[str, Any]:
    ctx, _ = _sandbox_config(config_id, request)
    data = await body(request)
    expected_message_id = str(data.get("expected_message_id") or "").strip()
    steer_id = str(data.get("steer_id") or "").strip()
    content = str(data.get("content") or "").strip()
    if not content or len(content) > 10000:
        raise validation("guidance must contain 1 to 10000 characters")
    try:
        uuid.UUID(steer_id)
    except (ValueError, AttributeError) as exc:
        raise validation("steer_id and expected_message_id are required") from exc
    if not expected_message_id:
        raise validation("steer_id and expected_message_id are required")
    skill = db.fetch_one(
        "SELECT status, install_message_id FROM tenant_skills "
        "WHERE id=? AND tenant_id=? AND sandbox_config_id=? AND deleted_at IS NULL",
        (skill_id, ctx.tenant_id, config_id),
    )
    if not skill:
        raise not_found("Skill not found")
    if str(skill.get("status") or "") != "installing" or str(skill.get("install_message_id") or "") != expected_message_id:
        raise conflict("the install run changed; refresh before sending guidance")

    existing = db.fetch_one(
        "SELECT content FROM tenant_skill_guidance WHERE tenant_id=? AND skill_id=? AND steer_id=?",
        (ctx.tenant_id, skill_id, steer_id),
    )
    if existing:
        if str(existing.get("content") or "") != content:
            raise conflict("steer_id already belongs to another message")
        return ok()
    rows = db.fetch_all(
        "SELECT consumed FROM tenant_skill_guidance WHERE tenant_id=? AND skill_id=? AND install_message_id=?",
        (ctx.tenant_id, skill_id, expected_message_id),
    )
    if sum(1 for row in rows if not bool(row.get("consumed"))) >= 10 or len(rows) >= 100:
        raise validation("too many install guidance messages")
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tenant_skill_guidance(tenant_id, sandbox_config_id, skill_id, steer_id, "
            "install_message_id, content, consumed, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (ctx.tenant_id, config_id, skill_id, steer_id, expected_message_id, content, now, now),
        )
    return ok()


@router.post("/system/sandbox-check")
async def sandbox_check(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    stored: dict[str, Any] = {}
    stored_type = ""
    config_id = str(data.get("config_id") or "")
    if config_id:
        row = db.fetch_one(
            "SELECT * FROM tenant_sandbox_configs WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (config_id, ctx.tenant_id),
        )
        if not row:
            raise not_found("Sandbox config not found")
        stored = loads(row.get("config"), {})
        stored_type = str(row.get("sandbox_type") or "")
    incoming = data.get("config") if isinstance(data.get("config"), dict) else {}
    effective_config = _merge_sandbox_config(incoming, stored)
    provider = str(incoming.get("sandbox_type") or stored_type or effective_config.get("sandbox_type") or "docker")
    deep = bool(data.get("deep"))
    checks: list[dict[str, Any]] = []
    all_ok = True

    def add(name: str, passed: bool, message: str = "", latency_ms: int = 0) -> None:
        nonlocal all_ok
        checks.append({"name": name, "ok": passed, "message": message, "latency_ms": latency_ms})
        if not passed:
            all_ok = False

    def skip(name: str, reason: str) -> None:
        checks.append({"name": name, "ok": None, "reason": reason})

    backend = effective_config.get(provider) if isinstance(effective_config.get(provider), dict) else {}
    capabilities = {
        "supports_volumes": False,
        "supports_pause_resume": provider in {"cube", "e2b"},
        "supports_reconnect": provider in {"docker", "cube", "e2b"},
    }
    if provider == "docker":
        add("client_build", True)
        host = str(backend.get("host") or "")
        if host.startswith(("http://", "https://", "tcp://")):
            url = host.replace("tcp://", "http://", 1).rstrip("/") + "/_ping"
            passed, message, latency = _sandbox_http_probe(url)
            add("api_url_reachable", passed, message, latency)
        else:
            socket_path = host.removeprefix("unix://") if host.startswith("unix://") else "/var/run/docker.sock"
            available = Path(socket_path).exists() or shutil.which("docker") is not None
            add("api_url_reachable", available, "" if available else "Docker daemon socket is not available")
    elif provider in {"cube", "e2b"}:
        api_url = str(backend.get("api_url") or "").strip()
        if not api_url:
            add("client_build", False, f"{provider}.api_url is required")
        else:
            add("client_build", True)
            headers = {"Accept": "application/json"}
            if backend.get("api_key"):
                if provider == "cube":
                    headers["Authorization"] = "Bearer " + str(backend["api_key"])
                else:
                    headers["X-API-Key"] = str(backend["api_key"])
            health_path = "/health" if provider == "cube" else "/v2/sandboxes?limit=1"
            passed, message, latency = _sandbox_http_probe(api_url.rstrip("/") + health_path, headers)
            if not passed and provider == "e2b":
                passed, message, latency = _sandbox_http_probe(
                    api_url.rstrip("/") + "/sandboxes?limit=1", headers
                )
            if not passed and provider == "e2b":
                passed, message, latency = _sandbox_http_probe(api_url.rstrip("/") + "/health", headers)
            add("api_url_reachable", passed, message, latency)
            add("credential_valid", passed, message if not passed else "")
            if passed and deep:
                template_passed, template_message, template_latency = _sandbox_http_probe(
                    api_url.rstrip("/") + "/templates", headers
                )
                add("template_exists", template_passed, template_message, template_latency)
            else:
                skip("template_exists", "needs_deep_check" if passed else "control_plane_unreachable")
    else:
        add("client_build", False, f"unsupported sandbox provider: {provider}")
    if provider in {"docker"} and not any(item.get("name") == "credential_valid" for item in checks):
        skip("credential_valid", "not_required")
    if not deep:
        skip("sandbox_exec", "needs_deep_check")
        skip("egress_available", "needs_deep_check")
    elif provider == "docker":
        passed, message, latency = _docker_sandbox_exec(backend, "echo weknora-ok")
        if passed and "weknora-ok" in message:
            add("sandbox_exec", True, "", latency)
        else:
            add("sandbox_exec", False, message or "sandbox did not return the probe marker", latency)
        if not passed:
            skip("egress_available", "sandbox_exec_failed")
        else:
            network = str(backend.get("network_mode") or backend.get("network") or "bridge").strip() or "bridge"
            if network == "none":
                skip("egress_available", "egress_restricted_by_policy")
            else:
                command = (
                    "if curl -fsS -o /dev/null -m 8 -I https://www.baidu.com; then echo cn:baidu; exit 0; fi; "
                    "if curl -fsS -o /dev/null -m 8 -I https://1.1.1.1; then echo intl:1.1.1.1; exit 0; fi; "
                    "echo 'all probes failed' >&2; exit 1"
                )
                egress_ok, egress_message, egress_latency = _docker_sandbox_exec(backend, command)
                add("egress_available", egress_ok, egress_message, egress_latency)
    else:
        remote_checks = await anyio.to_thread.run_sync(
            _remote_sandbox_deep_check,
            provider,
            backend,
            effective_config,
        )
        for item in remote_checks:
            if item.get("ok") is None:
                skip(str(item["name"]), str(item.get("reason") or ""))
            else:
                add(
                    str(item["name"]),
                    bool(item.get("ok")),
                    str(item.get("message") or ""),
                    int(item.get("latency_ms") or 0),
                )
    return ok({"ok": all_ok, "provider": provider, "checks": checks, "capabilities": capabilities})


def _remote_sandbox_deep_check(
    provider: str,
    backend: dict[str, Any],
    effective_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Create one throwaway Cube/E2B sandbox, probe it, then destroy it."""
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, message: str = "", latency_ms: int = 0) -> None:
        checks.append({"name": name, "ok": passed, "message": message, "latency_ms": latency_ms})

    def skip(name: str, reason: str) -> None:
        checks.append({"name": name, "ok": None, "reason": reason})

    try:
        client = remote_sandbox.RemoteSandboxClient(provider, backend, effective_config)
    except (ValueError, remote_sandbox.RemoteSandboxError) as exc:
        message = exc.reason() if isinstance(exc, remote_sandbox.RemoteSandboxError) else str(exc)
        add("template_exists", False, message)
        skip("sandbox_exec", "sandbox_not_created")
        skip("egress_available", "sandbox_not_created")
        return checks

    sandbox: remote_sandbox.RemoteSandbox | None = None
    try:
        try:
            sandbox = client.create(timeout_seconds=120)
        except remote_sandbox.RemoteSandboxError as exc:
            add("template_exists", False, exc.reason())
            skip("sandbox_exec", "sandbox_not_created")
            skip("egress_available", "sandbox_not_created")
            return checks
        add("template_exists", True)

        marker = "weknora-ok"
        try:
            result = client.exec(sandbox, "echo " + marker, timeout=30)
        except remote_sandbox.RemoteSandboxError as exc:
            add("sandbox_exec", False, exc.reason(), 0)
            skip("egress_available", "sandbox_exec_failed")
            return checks
        if result.exit_code == 0 and marker in result.stdout:
            add("sandbox_exec", True, "", result.latency_ms)
        else:
            detail = _probe_mismatch(result)
            add("sandbox_exec", False, detail, result.latency_ms)
            skip("egress_available", "sandbox_exec_failed")
            return checks

        command = (
            "if curl -fsS -o /dev/null -m 8 -I https://www.baidu.com; then echo cn:baidu; exit 0; fi; "
            "if curl -fsS -o /dev/null -m 8 -I https://1.1.1.1; then echo intl:1.1.1.1; exit 0; fi; "
            "echo 'all probes failed' >&2; exit 1"
        )
        try:
            egress = client.exec(sandbox, command, timeout=30)
        except remote_sandbox.RemoteSandboxError as exc:
            if remote_sandbox.denies_egress_by_default(effective_config):
                skip("egress_available", "egress_restricted_by_policy")
            else:
                add("egress_available", False, exc.reason(), 0)
            return checks
        if egress.exit_code == 0:
            hit = egress.stdout.strip() or "ok"
            add("egress_available", True, "reachable via " + hit, egress.latency_ms)
        elif remote_sandbox.denies_egress_by_default(effective_config):
            skip("egress_available", "egress_restricted_by_policy")
        else:
            detail = egress.stderr.strip() or egress.stdout.strip() or "国内与国际探测目标均不可达"
            add("egress_available", False, detail[:500], egress.latency_ms)
        return checks
    finally:
        if sandbox is not None:
            try:
                client.delete(sandbox)
            except remote_sandbox.RemoteSandboxError:
                pass


def _probe_mismatch(result: remote_sandbox.RemoteExecResult) -> str:
    parts = [f"退出码 {result.exit_code}"]
    detail = result.stderr.strip() or result.stdout.strip()
    if detail:
        parts.append(detail.splitlines()[0][:300])
    else:
        parts.append("没有任何输出")
    return "；".join(parts)


def _merge_sandbox_config(incoming: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    merged = dict(stored)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_sandbox_config(value, merged[key])
        elif value == "***" and key in merged:
            continue
        else:
            merged[key] = value
    return merged


def _merge_sandbox_template(
    templates: list[dict[str, Any]], template: dict[str, Any]
) -> list[dict[str, Any]]:
    template_id = str(template.get("id") or "").strip()
    if not template_id:
        return [*templates, dict(template)]
    merged: list[dict[str, Any]] = []
    replaced = False
    for item in templates:
        if str(item.get("id") or "").strip() == template_id:
            merged.append(dict(template))
            replaced = True
        else:
            merged.append(item)
    if not replaced:
        merged.append(dict(template))
    return merged


def _persist_sandbox_template_id(
    tenant_id: int, config_id: str, provider: str, template_id: str
) -> None:
    if not config_id or not template_id:
        return
    row = db.fetch_one(
        "SELECT config FROM tenant_sandbox_configs WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (config_id, tenant_id),
    )
    if not row:
        return
    config = loads(row.get("config"), {})
    backend = config.get(provider)
    if not isinstance(backend, dict):
        backend = {}
    backend["template_id"] = template_id
    config[provider] = backend
    with db.transaction() as conn:
        conn.execute(
            "UPDATE tenant_sandbox_configs SET config=?, updated_at=? WHERE id=? AND tenant_id=?",
            (dumps(config), db.utcnow(), config_id, tenant_id),
        )


def _sandbox_http_probe(url: str, headers: dict[str, str] | None = None) -> tuple[bool, str, int]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "sandbox endpoint must be an absolute http(s) URL", 0
    started = time.perf_counter()
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            latency = int((time.perf_counter() - started) * 1000)
            return 200 <= response.status < 300, "", latency
    except urllib.error.HTTPError as exc:
        latency = int((time.perf_counter() - started) * 1000)
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return False, f"HTTP {exc.code}: {detail}", latency
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        latency = int((time.perf_counter() - started) * 1000)
        return False, str(exc), latency


def _docker_sandbox_exec(
    backend: dict[str, Any],
    command: str,
    *,
    timeout: int = 60,
) -> tuple[bool, str, int]:
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker CLI is not available", 0
    image = str(backend.get("image") or "wechatopenai/weknora-sandbox:main").strip()
    host = str(backend.get("host") or "").strip()
    network = str(backend.get("network_mode") or backend.get("network") or "bridge").strip() or "bridge"
    argv = [docker]
    if host and host not in {"unix:///var/run/docker.sock"}:
        argv.extend(["-H", host])
    argv.extend(["run", "--rm", "--network", network, "--entrypoint", "sh", image, "-c", command])
    env = dict(os.environ)
    cert_path = str(backend.get("tls_cert_path") or "").strip()
    if cert_path:
        env["DOCKER_CERT_PATH"] = cert_path
        env["DOCKER_TLS_VERIFY"] = "1"
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:500], int((time.perf_counter() - started) * 1000)
    latency = int((time.perf_counter() - started) * 1000)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or f"docker exited {completed.returncode}").strip()
        return False, detail[:500], latency
    return True, (completed.stdout or "").strip(), latency


@router.post("/system/storage-engine-check")
async def storage_engine_check(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    provider = str(data.get("provider") or "").strip().lower()
    allowed = _allowed_storage_providers()
    if provider and provider not in _STORAGE_PROVIDERS:
        raise validation("unsupported storage provider")
    if provider and provider not in allowed:
        raise forbidden("该存储引擎已被禁用")
    if not provider or provider == "local":
        return {"code": 0, "data": {"ok": True, "message": "本地存储无需检测"}}

    nested = data.get(provider)
    config_data = dict(nested) if isinstance(nested, dict) else {}
    if provider == "minio" and str(config_data.get("mode") or "") != "remote":
        config_data = {
            **config_data,
            "endpoint": os.getenv("MINIO_ENDPOINT", ""),
            "access_key_id": os.getenv("MINIO_ACCESS_KEY_ID", ""),
            "secret_access_key": os.getenv("MINIO_SECRET_ACCESS_KEY", ""),
            "bucket_name": config_data.get("bucket_name") or os.getenv("MINIO_BUCKET_NAME", ""),
        }
    if provider == "cos":
        config_data.setdefault("access_key_id", config_data.get("secret_id", ""))
        config_data.setdefault("secret_access_key", config_data.get("secret_key", ""))
        if not config_data.get("endpoint"):
            region = str(config_data.get("region") or "").strip()
            bucket = str(config_data.get("bucket_name") or "").strip()
            app_id = str(config_data.get("app_id") or "").strip()
            if region and bucket and app_id:
                config_data["endpoint"] = f"https://{bucket}-{app_id}.cos.{region}.myqcloud.com"
    try:
        result = connectivity.test_storage(provider, config_data)
    except Exception as exc:  # noqa: BLE001
        return {"code": 0, "data": {"ok": False, "message": _storage_error_message(exc)}}
    bucket = str(config_data.get("bucket_name") or "")
    message = f"连接成功，Bucket「{bucket}」已确认存在" if bucket else "连接成功"
    return {"code": 0, "data": {"ok": True, "message": message}}


def _env_available(keys: tuple[str, ...]) -> bool:
    return all(str(os.getenv(key) or "").strip() for key in keys)


@router.get("/system/storage-engine-status")
def storage_engine_status(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT provider FROM storage_backends WHERE tenant_id=? AND deleted_at IS NULL AND status<>'disabled'",
        (ctx.tenant_id,),
    )
    active = {str(row.get("provider") or "").lower() for row in rows}
    minio_env = _env_available(("MINIO_ENDPOINT", "MINIO_ACCESS_KEY_ID", "MINIO_SECRET_ACCESS_KEY"))
    env_checks = {
        "minio": minio_env,
        "cos": _env_available(("COS_SECRET_ID", "COS_SECRET_KEY", "COS_REGION")),
        "tos": _env_available(("TOS_ENDPOINT", "TOS_REGION", "TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_BUCKET_NAME")),
        "s3": _env_available(("S3_ENDPOINT", "S3_REGION", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET_NAME")),
        "oss": _env_available(("OSS_ENDPOINT", "OSS_REGION", "OSS_ACCESS_KEY", "OSS_SECRET_KEY", "OSS_BUCKET_NAME")),
        "ks3": _env_available(("KS3_ENDPOINT", "KS3_REGION", "KS3_ACCESS_KEY", "KS3_SECRET_KEY", "KS3_BUCKET_NAME")),
        "obs": _env_available(("OBS_ENDPOINT", "OBS_REGION", "OBS_ACCESS_KEY", "OBS_SECRET_KEY", "OBS_BUCKET_NAME")),
    }
    details = {
        "local": "本地文件系统存储，仅适合单机部署",
        "minio": "S3 兼容的自托管对象存储，适合内网和私有云部署",
        "cos": "腾讯云对象存储服务，适合公有云部署，支持 CDN 加速",
        "tos": "火山引擎对象存储服务，适合公有云部署",
        "s3": "AWS S3 与兼容对象存储服务，适合公有云与混合云部署",
        "oss": "阿里云对象存储服务，适合公有云部署，支持 S3 兼容协议",
        "ks3": "金山云对象存储服务，适合公有云部署",
        "obs": "华为云对象存储服务，适合公有云部署",
    }
    allowed = _allowed_storage_providers()
    engines = [
        {
            "name": provider,
            "allowed": provider in allowed,
            "available": provider == "local" or provider in active or bool(env_checks.get(provider)),
            "description": details[provider],
        }
        for provider in _STORAGE_PROVIDERS
    ]
    return {
        "code": 0,
        "msg": "success",
        "data": {
            "engines": engines,
            "allowed_providers": allowed,
            "minio_env_available": minio_env,
        },
    }


@router.post("/embed-channels", status_code=201)
async def create_embed_channel(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    channel_id = db.new_id()
    token = "emb_" + secrets.token_urlsafe(24)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO embed_channels(id, tenant_id, agent_id, name, enabled, publish_token,
              allowed_origins, welcome_message, rate_limit_per_minute, rate_limit_per_day,
              primary_color, page_title, header_title_mode, show_suggested_questions,
              widget_position, allow_web_search, allow_file_upload, default_locale,
              created_at, updated_at, allow_memory)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, 60, 1000, '#07C05F', ?, 'default', 1,
              'bottom-right', 1, 1, 'zh-CN', ?, ?, 1)
            """,
            (channel_id, ctx.tenant_id, str(data.get("agent_id") or "builtin-quick-answer"), str(data.get("name") or "Web Embed"), token, dumps(data.get("allowed_origins") or ["*"]), str(data.get("welcome_message") or ""), str(data.get("page_title") or "WeKnora"), now, now),
        )
    row = db.fetch_one("SELECT * FROM embed_channels WHERE id=?", (channel_id,))
    row["token"] = token
    row.pop("publish_token", None)
    return ok(row)


@router.get("/wechat/qrcode")
@router.post("/wechat/qrcode")
def wechat_qrcode(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    try:
        return ok(wechat_service.get_login_qrcode())
    except wechat_service.WeChatError as exc:
        from ..errors import AppError

        raise AppError("failed to generate QR code: " + str(exc), status=500, code=1500) from exc


@router.post("/wechat/qrcode/status")
async def wechat_qrcode_status(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    qrcode = str(data.get("qrcode") or "").strip()
    if not qrcode:
        raise validation("qrcode is required")
    try:
        return ok(wechat_service.poll_qrcode_status(qrcode))
    except wechat_service.WeChatError as exc:
        from ..errors import AppError

        raise AppError("failed to check QR code status: " + str(exc), status=500, code=1500) from exc


@router.get("/initialization/ollama/status")
def ollama_status(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(ollama_service.status())


@router.get("/initialization/ollama/models")
def ollama_models(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    try:
        models = ollama_service.list_models()
    except ollama_service.OllamaError as exc:
        return ok({"models": []}, error=str(exc))
    return ok({"models": models})


@router.post("/initialization/ollama/models/check")
async def ollama_check(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    names = data.get("models")
    if not isinstance(names, list) or not names:
        raise validation("models is required")
    try:
        status_by_model = ollama_service.check_models([str(name) for name in names])
    except ollama_service.OllamaError as exc:
        return ok({"models": {str(name): False for name in names}}, error=str(exc))
    return ok({"models": status_by_model})


@router.post("/initialization/ollama/models/download")
async def ollama_download(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    model_name = str(data.get("modelName") or data.get("model_name") or "").strip()
    if not model_name:
        raise validation("modelName is required")
    try:
        result, message = ollama_service.download_model(model_name)
    except ollama_service.OllamaError as exc:
        raise validation(str(exc)) from exc
    return ok(result, message=message)


@router.get("/initialization/ollama/download/tasks")
def ollama_tasks(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    return ok(ollama_service.list_tasks())


@router.get("/initialization/ollama/download/progress/{task_id}")
def ollama_progress(task_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    task = ollama_service.get_task(task_id)
    if not task:
        raise not_found("下载任务不存在")
    return ok(task)


def _model_test_runtime(ctx: Any, data: dict[str, Any], model_type: str) -> dict[str, Any]:
    row: dict[str, Any] = {}
    model_id = str(data.get("modelId") or data.get("model_id") or "")
    if model_id:
        stored = model_runtime.visible_model(ctx.tenant_id, model_id)
        if not stored:
            raise not_found("Model not found")
        row = dict(stored)
    params = dict(row.get("parameters") or {}) if isinstance(row.get("parameters"), dict) else {}
    model_name = str(data.get("modelName") or data.get("model_name") or row.get("name") or "").strip()
    base_url = str(data.get("baseUrl") or data.get("base_url") or "").strip()
    api_key = str(data.get("apiKey") or data.get("api_key") or "")
    provider = str(data.get("provider") or params.get("provider") or row.get("source") or "openai-compatible").lower()
    interface_type = str(data.get("interfaceType") or data.get("interface_type") or "").lower()
    if interface_type == "ollama":
        provider = "ollama"
    if model_name:
        params["model_name"] = model_name
    if base_url:
        params["base_url"] = base_url
    if api_key:
        params["api_key"] = api_key
    app_secret = str(data.get("appSecret") or data.get("app_secret") or "")
    if app_secret:
        params["app_secret"] = app_secret
    params["provider"] = provider
    if isinstance(data.get("customHeaders") or data.get("custom_headers"), dict):
        params["custom_headers"] = data.get("customHeaders") or data.get("custom_headers")
    if isinstance(data.get("extraConfig") or data.get("extra_config"), dict):
        params["extra_config"] = data.get("extraConfig") or data.get("extra_config")
    if isinstance(data.get("spec"), dict):
        params["spec"] = data["spec"]
    row.update(
        {
            "id": model_id or str(row.get("id") or ""),
            "name": model_name or str(row.get("name") or ""),
            "display_name": str(data.get("displayName") or data.get("display_name") or row.get("display_name") or model_name),
            "source": str(data.get("source") or row.get("source") or "remote"),
            "parameters": params,
        }
    )
    runtime = model_runtime.runtime_config(row, model_type)
    if not runtime.get("base_url"):
        raise validation("模型名称和 Base URL 不能为空")
    return runtime


def _test_failure(exc: Exception, label: str) -> dict[str, Any]:
    return ok({"available": False, "message": f"{label}失败: {str(exc)[:800]}"})


_AUTH_FAILURE_MARKERS = (
    "api_key_invalid",
    "api key not valid",
    "invalid api key",
    "invalid_api_key",
    "unauthorized",
    "authentication",
)


def _chat_connection_reachable(exc: Exception) -> bool:
    if not isinstance(exc, model_runtime.UpstreamHTTPError) or exc.status != 400:
        return False
    lowered = exc.body.lower()
    return not any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


@router.post("/initialization/remote/check")
async def check_remote_model(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    runtime = _model_test_runtime(ctx, data, "KnowledgeQA")
    try:
        model_runtime.chat_completion(
            runtime,
            [{"role": "user", "content": "ping"}],
            max_tokens=8,
        )
    except Exception as exc:  # noqa: BLE001
        if _chat_connection_reachable(exc):
            return ok({"available": True, "message": "连接正常，模型可用"})
        return _test_failure(exc, "远程模型连接")
    return ok({"available": True, "message": "连接成功"})


@router.post("/initialization/embedding/test")
async def test_embedding_model(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    runtime = _model_test_runtime(ctx, data, "Embedding")
    try:
        vectors = model_runtime.embedding(runtime, ["hello"])
    except Exception as exc:  # noqa: BLE001
        return _test_failure(exc, "Embedding 测试")
    dimension = len(vectors[0]) if vectors and isinstance(vectors[0], list) else 0
    return ok({"available": bool(vectors) and dimension > 0, "message": "连接成功", "dimension": dimension})


@router.post("/initialization/rerank/check")
async def check_rerank_model(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    runtime = _model_test_runtime(ctx, data, "Rerank")
    try:
        results = model_runtime.rerank(runtime, "ping", ["pong"])
    except Exception as exc:  # noqa: BLE001
        return _test_failure(exc, "重排测试")
    if not results:
        return ok({"available": False, "message": "重排接口连接成功，但未返回重排结果"})
    return ok({"available": True, "message": f"重排功能正常，返回{len(results)}个结果"})


def _silent_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 800)
    return buffer.getvalue()


@router.post("/initialization/asr/check")
async def check_asr_model(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    runtime = _model_test_runtime(ctx, data, "ASR")
    try:
        result = model_runtime.transcribe(runtime, _silent_wav(), "silence.wav")
    except Exception as exc:  # noqa: BLE001
        return _test_failure(exc, "ASR 连接")
    text = str(result.get("text") or "").strip() if isinstance(result, dict) else ""
    return ok({"available": True, "message": f"ASR连接成功，转写结果: {text}" if text else "ASR连接成功"})


@router.post("/initialization/multimodal/test")
async def test_multimodal(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    form = await request.form()
    image = form.get("image")
    if not image or not hasattr(image, "read"):
        raise validation("只允许上传图片文件")
    content_type = str(getattr(image, "content_type", "") or "")
    if not content_type.startswith("image/"):
        raise validation("只允许上传图片文件")
    data = await image.read()
    if len(data) > 50 * 1024 * 1024:
        raise validation("图片文件大小不能超过50MB")
    model_name = str(form.get("vlm_model") or "").strip()
    base_url = str(form.get("vlm_base_url") or "").strip()
    interface_type = str(form.get("vlm_interface_type") or "")
    if interface_type == "ollama":
        base_url = ollama_service.base_url() + "/v1"
    runtime = _model_test_runtime(
        ctx,
        {
            "modelName": model_name,
            "baseUrl": base_url,
            "apiKey": str(form.get("vlm_api_key") or ""),
            "interfaceType": interface_type,
            "source": "local" if interface_type == "ollama" else "remote",
        },
        "VLLM",
    )
    started = time.perf_counter()
    try:
        response = model_runtime.chat_completion(
            runtime,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image briefly."},
                        {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64.b64encode(data).decode()}"}}],
                }
            ],
            max_tokens=256,
        )
    except Exception as exc:  # noqa: BLE001
        return ok({"success": False, "message": f"多模态测试失败: {str(exc)[:800]}"})
    caption, _ = model_runtime.text_from_chat_response(response)
    return ok({
        "success": True,
        "caption": caption,
        "ocr": "",
        "processing_time": time.perf_counter() - started,
        "message": "多模态测试成功",
    })


@router.get("/initialization/config/{kb_id}")
def initialization_config(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = readable_knowledge_base(ctx.tenant_id, kb_id, ctx.role)
    if not kb:
        raise not_found("Knowledge base not found")
    tenant_id = int(kb["tenant_id"])
    include_details = tenant_id == ctx.tenant_id and _can_view_initialization_details(ctx)
    llm = model_runtime.visible_model(tenant_id, str(kb.get("summary_model_id") or "")) or model_runtime.default_model(tenant_id, "KnowledgeQA")
    embedding = model_runtime.visible_model(tenant_id, str(kb.get("embedding_model_id") or "")) or model_runtime.default_model(tenant_id, "Embedding")
    rerank = model_runtime.default_model(tenant_id, "Rerank")
    chunking = loads(kb.get("chunking_config"), {})
    vlm = loads(kb.get("vlm_config"), {})
    extract = loads(kb.get("extract_config"), {})
    question_generation = loads(kb.get("question_generation_config"), {})
    asr = loads(kb.get("asr_config"), {})
    count = db.fetch_one(
        "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, tenant_id),
    )
    storage_provider, legacy_storage = _initialization_storage(kb)
    multimodal: dict[str, Any] = {
        "enabled": _initialization_multimodal_enabled(kb),
        "vlm": _model_config_json(
            model_runtime.visible_model(tenant_id, str(vlm.get("model_id") or "")) if isinstance(vlm, dict) else None,
            model_type="VLLM",
            include_details=include_details,
        ),
    }
    if isinstance(vlm, dict):
        if vlm.get("description_language"):
            multimodal["descriptionLanguage"] = str(vlm["description_language"])
        if vlm.get("custom_instructions"):
            multimodal["customInstructions"] = str(vlm["custom_instructions"])
    if legacy_storage.get("secret_id") or (storage_provider and storage_provider != "local"):
        multimodal["storageType"] = storage_provider
        if storage_provider == "cos":
            multimodal["cos"] = {
                "region": str(legacy_storage.get("region") or ""),
                "bucketName": str(legacy_storage.get("bucket_name") or ""),
                "appId": str(legacy_storage.get("app_id") or ""),
                "pathPrefix": str(legacy_storage.get("path_prefix") or ""),
                "credentials": {
                    "secretId": bool(legacy_storage.get("secret_id")),
                    "secretKey": bool(legacy_storage.get("secret_key")),
                },
            }
        elif storage_provider == "minio":
            multimodal["minio"] = {
                "bucketName": str(legacy_storage.get("bucket_name") or ""),
                "pathPrefix": str(legacy_storage.get("path_prefix") or ""),
            }
        if not include_details:
            for provider in ("cos", "minio"):
                detail = multimodal.get(provider)
                if isinstance(detail, dict):
                    for field in ("region", "bucketName", "appId", "pathPrefix"):
                        detail.pop(field, None)
    return ok({
        "llm": _model_config_json(llm, include_details=include_details),
        "embedding": _model_config_json(embedding, include_details=include_details),
        "rerank": {**_model_config_json(rerank, include_details=include_details), "enabled": bool(rerank)},
        "multimodal": multimodal,
        "documentSplitting": {
            "chunkSize": int(chunking.get("chunk_size") or 512) if isinstance(chunking, dict) else 512,
            "chunkOverlap": int(chunking.get("chunk_overlap") or 50) if isinstance(chunking, dict) else 50,
            "separators": chunking.get("split_markers") or ["\n\n", "\n", "。"] if isinstance(chunking, dict) else ["\n\n", "\n", "。"],
            "strategy": str(chunking.get("strategy") or "") if isinstance(chunking, dict) else "",
            "tokenLimit": int(chunking.get("token_limit") or 0) if isinstance(chunking, dict) else 0,
            "languages": chunking.get("languages") or [] if isinstance(chunking, dict) else [],
        },
        "nodeExtract": extract if isinstance(extract, dict) else {"enabled": False},
        "questionGeneration": {
            "enabled": bool(question_generation.get("enabled")),
            "questionCount": int(
                question_generation.get("question_count")
                or question_generation.get("questionCount")
                or 0
            ),
            "customInstructions": str(
                question_generation.get("custom_instructions")
                or question_generation.get("customInstructions")
                or ""
            ),
        } if isinstance(question_generation, dict) and question_generation else {"enabled": False},
        "asr": {
            "enabled": bool(asr.get("enabled")) if isinstance(asr, dict) else False,
            **(_model_config_json(
                model_runtime.visible_model(tenant_id, str(asr.get("model_id") or "")) if isinstance(asr, dict) else None,
                model_type="ASR",
                include_details=include_details,
            )),
        },
        "hasFiles": bool(count and int(count.get("n") or 0)),
    })


def _model_config_json(
    row: dict[str, Any] | None,
    *,
    model_type: str | None = None,
    include_details: bool = True,
) -> dict[str, Any]:
    if not row:
        return {"source": "remote", "modelName": "", "baseUrl": "", "credentials": {"apiKey": False}}
    params = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
    hidden = params.get("_credentials") if isinstance(params.get("_credentials"), dict) else {}
    is_builtin = bool(row.get("is_builtin"))
    configured = bool(params.get("api_key") or params.get("app_secret") or hidden.get("api_key")) and not is_builtin
    runtime = model_runtime.runtime_config(row, model_type or str(row.get("type") or "KnowledgeQA"))
    return {
        "source": str(row.get("source") or "remote"),
        "modelName": str(row.get("name") or ""),
        "baseUrl": str(runtime.get("base_url") or "") if include_details and not is_builtin else "",
        "provider": str(runtime.get("provider") or ""),
        "credentials": {"apiKey": configured},
    }


def _can_view_initialization_details(ctx: Any) -> bool:
    if ctx.role in {"admin", "owner"}:
        return True
    if ctx.api_key is not None:
        return ctx.api_key.full_access or ctx.api_key.has("manage_tenant_settings")
    return False


def _initialization_storage(kb: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    legacy = loads(kb.get("cos_config"), {})
    legacy = legacy if isinstance(legacy, dict) else {}
    provider_config = loads(kb.get("storage_provider_config"), {})
    provider = ""
    if isinstance(provider_config, dict):
        provider = str(provider_config.get("provider") or "").strip().lower()
    if not provider or provider == "__pending_env__":
        provider = str(legacy.get("provider") or "").strip().lower()
    return provider, legacy


def _initialization_multimodal_enabled(kb: dict[str, Any]) -> bool:
    if image_multimodal.vlm_configured(kb):
        return True
    provider, legacy = _initialization_storage(kb)
    return bool(
        legacy.get("secret_id")
        or legacy.get("bucket_name")
        or (provider and provider != "local")
    )


@router.put("/initialization/config/{kb_id}")
async def update_initialization_config(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "contributor")
    data = await body(request)
    _save_initialization_config(ctx, kb_id, data, apply_storage=True, replace_models=True)
    return initialization_config(kb_id, request)


@router.post("/initialization/initialize/{kb_id}")
async def initialize_kb(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "contributor")
    data = await body(request)
    _save_initialization_config(ctx, kb_id, data)
    return ok({"initialized": True})


def _save_initialization_config(
    ctx: Any,
    kb_id: str,
    data: dict[str, Any],
    *,
    apply_storage: bool = False,
    replace_models: bool = False,
) -> None:
    kb = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, ctx.tenant_id),
    )
    if not kb:
        raise not_found("Knowledge base not found")
    if str(kb.get("creator_id") or "") != ctx.user_id:
        require_role(ctx, "admin")
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    llm_model_id = str(data.get("llmModelId") or data.get("llm_model_id") or "").strip()
    embedding_model_id = str(
        data.get("embeddingModelId") or data.get("embedding_model_id") or ""
    ).strip()
    if replace_models:
        if llm_model_id and not model_runtime.visible_model(ctx.tenant_id, llm_model_id):
            raise validation("LLM模型不存在")
        if embedding_model_id and not model_runtime.visible_model(ctx.tenant_id, embedding_model_id):
            raise validation("Embedding模型不存在")
        current_embedding_id = str(kb.get("embedding_model_id") or "").strip()
        if current_embedding_id and embedding_model_id and current_embedding_id != embedding_model_id:
            count = db.fetch_one(
                "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
                (kb_id, ctx.tenant_id),
            )
            if count and int(count.get("n") or 0) > 0:
                raise validation("知识库中已有文件，无法修改Embedding模型")
    if llm_model_id:
        updates["summary_model_id"] = llm_model_id
    if embedding_model_id:
        updates["embedding_model_id"] = embedding_model_id
    if replace_models:
        updates["vlm_config"] = dumps(_initialization_vlm_config(ctx.tenant_id, data))
        updates["asr_config"] = dumps(_initialization_asr_config(ctx.tenant_id, data))
    elif isinstance(data.get("vlm_config"), dict):
        current = loads(kb.get("vlm_config"), {})
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(data["vlm_config"])
        updates["vlm_config"] = dumps(merged)
    if not replace_models and isinstance(data.get("asr_config"), dict):
        updates["asr_config"] = dumps(data["asr_config"])
    if isinstance(data.get("documentSplitting"), dict):
        splitting = data["documentSplitting"]
        updates["chunking_config"] = dumps({
            "chunk_size": int(splitting.get("chunkSize") or splitting.get("chunk_size") or 512),
            "chunk_overlap": int(splitting.get("chunkOverlap") or splitting.get("chunk_overlap") or 50),
            "split_markers": splitting.get("separators") or ["\n\n", "\n", "。"],
            "strategy": str(splitting.get("strategy") or ""),
            "token_limit": int(splitting.get("tokenLimit") or splitting.get("token_limit") or 0),
            "languages": splitting.get("languages") or [],
        })
    if isinstance(data.get("nodeExtract"), dict):
        updates["extract_config"] = dumps(data["nodeExtract"])
    if isinstance(data.get("questionGeneration"), dict):
        generation = data["questionGeneration"]
        enabled = bool(generation.get("enabled"))
        question_count = int(
            generation.get("questionCount") or generation.get("question_count") or 3
        ) if enabled else 0
        question_count = min(max(question_count, 1), 10) if enabled else 0
        updates["question_generation_config"] = dumps({
            "enabled": enabled,
            "question_count": question_count,
            "custom_instructions": str(
                generation.get("customInstructions") or generation.get("custom_instructions") or ""
            ).strip(),
        })
    if apply_storage:
        _apply_initialization_storage(ctx, kb, kb_id, data, updates)
    elif "storageBackendId" in data:
        updates["storage_backend_id"] = str(data.get("storageBackendId") or "") or None
    if isinstance(data.get("indexing_strategy"), dict):
        updates["indexing_strategy"] = dumps(data["indexing_strategy"])
    with db.transaction() as conn:
        db.update("knowledge_bases", updates, "id=? AND tenant_id=?", (kb_id, ctx.tenant_id), conn)


def _apply_initialization_storage(
    ctx: Any, kb: dict[str, Any], kb_id: str, data: dict[str, Any], updates: dict[str, Any]
) -> None:
    backend_id = str(
        data.get("storageBackendId") or data.get("storage_backend_id") or ""
    ).strip()
    if backend_id:
        backend = db.fetch_one(
            "SELECT * FROM storage_backends WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (backend_id, ctx.tenant_id),
        )
        if not backend or str(backend.get("status") or "").lower() not in {"active", "ready", ""}:
            raise validation("Storage backend is unavailable")
        current_backend_id = str(kb.get("storage_backend_id") or "").strip()
        if current_backend_id and current_backend_id != backend_id:
            count = db.fetch_one(
                "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
                (kb_id, ctx.tenant_id),
            )
            if count and int(count.get("n") or 0) > 0:
                raise validation(
                    "Storage backend cannot be changed while the knowledge base contains files; migrate storage first"
                )
        updates["storage_backend_id"] = backend_id
        provider = str(backend.get("provider") or "").strip().lower()
    else:
        provider = str(
            data.get("storageProvider") or data.get("storage_provider") or ""
        ).strip().lower() or "local"
    if provider not in _allowed_storage_providers():
        raise validation("Storage provider is not allowed by STORAGE_ALLOW_LIST")
    current = loads(kb.get("storage_provider_config"), {})
    provider_config = dict(current) if isinstance(current, dict) else {}
    provider_config["provider"] = provider
    updates["storage_provider_config"] = dumps(provider_config)


def _initialization_vlm_config(tenant_id: int, data: dict[str, Any]) -> dict[str, Any]:
    config = {"enabled": False, "model_id": ""}
    incoming = data.get("vlm_config")
    if not isinstance(incoming, dict):
        return config
    multimodal = data.get("multimodal") if isinstance(data.get("multimodal"), dict) else {}
    model_id = str(incoming.get("model_id") or incoming.get("modelId") or "").strip()
    if bool(incoming.get("enabled")) and bool(multimodal.get("enabled")) and model_runtime.visible_model(tenant_id, model_id):
        config.update({"enabled": True, "model_id": model_id})
    description = str(
        incoming.get("description_language") or incoming.get("descriptionLanguage") or ""
    ).strip()
    instructions = str(
        incoming.get("custom_instructions") or incoming.get("customInstructions") or ""
    ).strip()
    if description:
        config["description_language"] = description
    if instructions:
        config["custom_instructions"] = instructions
    return config


def _initialization_asr_config(tenant_id: int, data: dict[str, Any]) -> dict[str, Any]:
    config = {"enabled": False, "model_id": "", "language": ""}
    incoming = data.get("asr_config")
    if not isinstance(incoming, dict) or not bool(incoming.get("enabled")):
        return config
    model_id = str(incoming.get("model_id") or incoming.get("modelId") or "").strip()
    if model_id and model_runtime.visible_model(tenant_id, model_id):
        config.update({
            "enabled": True,
            "model_id": model_id,
            "language": str(incoming.get("language") or ""),
        })
    return config


_TAG_OPTIONS = [
    "Content",
    "Culture",
    "Person",
    "Event",
    "Time",
    "Location",
    "Work",
    "Author",
    "Relation",
    "Attribute",
]

_GRAPH_EXTRACTION_DESCRIPTION = """\
Based on the given text, complete the information extraction task following these steps, ensuring clear logic and complete, accurate information:

## Step 1: Entity Extraction and Attribute Enrichment
1. **Extract core entities**: Read through the text and extract all core entities relevant to the task in logical order (such as narrative order or entity association closeness).
2. **Enrich entity attributes**: For each extracted entity, comprehensively supplement its detailed attributes explicitly mentioned in the text, ensuring no key attributes are omitted.

## Step 2: Relationship Extraction and Verification
1. **Identify relationship types**: Select corresponding types only from the specified relationship list. Allowed relationship types are: %s.
2. **Extract valid relationships**: Based on the extracted entities and attributes, identify relationships that genuinely exist in the text, ensuring relationships are factually accurate with no false associations.
3. **Clarify relationship subjects**: For each extracted relationship, clearly annotate the two associated entities to avoid subject confusion.
4. **Supplement related attributes**: If the text contains supplementary information directly related to a relationship, include it as a related attribute of the relationship."""

_GRAPH_EXAMPLE_TEXT = (
    '"Romeo and Juliet" is a tragedy written by William Shakespeare early in his career about the romance '
    'between two Italian youths from feuding families. It was among Shakespeare\'s most popular plays during '
    'his lifetime. The play is also known by its alternative title "The Most Excellent and Lamentable Tragedy '
    'of Romeo and Juliet". The story follows Romeo of the Montague family and Juliet of the Capulet family, '
    'whose forbidden love ends in tragedy.'
)

_GRAPH_EXAMPLE_ITEMS = [
    {
        "entity": "Romeo and Juliet",
        "entity_attributes": [
            "A tragedy by William Shakespeare",
            "Also known as 'The Most Excellent and Lamentable Tragedy of Romeo and Juliet'",
            "Among Shakespeare's most popular plays",
        ],
    },
    {
        "entity": "The Most Excellent and Lamentable Tragedy of Romeo and Juliet",
        "entity_attributes": ["Alternative title for Romeo and Juliet"],
    },
    {
        "entity": "William Shakespeare",
        "entity_attributes": ["Playwright", "Author of Romeo and Juliet, written early in his career"],
    },
    {"entity1": "Romeo and Juliet", "entity2": "William Shakespeare", "relation": "Author"},
    {
        "entity1": "Romeo and Juliet",
        "entity2": "The Most Excellent and Lamentable Tragedy of Romeo and Juliet",
        "relation": "Alias",
    },
]

_FABRI_TEXT_WITH_TAG = (
    "Please randomly generate a text related to %s, with a word count between [50-200], and try to include "
    "some professional terms or typical elements related to these tags to make the text more targeted and relevant."
)

_FABRI_TEXT_WITHOUT_TAG = (
    "Please randomly generate a text with freely chosen content, with a word count between [50-200]."
)


def _extract_chat_runtime(ctx: Any, model_id: str) -> dict[str, Any]:
    if not model_id:
        raise validation("获取模型失败: model ID cannot be empty")
    model = model_runtime.visible_model(ctx.tenant_id, model_id)
    if not model:
        raise validation("获取模型失败: model not found")
    runtime = model_runtime.runtime_config(model, "KnowledgeQA")
    if not model_runtime.available(runtime):
        raise validation("获取模型失败: model base URL is not configured")
    return runtime


def _chat_text(runtime: dict[str, Any], messages: list[dict[str, Any]], *, max_tokens: int) -> str:
    response = model_runtime.chat_completion(runtime, messages, temperature=0.3, max_tokens=max_tokens)
    content, _message = model_runtime.text_from_chat_response(response)
    return content


def _graph_json_payload(content: str) -> Any:
    candidates = [
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", content, flags=re.IGNORECASE)
    ]
    if content.strip():
        candidates.append(content.strip())
    decoder = json.JSONDecoder()
    for candidate in candidates:
        for match in re.finditer(r"[\[{]", candidate):
            try:
                value, _end = decoder.raw_decode(candidate[match.start() :])
            except ValueError:
                continue
            return value
    raise RuntimeError("failed to parse JSON content from model response")


def _parse_graph(content: str, tags: list[str]) -> dict[str, list[dict[str, Any]]]:
    value = _graph_json_payload(content)
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        raise RuntimeError("expected list or dict from model response")

    nodes: dict[str, dict[str, Any]] = {}
    relations: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("each item in the sequence must be a mapping")
        if item.get("entity") is not None:
            name = str(item["entity"])
            attributes = item.get("entity_attributes")
            values = [str(attribute) for attribute in attributes] if isinstance(attributes, list) else []
            node = nodes.setdefault(name, {"name": name, "attributes": []})
            for attribute in values:
                if attribute not in node["attributes"]:
                    node["attributes"].append(attribute)
        elif item.get("entity1") is not None and item.get("entity2") is not None:
            relations.append(
                {
                    "node1": str(item["entity1"]),
                    "node2": str(item["entity2"]),
                    "type": str(item.get("relation") or ""),
                }
            )

    allowed = set(tags)
    valid_relations = []
    for relation in relations:
        if relation["node1"] == relation["node2"] or relation["type"] not in allowed:
            continue
        for name in (relation["node1"], relation["node2"]):
            nodes.setdefault(name, {"name": name, "attributes": []})
        valid_relations.append(relation)
    return {"nodes": list(nodes.values()), "relations": valid_relations}


@router.post("/initialization/extract/fabri-tag")
def fabri_tag(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    count = random.randint(1, len(_TAG_OPTIONS) - 1)
    return ok({"tags": random.sample(_TAG_OPTIONS, count)})


@router.post("/initialization/extract/fabri-text")
async def fabri_text(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    tags = data.get("tags")
    if tags is not None and (not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)):
        raise validation("tags must be a string array")
    prompt = _FABRI_TEXT_WITHOUT_TAG
    if tags:
        prompt = _FABRI_TEXT_WITH_TAG % json.dumps(tags, ensure_ascii=False)
    runtime = _extract_chat_runtime(ctx, str(data.get("model_id") or ""))
    return ok({"text": _chat_text(runtime, [{"role": "user", "content": prompt}], max_tokens=4096)})


@router.post("/initialization/extract/text-relation")
async def extract_text_relations(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    text = data.get("text")
    tags = data.get("tags")
    if not isinstance(text, str) or not text:
        raise validation("文本内容不能为空")
    if len(text.encode("utf-8")) > 5000:
        raise validation("文本内容长度不能超过5000字符")
    if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag for tag in tags):
        raise validation("至少需要选择一个关系标签")

    runtime = _extract_chat_runtime(ctx, str(data.get("model_id") or ""))
    description = _GRAPH_EXTRACTION_DESCRIPTION % json.dumps(tags, ensure_ascii=False)
    example = "```json\n" + json.dumps(_GRAPH_EXAMPLE_ITEMS, ensure_ascii=False, indent=2) + "\n```"
    system = "\n".join([description, "# Examples", f"Q: {_GRAPH_EXAMPLE_TEXT}", f"A: {example}", ""])
    user = f"# Question\nQ: {text}\nA: "
    content = _chat_text(
        runtime,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=8192,
    )
    return ok(_parse_graph(content, tags))

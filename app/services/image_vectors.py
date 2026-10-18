"""Index images referenced by knowledge chunks with a multimodal model."""

from __future__ import annotations

import base64
import hashlib
import re
import urllib.parse
from typing import Any

from .. import db
from ..utils import dumps, loads
from . import file_proxy, model_runtime, object_storage, url_guard


MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_CHILD_TYPES = {"image", "image_ocr", "image_caption"}


def image_vectors_enabled(kb: dict[str, Any]) -> bool:
    strategy = loads(kb.get("indexing_strategy"), {})
    if isinstance(strategy, dict) and strategy.get("vector_enabled") is False:
        return False
    config = loads(kb.get("image_processing_config"), {})
    return isinstance(config, dict) and bool(config.get("image_vector_enabled"))


def detect_image_mime(data: bytes) -> str:
    try:
        return model_runtime.image_mime(data)
    except Exception:  # noqa: BLE001 - not an image
        return ""


def _embedding_runtime(kb: dict[str, Any], knowledge: dict[str, Any]) -> dict[str, Any]:
    tenant_id = int(knowledge.get("tenant_id") or kb.get("tenant_id") or 0)
    model_id = str(kb.get("embedding_model_id") or knowledge.get("embedding_model_id") or "")
    row = (
        model_runtime.visible_model(tenant_id, model_id)
        if model_id
        else model_runtime.default_model(tenant_id, "Embedding")
    )
    return model_runtime.runtime_config(row, "Embedding")


def _read_image(reference: str, tenant_id: int) -> bytes:
    value = str(reference or "").strip()
    if not value:
        raise ValueError("image reference is empty")
    if value.startswith("data:"):
        header, separator, payload = value.partition(",")
        if not separator:
            raise ValueError("invalid data URI")
        data = base64.b64decode(payload) if ";base64" in header else urllib.parse.unquote_to_bytes(payload)
    elif object_storage.is_object_reference(value):
        data = object_storage.read_object(value, tenant_id)
    elif value.startswith(("http://", "https://")):
        data, _headers, _url = url_guard.fetch_public_url(
            value, timeout=30, max_bytes=MAX_IMAGE_BYTES
        )
    else:
        path, _name = file_proxy.resolve_file(value, tenant_id)
        data = path.read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is {len(data)} bytes; the limit is {MAX_IMAGE_BYTES}")
    return data


def extract_image_entries(content: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    markdown = re.compile(
        r"!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+['\"][^'\"]*['\"])?\s*\)",
        re.IGNORECASE | re.DOTALL,
    )
    html = re.compile(r"<img\b[^>]*\bsrc\s*=\s*(['\"])(.*?)\1[^>]*>", re.IGNORECASE | re.DOTALL)
    found: list[tuple[str, str, int, int]] = [
        (str(match.group(2) or match.group(3) or ""), str(match.group(1) or ""), match.start(), match.end())
        for match in markdown.finditer(content or "")
    ]
    for match in html.finditer(content or ""):
        alt = re.search(r"\balt\s*=\s*(['\"])(.*?)\1", match.group(0), re.IGNORECASE | re.DOTALL)
        found.append(
            (
                str(match.group(2) or ""),
                str(alt.group(2) if alt else ""),
                match.start(),
                match.end(),
            )
        )
    found.sort(key=lambda item: item[2])
    for url, caption, start, end in found:
        url = url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        entries.append(
            {
                "url": url,
                "original_url": url,
                "caption": caption.strip(),
                "start_pos": start,
                "end_pos": end,
            }
        )
    return entries


def _candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if str(row.get("chunk_type") or "") == "image_vector":
            continue
        images = loads(row.get("image_info"), [])
        if not isinstance(images, list):
            continue
        parent = str(row.get("parent_chunk_id") or row.get("id") or "")
        if str(row.get("chunk_type") or "") in IMAGE_CHILD_TYPES and row.get("parent_chunk_id"):
            parent = str(row["parent_chunk_id"])
        for image in images:
            if not isinstance(image, dict):
                continue
            url = str(image.get("url") or image.get("original_url") or "").strip()
            if not url:
                continue
            caption = str(image.get("caption") or "").strip()
            ocr_text = str(image.get("ocr_text") or "").strip()
            content = caption or ocr_text
            if not content and str(row.get("chunk_type") or "") in {"image_caption", "image_ocr"}:
                content = str(row.get("content") or "").strip()
            key = (parent, url)
            previous = candidates.get(key)
            if previous is not None and (previous["caption"] or not caption):
                continue
            candidates[key] = {
                "parent_id": parent or None,
                "source_id": str(row.get("id") or ""),
                "url": url,
                "caption": caption,
                "ocr_text": ocr_text,
                "content": content,
                "image_info": image,
            }
    return list(candidates.values())


def _store_vector(
    *,
    tenant_id: int,
    knowledge: dict[str, Any],
    candidate: dict[str, Any],
    vector: list[float],
) -> str:
    now = db.utcnow()
    metadata = dumps(
        {
            "image_url": candidate["url"],
            "source_chunk_id": candidate["source_id"],
        }
    )
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT id FROM chunks WHERE tenant_id=? AND knowledge_id=? AND chunk_type='image_vector' "
            "AND COALESCE(parent_chunk_id, '')=? AND json_extract(metadata, '$.image_url')=? "
            "AND deleted_at IS NULL LIMIT 1",
            (tenant_id, knowledge["id"], candidate["parent_id"] or "", candidate["url"]),
        ).fetchone()
        if existing:
            chunk_id = str(existing["id"])
            conn.execute(
                "UPDATE chunks SET content=?, source_content=?, image_info=?, embedding=?, "
                "index_status='indexed', status=2, is_enabled=1, metadata=?, updated_at=? "
                "WHERE id=? AND tenant_id=?",
                (
                    candidate["content"],
                    candidate["content"],
                    dumps([candidate["image_info"]]),
                    dumps(vector),
                    metadata,
                    now,
                    chunk_id,
                    tenant_id,
                ),
            )
            return chunk_id
        row = conn.execute(
            "SELECT COALESCE(MAX(chunk_index), -1) + 1 AS next_index FROM chunks "
            "WHERE tenant_id=? AND knowledge_id=?",
            (tenant_id, knowledge["id"]),
        ).fetchone()
        chunk_id = db.new_id()
        conn.execute(
            """
            INSERT INTO chunks
            (id, tenant_id, knowledge_base_id, knowledge_id, content, source_content,
             content_revision, index_status, last_editor_id, context_header,
             chunk_index, is_enabled, start_at, end_at, chunk_type, parent_chunk_id,
             image_info, metadata, status, content_hash, flags, embedding, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'indexed', '', '', ?, 1, 0, 0, 'image_vector', ?,
                    ?, ?, 2, ?, 1, ?, ?, ?)
            """,
            (
                chunk_id,
                tenant_id,
                knowledge["knowledge_base_id"],
                knowledge["id"],
                candidate["content"],
                candidate["content"],
                int(row["next_index"] or 0),
                candidate["parent_id"],
                dumps([candidate["image_info"]]),
                metadata,
                hashlib.sha256(candidate["content"].encode("utf-8")).hexdigest(),
                dumps(vector),
                now,
                now,
            ),
        )
    return chunk_id


def _prune_vectors(tenant_id: int, knowledge_id: str, keys: set[tuple[str, str]]) -> None:
    rows = db.fetch_all(
        "SELECT id, parent_chunk_id, metadata FROM chunks WHERE tenant_id=? AND knowledge_id=? "
        "AND chunk_type='image_vector' AND deleted_at IS NULL",
        (tenant_id, knowledge_id),
    )
    stale = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        metadata = loads(row.get("metadata"), {})
        url = str(metadata.get("image_url") or "") if isinstance(metadata, dict) else ""
        key = (str(row.get("parent_chunk_id") or ""), url)
        if key not in keys or key in seen:
            stale.append(str(row["id"]))
        else:
            seen.add(key)
    if not stale:
        return
    with db.transaction() as conn:
        marks = ",".join("?" for _ in stale)
        conn.execute(f"DELETE FROM chunks WHERE id IN ({marks}) AND tenant_id=?", (*stale, tenant_id))


def index_knowledge_images(
    tenant_id: int,
    kb: dict[str, Any],
    knowledge: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"enabled": False, "indexed": 0, "skipped": 0, "failed": 0, "items": []}
    if not image_vectors_enabled(kb):
        return result
    metadata = loads(knowledge.get("metadata"), {})
    if isinstance(metadata, dict) and metadata.get("image_source_type") == "scanned_pdf":
        result.update({"enabled": True, "skipped": 1, "items": [{"status": "skipped: scanned page"}]})
        return result
    runtime = runtime if runtime is not None else _embedding_runtime(kb, knowledge)
    if not model_runtime.available(runtime) or not model_runtime.embedding_accepts_images(runtime):
        result.update({"enabled": True, "skipped": 1, "items": [{"status": "skipped: model does not accept images"}]})
        return result
    rows = db.fetch_all(
        "SELECT * FROM chunks WHERE tenant_id=? AND knowledge_id=? AND deleted_at IS NULL "
        "ORDER BY chunk_index",
        (tenant_id, knowledge["id"]),
    )
    result["enabled"] = True
    candidates = _candidates(rows)
    _prune_vectors(
        tenant_id,
        str(knowledge["id"]),
        {(candidate["parent_id"] or "", candidate["url"]) for candidate in candidates},
    )
    if not candidates:
        return result
    expected_dimensions = int(runtime.get("dimension") or 0)
    if not expected_dimensions:
        probe = next((candidate["content"] for candidate in candidates if candidate["content"]), "")
        if probe:
            try:
                expected_dimensions = len(model_runtime.embedding(runtime, [probe])[0])
            except Exception:  # noqa: BLE001 - an unknown width just skips the check
                expected_dimensions = 0
    items = [{"url": candidate["url"], "status": ""} for candidate in candidates]
    pending: list[tuple[int, bytes]] = []

    def flush_pending() -> None:
        if not pending:
            return
        batch, pending[:] = pending[:], []
        try:
            vectors = model_runtime.image_embeddings(
                runtime, [data for _position, data in batch]
            )
            if len(vectors) != len(batch):
                raise ValueError("upstream returned an unexpected embedding count")
        except Exception as exc:  # noqa: BLE001 - the whole batch is unusable
            for position, _data in batch:
                items[position]["status"] = f"failed: {exc}"
                result["failed"] += 1
            return
        for (position, _data), vector in zip(batch, vectors):
            candidate = candidates[position]
            item = items[position]
            try:
                if expected_dimensions and len(vector) != expected_dimensions:
                    raise ValueError(
                        f"{len(vector)} dimensions, index has {expected_dimensions}"
                    )
                chunk_id = _store_vector(
                    tenant_id=tenant_id,
                    knowledge=knowledge,
                    candidate=candidate,
                    vector=vector,
                )
                item.update({"status": "indexed", "chunk_id": chunk_id})
                result["indexed"] += 1
            except Exception as exc:  # noqa: BLE001 - image indexing is best effort
                item["status"] = f"failed: {exc}"
                result["failed"] += 1

    batch_size = model_runtime.image_embedding_batch_size(runtime)
    for position, candidate in enumerate(candidates):
        item = items[position]
        if not candidate["content"]:
            item["status"] = "skipped: no caption or OCR text"
            result["skipped"] += 1
            continue
        try:
            data = _read_image(candidate["url"], tenant_id)
            pending.append((position, data))
            if len(pending) >= batch_size:
                flush_pending()
        except Exception as exc:  # noqa: BLE001 - image indexing is best effort
            item["status"] = f"failed: {exc}"
            result["failed"] += 1
    flush_pending()
    result["items"] = items
    return result


def index_knowledge_base_images(tenant_id: int, kb: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"indexed": 0, "skipped": 0, "failed": 0}
    if not image_vectors_enabled(kb):
        return result
    rows = db.fetch_all(
        "SELECT * FROM knowledges WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL",
        (tenant_id, kb["id"]),
    )
    runtime = _embedding_runtime(kb, {})
    if not model_runtime.available(runtime) or not model_runtime.embedding_accepts_images(runtime):
        return result
    for knowledge in rows:
        item = index_knowledge_images(tenant_id, kb, knowledge, runtime=runtime)
        for key in ("indexed", "skipped", "failed"):
            result[key] += int(item[key])
    return result

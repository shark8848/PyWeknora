"""Hybrid keyword/vector retrieval over SQLite chunks."""

from __future__ import annotations

import base64
import json
import math
import re
from collections import Counter
from typing import Any

from .. import db
from ..utils import loads
from . import image_prep, image_vectors, model_runtime
from .embeddings import cosine, embed_texts, local_embedding
from .image_vectors import image_vectors_enabled


IMAGE_VECTOR_THRESHOLD = 0.2
CONTEXT_IMAGE_LIMIT = 3
VISION_IMAGE_MAX_BYTES = 5_000_000
VISION_IMAGE_MIME_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"]
MMR_LAMBDA = 0.7


class _RetrievalRows(list[dict[str, Any]]):
    def __init__(self, rows: list[dict[str, Any]], candidates: int) -> None:
        super().__init__(rows)
        self.candidates = candidates


def _terms(text: str) -> list[str]:
    text = text.lower()
    words = re.findall(r"[a-z0-9_]{2,}", text)
    cjk = re.findall(r"[\u3400-\u9fff]", text)
    grams = ["".join(cjk[i:i + 2]) for i in range(max(0, len(cjk) - 1))]
    return words + cjk + grams


def keyword_score(query: str, content: str) -> float:
    query_terms = Counter(_terms(query))
    if not query_terms:
        return 0.0
    content_lower = content.lower()
    hits = sum(count for term, count in query_terms.items() if term in content_lower)
    exact = 1.5 if query.strip().lower() in content_lower else 0.0
    return (hits / max(1, len(query_terms))) + exact


def _passage_text(row: dict[str, Any]) -> str:
    parts = [str(row.get("content") or "")]
    images = loads(row.get("image_info"), [])
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict):
                continue
            parts.extend(
                str(image.get(key) or "")
                for key in ("caption", "ocr_text")
                if str(image.get(key) or "").strip()
            )
    metadata = loads(row.get("metadata"), {})
    questions = metadata.get("generated_questions") if isinstance(metadata, dict) else None
    if isinstance(questions, list):
        for question in questions:
            value = question.get("question") if isinstance(question, dict) else question
            if str(value or "").strip():
                parts.append(str(value))
    text = "\n".join(parts)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", text, flags=re.M)
    text = re.sub(r"\*\*|__|\*|_", "", text)
    return text.strip()


def _passage_tokens(row: dict[str, Any]) -> set[str]:
    return set(_terms(_passage_text(row)))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def mmr_indices(
    rows: list[dict[str, Any]],
    k: int,
    lambda_: float = MMR_LAMBDA,
) -> list[int]:
    """Select up to k rows by relevance and passage-level redundancy."""
    if k <= 0 or not rows:
        return []
    tokens = [_passage_tokens(row) for row in rows]
    remaining = list(range(len(rows)))
    selected: list[int] = []
    while remaining and len(selected) < k:
        best_pos = 0
        best_value = -math.inf
        for position, index in enumerate(remaining):
            redundancy = max(
                (_jaccard(tokens[index], tokens[chosen]) for chosen in selected),
                default=0.0,
            )
            value = lambda_ * float(rows[index].get("score") or 0.0) - (1.0 - lambda_) * redundancy
            if value > best_value:
                best_pos = position
                best_value = value
        selected.append(remaining.pop(best_pos))
    return selected


def retrieve(
    tenant_id: int,
    query: str,
    *,
    knowledge_base_ids: list[str] | None = None,
    tag_ids: list[str] | None = None,
    knowledge_ids: list[str] | None = None,
    limit: int = 10,
    embedding_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    clauses = [
        "(c.tenant_id = ? OR EXISTS ("
        "SELECT 1 FROM kb_shares s "
        "JOIN organization_tenant_members m ON m.organization_id=s.organization_id "
        "JOIN organizations o ON o.id=s.organization_id AND o.deleted_at IS NULL "
        "WHERE s.knowledge_base_id=c.knowledge_base_id AND s.deleted_at IS NULL AND m.tenant_id=?"
        "))",
        "c.deleted_at IS NULL",
        "c.is_enabled = 1",
        "k.deleted_at IS NULL",
        "k.parse_status NOT IN ('failed', 'deleting')",
    ]
    params: list[Any] = [tenant_id, tenant_id]
    if knowledge_base_ids:
        clauses.append("c.knowledge_base_id IN (" + ",".join("?" for _ in knowledge_base_ids) + ")")
        params.extend(knowledge_base_ids)
    if tag_ids:
        clauses.append(
            "EXISTS (SELECT 1 FROM knowledge_tag_relations tr WHERE tr.knowledge_id=c.knowledge_id AND tr.tag_id IN ("
            + ",".join("?" for _ in tag_ids)
            + "))"
        )
        params.extend(tag_ids)
    if knowledge_ids:
        clauses.append(
            "c.knowledge_id IN (" + ",".join("?" for _ in knowledge_ids) + ")"
        )
        params.extend(knowledge_ids)
    rows = db.fetch_all(
        f"""
        SELECT c.*, k.title, k.file_name, k.type AS knowledge_type,
               k.description AS knowledge_description, k.source AS knowledge_source,
               k.channel AS knowledge_channel, k.custom_metadata AS knowledge_custom_metadata,
               k.knowledge_base_id, kb.name AS knowledge_base_name,
               kb.image_processing_config AS kb_image_processing_config,
               kb.indexing_strategy AS kb_indexing_strategy
        FROM chunks c
        JOIN knowledges k ON k.id = c.knowledge_id
        JOIN knowledge_bases kb ON kb.id = c.knowledge_base_id
        WHERE {' AND '.join(clauses)}
        ORDER BY c.chunk_index ASC
        LIMIT 5000
        """,
        params,
    )
    if not rows:
        return []
    query_vector = embed_texts([query], runtime=embedding_config)[0]
    accepts_images = bool(
        embedding_config and model_runtime.embedding_accepts_images(embedding_config)
    )
    image_kbs: dict[str, bool] = {}
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        chunk_type = str(row.get("chunk_type") or "")
        if chunk_type == "image_vector":
            kb_id = str(row.get("knowledge_base_id") or "")
            if kb_id not in image_kbs:
                image_kbs[kb_id] = image_vectors_enabled(
                    {
                        "image_processing_config": row.get("kb_image_processing_config"),
                        "indexing_strategy": row.get("kb_indexing_strategy"),
                    }
                )
            if not accepts_images or not image_kbs[kb_id]:
                continue
            try:
                stored = json.loads(row.get("embedding") or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(stored, list) or len(stored) != len(query_vector):
                continue
            lexical = 0.0
            vector = cosine(query_vector, stored)
            if vector < IMAGE_VECTOR_THRESHOLD:
                continue
            score = vector
        else:
            lexical = keyword_score(query, row.get("content") or "")
            vector = cosine(query_vector, local_embedding(row.get("content") or "")) if len(query_vector) <= 512 else 0.0
            score = lexical * 0.72 + vector * 0.28
        if score > 0:
            enriched = dict(row)
            enriched["score"] = round(score, 6)
            enriched["keyword_score"] = round(lexical, 6)
            enriched["vector_score"] = round(vector, 6)
            enriched["source_type"] = "image" if chunk_type == "image_vector" else "chunk"
            scored.append((score, enriched))
    scored.sort(key=lambda item: item[0], reverse=True)
    ranked = [scored[index][1] for index in mmr_indices([row for _, row in scored], max(1, limit))]
    return _RetrievalRows(
        ranked,
        candidates=len(scored),
    )


def build_context(rows: list[dict[str, Any]], budget: int = 6000) -> str:
    parts: list[str] = []
    used = 0
    for row in rows:
        title = row.get("title") or row.get("file_name") or "文档"
        part = f"[{title}]\n{row.get('content', '')}"
        if used + len(part) > budget:
            break
        parts.append(part)
        used += len(part)
    return "\n\n".join(parts)


def context_images(rows: list[dict[str, Any]], tenant_id: int) -> list[str]:
    """Return distinct image-vector hits prepared for a vision chat model."""
    images: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if str(row.get("chunk_type") or "") != "image_vector":
            continue
        infos = loads(row.get("image_info"), [])
        if not isinstance(infos, list):
            continue
        for info in infos:
            if not isinstance(info, dict):
                continue
            url = str(info.get("url") or info.get("original_url") or "").strip()
            key = (str(row.get("knowledge_base_id") or ""), url)
            if not url or key in seen:
                continue
            seen.add(key)
            try:
                data = image_vectors._read_image(url, tenant_id)
                prepared, mime_type = image_prep.prepare(
                    data,
                    max_bytes=VISION_IMAGE_MAX_BYTES,
                    mime_types=VISION_IMAGE_MIME_TYPES,
                )
            except Exception:  # noqa: BLE001 - caption remains available
                continue
            images.append(
                f"data:{mime_type};base64,{base64.b64encode(prepared).decode('ascii')}"
            )
            if len(images) >= CONTEXT_IMAGE_LIMIT:
                return images
    return images


def subset_note(shown: int, candidates: int) -> str:
    if shown <= 0 or candidates <= shown:
        return ""
    return (
        f'<subset shown="{shown}" candidates="{candidates}">If the question asks for a '
        f"count or an exhaustive list, state that the provided context contains only the "
        f"top {shown} of {candidates} candidate passages at this filtering stage, and do "
        f"not treat these passage counts as the requested total; otherwise answer normally "
        f"without adding a disclaimer solely because of this truncation.</subset>"
    )


def _custom_metadata_text(raw: Any) -> str:
    values = loads(raw, {})
    if not isinstance(values, dict):
        return ""
    lines: list[str] = []
    for key in sorted(values):
        value = values[key]
        if value is None:
            continue
        text = str(value).strip()
        if str(key).strip() and text:
            lines.append(f"{str(key).strip()}: {text}")
    return "\n".join(lines)


def references(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        output.append(
            {
                "id": row.get("id"),
                "chunk_id": row.get("id"),
                "knowledge_id": row.get("knowledge_id"),
                "knowledge_base_id": row.get("knowledge_base_id"),
                "knowledge_title": row.get("title") or row.get("file_name") or "",
                "knowledge_filename": row.get("file_name") or "",
                "knowledge_source": row.get("knowledge_source") or "",
                "knowledge_channel": row.get("knowledge_channel") or "",
                "knowledge_description": row.get("knowledge_description") or "",
                "knowledge_custom_metadata": _custom_metadata_text(
                    row.get("knowledge_custom_metadata")
                ),
                "knowledge_base_name": row.get("knowledge_base_name") or "",
                "content": row.get("content") or "",
                "chunk_index": int(row.get("chunk_index") or 0),
                "chunk_type": row.get("chunk_type") or "",
                "source_type": row.get("source_type") or "chunk",
                "parent_chunk_id": row.get("parent_chunk_id") or "",
                "score": row.get("score", 0),
                "start_at": row.get("start_at", 0),
                "end_at": row.get("end_at", 0),
            }
        )
    return output

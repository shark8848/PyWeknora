"""Deterministic Wiki page synthesis for indexed knowledge documents."""

from __future__ import annotations

import re
from typing import Any

from .. import db
from ..utils import dumps, loads
from .wiki_write_row_guard import INGEST_ROW_IDENTITY_LOSS_LIMIT, dropped_rows_message


def _slugify(value: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip().lower()).strip("-")
    return slug or "document"


def wiki_enabled(kb: dict[str, Any]) -> bool:
    config = loads(kb.get("wiki_config"), {})
    strategy = loads(kb.get("indexing_strategy"), {})
    return bool((config or {}).get("enabled")) or bool((strategy or {}).get("wiki_enabled"))


def _page_slug(kb_id: str, knowledge: dict[str, Any]) -> str:
    base = "doc-" + _slugify(str(knowledge.get("title") or knowledge.get("file_name") or "document"))[:80]
    candidate = base
    suffix = 2
    while True:
        row = db.fetch_one(
            "SELECT id, page_metadata FROM wiki_pages WHERE knowledge_base_id=? AND slug=? AND deleted_at IS NULL",
            (kb_id, candidate),
        )
        if not row:
            return candidate
        metadata = loads(row.get("page_metadata"), {})
        if isinstance(metadata, dict) and metadata.get("knowledge_id") == knowledge.get("id"):
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def _snapshot(page: dict[str, Any]) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_page_revisions
            (id, tenant_id, knowledge_base_id, page_id, slug, version, title,
             page_type, status, content, summary, aliases, edit_source,
             editor_id, edited_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ingest', '', ?, ?)
            """,
            (
                db.new_id(), page["tenant_id"], page["knowledge_base_id"], page["id"],
                page["slug"], page["version"], page.get("title") or "",
                page.get("page_type") or "summary", page.get("status") or "published",
                page.get("content") or "", page.get("summary") or "",
                page.get("aliases") or "[]", db.utcnow(), db.utcnow(),
            ),
        )


def ingest_knowledge(kb: dict[str, Any], knowledge: dict[str, Any]) -> dict[str, Any] | None:
    if not wiki_enabled(kb) or knowledge.get("deleted_at") or knowledge.get("parse_status") not in {"completed", "parsed", "indexed"}:
        return None
    chunks = db.fetch_all(
        "SELECT id, content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index LIMIT 50",
        (knowledge["id"],),
    )
    if not chunks:
        return None
    text = "\n\n".join(str(chunk.get("content") or "") for chunk in chunks).strip()
    if not text:
        return None
    summary = re.sub(r"\s+", " ", text)[:360]
    title = str(knowledge.get("title") or knowledge.get("file_name") or "Document")
    content = f"# {title}\n\n{summary}\n\n## Source\n\n- {title}\n"
    metadata = {"knowledge_id": knowledge["id"], "auto_generated": True}
    source_refs = [{"knowledge_id": knowledge["id"], "title": title}]
    chunk_refs = [chunk["id"] for chunk in chunks]
    slug = _page_slug(str(kb["id"]), knowledge)
    existing = db.fetch_one(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug=? AND deleted_at IS NULL",
        (kb["id"], slug),
    )
    now = db.utcnow()
    if existing:
        if (
            str(existing.get("title") or "") == title
            and str(existing.get("content") or "") == content
        ):
            return existing
        refusal = dropped_rows_message(
            str(existing.get("content") or ""),
            content,
            slug,
            loss_limit=INGEST_ROW_IDENTITY_LOSS_LIMIT,
        )
        if refusal:
            return None
        _snapshot(existing)
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE wiki_pages SET title=?, content=?, summary=?, source_refs=?,
                  chunk_refs=?, page_metadata=?, version=version+1,
                  last_edit_source='ingest', updated_at=?
                WHERE id=?
                """,
                (title, content, summary, dumps(source_refs), dumps(chunk_refs),
                 dumps(metadata), now, existing["id"]),
            )
        return db.fetch_one("SELECT * FROM wiki_pages WHERE id=?", (existing["id"],))
    page_id = db.new_id()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages
            (id, tenant_id, knowledge_base_id, slug, title, page_type, status,
             content, summary, parent_slug, folder_id, category_path, wiki_path,
             depth, sort_order, source_refs, chunk_refs, in_links, out_links,
             page_metadata, aliases, version, last_edit_source, last_editor_id,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'summary', 'published', ?, ?, '', '', '[]',
                    ?, 0, 0, ?, ?, '[]', '[]', ?, '[]', 1, 'ingest', '', ?, ?)
            """,
            (
                page_id, int(kb["tenant_id"]), kb["id"], slug, title, content,
                summary, slug, dumps(source_refs), dumps(chunk_refs), dumps(metadata),
                now, now,
            ),
        )
    return db.fetch_one("SELECT * FROM wiki_pages WHERE id=?", (page_id,))


def ingest_all(kb: dict[str, Any]) -> int:
    rows = db.fetch_all(
        "SELECT * FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
        (kb["id"],),
    )
    count = 0
    for row in rows:
        if ingest_knowledge(kb, row):
            count += 1
    return count


def remove_knowledge_source(
    kb_id: str,
    tenant_id: int,
    knowledge_id: str,
    chunk_ids: list[str] | tuple[str, ...] = (),
) -> int:
    """Remove one moved document from the source KB's Wiki pages."""
    pages = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE tenant_id=? AND knowledge_base_id=? AND deleted_at IS NULL",
        (tenant_id, kb_id),
    )
    removed_chunks = {str(value) for value in chunk_ids if value}
    affected = 0
    now = db.utcnow()
    for page in pages:
        source_refs = loads(page.get("source_refs"), [])
        source_refs = source_refs if isinstance(source_refs, list) else []
        metadata = loads(page.get("page_metadata"), {})
        metadata = metadata if isinstance(metadata, dict) else {}
        metadata_match = str(metadata.get("knowledge_id") or "") == knowledge_id
        ref_match = any(
            isinstance(ref, dict) and str(ref.get("knowledge_id") or "") == knowledge_id
            for ref in source_refs
        )
        if not metadata_match and not ref_match:
            continue
        remaining_refs = [
            ref
            for ref in source_refs
            if not (isinstance(ref, dict) and str(ref.get("knowledge_id") or "") == knowledge_id)
        ]
        if not remaining_refs:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE wiki_pages SET deleted_at=?, updated_at=? WHERE id=?",
                    (now, now, page["id"]),
                )
        else:
            chunk_refs = loads(page.get("chunk_refs"), [])
            chunk_refs = chunk_refs if isinstance(chunk_refs, list) else []
            remaining_chunks = [
                value for value in chunk_refs if str(value) not in removed_chunks
            ]
            if metadata_match:
                metadata.pop("knowledge_id", None)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE wiki_pages SET source_refs=?, chunk_refs=?, page_metadata=?, "
                    "version=version+1, updated_at=? WHERE id=?",
                    (dumps(remaining_refs), dumps(remaining_chunks), dumps(metadata), now, page["id"]),
                )
        affected += 1
    return affected

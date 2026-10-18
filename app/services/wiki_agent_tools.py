"""Wiki tools exposed to agent chat."""

from __future__ import annotations

import json
import re
from typing import Any

from .. import db
from ..utils import dumps, loads
from .access import knowledge_base_share_permission, readable_knowledge_base
from .wiki_ingest import wiki_enabled
from .wiki_write_row_guard import dropped_rows_message


TOOL_NAMES = {
    "wiki_search",
    "wiki_read_page",
    "wiki_write_page",
    "wiki_replace_text",
    "wiki_rename_page",
    "wiki_delete_page",
    "wiki_flag_issue",
    "wiki_read_issue",
    "wiki_update_issue",
}

_ISSUE_TYPES = {"mixed_entities", "contradictory_facts", "out_of_date", "other"}
_ISSUE_STATUSES = {"resolved", "ignored", "pending"}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
        "service_id": "",
        "tool_name": name,
        "require_approval": False,
    }


def _definitions() -> dict[str, dict[str, Any]]:
    return {
        "wiki_search": _tool(
            "wiki_search",
            "Search the Wiki for pages matching a query and return ranked page summaries.",
            {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["query"],
        ),
        "wiki_read_page": _tool(
            "wiki_read_page",
            "Read a Wiki page by slug. Use slug 'index' for a compact Wiki overview.",
            {"slug": {"type": "string"}},
            ["slug"],
        ),
        "wiki_write_page": _tool(
            "wiki_write_page",
            "Create a Wiki page or completely overwrite an existing one.",
            {
                "slug": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "content": {"type": "string"},
                "page_type": {"type": "string"},
                "aliases": {"type": "array", "items": {"type": "string"}},
                "source_refs": {"type": "array", "items": {"type": "string"}},
            },
            ["slug", "title", "summary", "content", "page_type"],
        ),
        "wiki_replace_text": _tool(
            "wiki_replace_text",
            "Replace all exact occurrences of text in a Wiki page.",
            {
                "slug": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "source_refs": {"type": "array", "items": {"type": "string"}},
            },
            ["slug", "old_text", "new_text"],
        ),
        "wiki_rename_page": _tool(
            "wiki_rename_page",
            "Rename a Wiki page slug and update links that point to it.",
            {
                "slug": {"type": "string"},
                "new_slug": {"type": "string"},
            },
            ["slug", "new_slug"],
        ),
        "wiki_delete_page": _tool(
            "wiki_delete_page",
            "Delete a Wiki page and replace incoming links with plain text.",
            {"slug": {"type": "string"}},
            ["slug"],
        ),
        "wiki_flag_issue": _tool(
            "wiki_flag_issue",
            "Flag a Wiki page for factual errors, mixed entities, stale content, or other repair.",
            {
                "slug": {"type": "string"},
                "issue_type": {"type": "string", "enum": sorted(_ISSUE_TYPES)},
                "description": {"type": "string"},
                "suspected_knowledge_ids": {"type": "array", "items": {"type": "string"}},
            },
            ["slug", "issue_type", "description"],
        ),
        "wiki_read_issue": _tool(
            "wiki_read_issue",
            "Read one Wiki issue or list pending issues for a page slug.",
            {
                "issue_id": {"type": "string"},
                "slug": {"type": "string"},
            },
            [],
        ),
        "wiki_update_issue": _tool(
            "wiki_update_issue",
            "Update a Wiki issue status.",
            {
                "issue_id": {"type": "string"},
                "status": {"type": "string", "enum": sorted(_ISSUE_STATUSES)},
            },
            ["issue_id", "status"],
        ),
    }


_DEFINITIONS = _definitions()


def builtin_tools(
    tenant_id: int, allowed_tools: list[str], knowledge_base_ids: list[str]
) -> list[dict[str, Any]]:
    names = [name for name in _DEFINITIONS if name in set(allowed_tools or [])]
    if not names or not _candidate_kbs(tenant_id, "", knowledge_base_ids, write=False):
        return []
    return [_DEFINITIONS[name] for name in names]


def _tenant_role(tenant_id: int, user_id: str) -> str:
    if not user_id:
        return ""
    row = db.fetch_one(
        "SELECT role FROM tenant_members WHERE tenant_id=? AND user_id=? "
        "AND status='active' AND deleted_at IS NULL",
        (tenant_id, user_id),
    )
    return str((row or {}).get("role") or "")


def _can_write(kb: dict[str, Any], tenant_id: int, user_id: str, role: str) -> bool:
    if int(kb.get("tenant_id") or 0) == tenant_id:
        return bool(user_id) and (
            str(kb.get("creator_id") or "") == user_id or role in {"admin", "owner"}
        )
    return knowledge_base_share_permission(tenant_id, str(kb["id"]), role) in {"editor", "admin"}


def _candidate_kbs(
    tenant_id: int,
    user_id: str,
    knowledge_base_ids: list[str] | None,
    *,
    write: bool,
    role: str | None = None,
) -> list[dict[str, Any]]:
    if tenant_id <= 0:
        return []
    caller_role = role if role is not None else _tenant_role(tenant_id, user_id)
    result: list[dict[str, Any]] = []
    for kb_id in dict.fromkeys(str(value) for value in knowledge_base_ids or [] if str(value)):
        kb = readable_knowledge_base(tenant_id, kb_id, caller_role)
        if not kb or not wiki_enabled(kb):
            continue
        if write and not _can_write(kb, tenant_id, user_id, caller_role):
            continue
        result.append(kb)
    return result


def _normalize_slug(raw: str) -> str:
    value = str(raw or "").strip().lower().replace(" ", "-")
    if not value:
        raise ValueError("slug is required and must be non-empty")
    if "//" in value or value.startswith("/") or value.endswith("/"):
        raise ValueError(f"invalid slug {raw!r}: '/' separators are malformed")
    for char in value:
        if char.isascii() and (char.islower() or char.isdigit() or char in {"-", "/"}):
            continue
        if "\u4e00" <= char <= "\u9fff":
            continue
        raise ValueError(
            f"invalid slug {raw!r}: character {char!r} is not allowed "
            "(use lowercase letters, digits, '-', '/', or CJK)"
        )
    return value


def _page_rows(kbs: list[dict[str, Any]], slug: str) -> list[dict[str, Any]]:
    ids = [str(kb["id"]) for kb in kbs]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return db.fetch_all(
        f"SELECT * FROM wiki_pages WHERE slug=? AND deleted_at IS NULL "
        f"AND knowledge_base_id IN ({marks}) ORDER BY updated_at DESC",
        (slug, *ids),
    )


def _resolve_page(
    kbs: list[dict[str, Any]], raw_slug: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    slug = _normalize_slug(raw_slug)
    rows = _page_rows(kbs, slug)
    if len(rows) > 1:
        raise ValueError(f"slug {slug!r} exists in more than one knowledge base")
    if not rows:
        return None, None, slug
    page = rows[0]
    kb = next(kb for kb in kbs if str(kb["id"]) == str(page["knowledge_base_id"]))
    return page, kb, slug


def _resolve_sources(
    kbs: list[dict[str, Any]], values: Any
) -> tuple[list[dict[str, str]], set[str]]:
    if not isinstance(values, list):
        raise ValueError("source_refs must be an array")
    ids = [str(kb["id"]) for kb in kbs]
    if not ids:
        raise ValueError("no knowledge bases are available")
    marks = ",".join("?" for _ in ids)
    refs: list[dict[str, str]] = []
    kb_ids: set[str] = set()
    for value in values:
        raw = str(value or "").split("|", 1)[0].strip()
        if not raw:
            continue
        row = db.fetch_one(
            f"SELECT id, title, file_name, knowledge_base_id FROM knowledges "
            f"WHERE id=? AND deleted_at IS NULL AND knowledge_base_id IN ({marks})",
            (raw, *ids),
        )
        if not row:
            raise ValueError(f"source document {raw!r} is not available in scope")
        title = str(row.get("title") or row.get("file_name") or row["id"])
        refs.append({"knowledge_id": str(row["id"]), "title": title})
        kb_ids.add(str(row["knowledge_base_id"]))
    return refs, kb_ids


def _snapshot(page: dict[str, Any], user_id: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_page_revisions
            (id, tenant_id, knowledge_base_id, page_id, slug, version, title,
             page_type, status, content, summary, aliases, edit_source,
             editor_id, edited_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'agent', ?, ?, ?)
            """,
            (
                db.new_id(), page["tenant_id"], page["knowledge_base_id"], page["id"],
                page["slug"], page["version"], page.get("title") or "",
                page.get("page_type") or "summary", page.get("status") or "published",
                page.get("content") or "", page.get("summary") or "",
                page.get("aliases") or "[]", user_id, db.utcnow(), db.utcnow(),
            ),
        )


_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def _links_for(content: str, pages: list[dict[str, Any]]) -> list[str]:
    lookup: dict[str, str] = {}
    for page in pages:
        lookup[str(page["slug"]).lower()] = str(page["slug"])
        lookup[str(page.get("title") or "").lower()] = str(page["slug"])
    result: list[str] = []
    for match in _LINK_RE.finditer(content or ""):
        target = lookup.get(match.group(1).strip().lower())
        if target and target not in result:
            result.append(target)
    return result


def _rebuild_links(kb_id: str, tenant_id: int) -> None:
    pages = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, tenant_id),
    )
    inbound: dict[str, list[str]] = {str(page["slug"]): [] for page in pages}
    outgoing: dict[str, list[str]] = {}
    for page in pages:
        links = _links_for(str(page.get("content") or ""), pages)
        outgoing[str(page["id"])] = links
        for target in links:
            inbound.setdefault(target, []).append(str(page["slug"]))
    with db.transaction() as conn:
        for slug, sources in inbound.items():
            conn.execute(
                "UPDATE wiki_pages SET in_links=? WHERE knowledge_base_id=? AND slug=? AND deleted_at IS NULL",
                (dumps(sorted(set(sources))), kb_id, slug),
            )
        for page in pages:
            links = outgoing[str(page["id"])]
            conn.execute(
                "UPDATE wiki_pages SET out_links=?, updated_at=? WHERE id=?",
                (dumps(links), db.utcnow(), page["id"]),
            )


def _result(success: bool, output: str, raw: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"success": success, "output": output}
    if raw is not None:
        result["raw"] = raw
    return result


def _json_result(data: Any) -> dict[str, Any]:
    return _result(True, json.dumps(data, ensure_ascii=False, indent=2), data)


def _search(kbs: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return _result(False, "query is required")
    try:
        limit = min(50, max(1, int(args.get("limit") or 10)))
    except (TypeError, ValueError):
        limit = 10
    ids = [str(kb["id"]) for kb in kbs]
    marks = ",".join("?" for _ in ids)
    pattern = f"%{query.lower()}%"
    rows = db.fetch_all(
        f"SELECT * FROM wiki_pages WHERE knowledge_base_id IN ({marks}) "
        "AND deleted_at IS NULL AND (lower(title) LIKE ? OR lower(summary) LIKE ? "
        "OR lower(content) LIKE ?) ORDER BY updated_at DESC LIMIT ?",
        (*ids, pattern, pattern, pattern, limit),
    )
    results = []
    for row in rows:
        content = str(row.get("content") or "")
        position = content.lower().find(query.lower())
        start = max(0, position - 120) if position >= 0 else 0
        snippet = re.sub(r"\s+", " ", content[start:start + 360]).strip()
        results.append({
            "slug": row["slug"],
            "title": row.get("title") or "",
            "page_type": row.get("page_type") or "",
            "summary": row.get("summary") or "",
            "knowledge_base_id": row["knowledge_base_id"],
            "snippet": snippet,
        })
    return _json_result({"query": query, "count": len(results), "results": results})


def _read_page(kbs: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    try:
        page, _kb, slug = _resolve_page(kbs, str(args.get("slug") or ""))
    except ValueError as exc:
        return _result(False, str(exc))
    if page is None:
        if slug in {"index", "wiki-index"}:
            ids = [str(kb["id"]) for kb in kbs]
            marks = ",".join("?" for _ in ids)
            rows = db.fetch_all(
                f"SELECT slug, title, page_type, summary, knowledge_base_id FROM wiki_pages "
                f"WHERE knowledge_base_id IN ({marks}) AND deleted_at IS NULL "
                "ORDER BY page_type, sort_order, title LIMIT 200",
                tuple(ids),
            )
            return _json_result({"slug": "index", "pages": rows, "total": len(rows)})
        return _result(False, f"Wiki page {slug!r} was not found")
    data = dict(page)
    for key in ("source_refs", "aliases", "in_links", "out_links", "category_path"):
        data[key] = loads(data.get(key), [])
    data["page_metadata"] = loads(data.get("page_metadata"), {})
    return _json_result(data)


def _write_page(
    kbs: list[dict[str, Any]], args: dict[str, Any], *, user_id: str
) -> dict[str, Any]:
    required = ("title", "summary", "content", "page_type")
    if any(not str(args.get(key) or "").strip() for key in required):
        return _result(False, "title, summary, content, and page_type are required")
    try:
        existing, existing_kb, slug = _resolve_page(kbs, str(args.get("slug") or ""))
    except ValueError as exc:
        return _result(False, str(exc))
    refs: list[dict[str, str]] | None = None
    ref_kbs: set[str] = set()
    if args.get("source_refs") is not None:
        try:
            refs, ref_kbs = _resolve_sources(kbs, args.get("source_refs"))
        except ValueError as exc:
            return _result(False, f"Invalid source_refs: {exc}")
    target_kb = existing_kb
    if target_kb is None:
        if len(ref_kbs) == 1:
            target_kb = next(kb for kb in kbs if str(kb["id"]) in ref_kbs)
        elif len(kbs) == 1:
            target_kb = kbs[0]
        else:
            return _result(False, "Cannot determine the target Wiki knowledge base; provide source_refs")
    page_type = str(args.get("page_type") or "").strip()
    if existing is None and (slug.startswith("summary/") or page_type.lower() == "summary"):
        return _result(
            False,
            "summary pages are generated automatically from source documents and cannot be created manually",
        )
    aliases_value = args.get("aliases")
    if aliases_value is not None and not isinstance(aliases_value, list):
        return _result(False, "aliases must be an array")
    title = str(args.get("title") or "").strip()
    summary = str(args.get("summary") or "").strip()
    content = str(args.get("content") or "")
    owner_tenant = int(target_kb.get("tenant_id") or 0)
    now = db.utcnow()
    if existing is not None:
        refusal = dropped_rows_message(
            str(existing.get("content") or ""),
            content,
            slug,
            loss_limit=0,
        )
        if refusal:
            return _result(False, refusal)
        _snapshot(existing, user_id)
        updates: dict[str, Any] = {
            "title": title,
            "summary": summary,
            "content": content,
            "page_type": page_type,
            "version": int(existing.get("version") or 1) + 1,
            "last_edit_source": "agent",
            "last_editor_id": user_id,
            "updated_at": now,
        }
        if aliases_value is not None:
            updates["aliases"] = dumps([str(value) for value in aliases_value])
        if refs is not None:
            updates["source_refs"] = dumps(refs)
        with db.transaction() as conn:
            db.update("wiki_pages", updates, "id=?", (existing["id"],), conn)
        action = "updated"
        page_id = str(existing["id"])
    else:
        page_id = db.new_id()
        depth = slug.count("/")
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO wiki_pages
                (id, tenant_id, knowledge_base_id, slug, title, page_type, status,
                 content, summary, parent_slug, folder_id, category_path, wiki_path,
                 depth, sort_order, source_refs, chunk_refs, in_links, out_links,
                 page_metadata, aliases, version, last_edit_source, last_editor_id,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?, '', '', '[]', ?, ?, 0,
                        ?, '[]', '[]', '[]', '{}', ?, 1, 'agent', ?, ?, ?)
                """,
                (
                    page_id, owner_tenant, target_kb["id"], slug, title, page_type,
                    content, summary, slug, depth, dumps(refs or []),
                    dumps([str(value) for value in aliases_value] if isinstance(aliases_value, list) else []),
                    user_id, now, now,
                ),
            )
        action = "created"
    _rebuild_links(str(target_kb["id"]), owner_tenant)
    raw = {
        "display_type": "wiki_write_page",
        "action": action,
        "slug": slug,
        "title": title,
        "page_type": page_type,
        "summary": summary,
        "knowledge_base_id": target_kb["id"],
    }
    output = (
        f"Successfully {action} page [[{slug}]].\n"
        f"- Title: {title}\n- Type: {page_type}\n- Summary: {summary}\n"
        f"- Content length: {len(content)} chars"
    )
    if refs is not None:
        output += f"\n- Source refs: {len(refs)} document(s)"
    return _result(True, output, raw)


def _replace_text(
    kbs: list[dict[str, Any]], args: dict[str, Any], *, user_id: str
) -> dict[str, Any]:
    old_text = str(args.get("old_text") or "")
    if not old_text:
        return _result(False, "old_text is required")
    try:
        page, kb, slug = _resolve_page(kbs, str(args.get("slug") or ""))
    except ValueError as exc:
        return _result(False, str(exc))
    if page is None or kb is None:
        return _result(False, f"Wiki page {slug!r} was not found")
    content = str(page.get("content") or "")
    count = content.count(old_text)
    if count == 0:
        return _result(False, "old_text not found in the current page content")
    updates: dict[str, Any] = {
        "content": content.replace(old_text, str(args.get("new_text") or "")),
        "version": int(page.get("version") or 1) + 1,
        "last_edit_source": "agent",
        "last_editor_id": user_id,
        "updated_at": db.utcnow(),
    }
    if args.get("source_refs") is not None:
        try:
            refs, _ = _resolve_sources(kbs, args.get("source_refs"))
        except ValueError as exc:
            return _result(False, f"Invalid source_refs: {exc}")
        updates["source_refs"] = dumps(refs)
    _snapshot(page, user_id)
    with db.transaction() as conn:
        db.update("wiki_pages", updates, "id=?", (page["id"],), conn)
    _rebuild_links(str(kb["id"]), int(kb.get("tenant_id") or 0))
    return _result(
        True,
        f"Successfully replaced {count} occurrence(s) on page [[{slug}]].",
        {"display_type": "wiki_replace_text", "slug": slug, "replacement_count": count},
    )


def _rename_page(
    kbs: list[dict[str, Any]], args: dict[str, Any], *, user_id: str
) -> dict[str, Any]:
    try:
        page, kb, slug = _resolve_page(kbs, str(args.get("slug") or ""))
        new_slug = _normalize_slug(str(args.get("new_slug") or ""))
    except ValueError as exc:
        return _result(False, str(exc))
    if page is None or kb is None:
        return _result(False, f"Wiki page {slug!r} was not found")
    if new_slug == slug:
        return _result(False, "new_slug must be different from old slug")
    if _page_rows(kbs, new_slug):
        return _result(False, f"Wiki page {new_slug!r} already exists")
    _snapshot(page, user_id)
    owner_tenant = int(kb.get("tenant_id") or 0)
    updated_pages: list[str] = []
    with db.transaction() as conn:
        wiki_path = str(page.get("wiki_path") or "")
        conn.execute(
            "UPDATE wiki_pages SET slug=?, wiki_path=?, version=?, last_edit_source='agent', "
            "last_editor_id=?, updated_at=? WHERE id=?",
            (
                new_slug, new_slug if wiki_path in {"", slug} else wiki_path,
                int(page.get("version") or 1) + 1, user_id, db.utcnow(), page["id"],
            ),
        )
        siblings = conn.execute(
            "SELECT id, slug, content FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? "
            "AND deleted_at IS NULL AND id<>?",
            (kb["id"], owner_tenant, page["id"]),
        ).fetchall()
        for sibling in siblings:
            content = str(sibling["content"] or "")
            updated = content.replace(f"[[{slug}]]", f"[[{new_slug}]]")
            updated = updated.replace(f"[[{slug}|", f"[[{new_slug}|")
            if updated != content:
                conn.execute(
                    "UPDATE wiki_pages SET content=?, updated_at=? WHERE id=?",
                    (updated, db.utcnow(), sibling["id"]),
                )
                updated_pages.append(str(sibling["slug"]))
    _rebuild_links(str(kb["id"]), owner_tenant)
    return _result(
        True,
        f"Successfully renamed page [[{slug}]] to [[{new_slug}]] and updated "
        f"{len(updated_pages)} incoming link page(s).",
        {
            "display_type": "wiki_rename_page",
            "old_slug": slug,
            "new_slug": new_slug,
            "updated_count": len(updated_pages),
            "affected_pages": updated_pages,
        },
    )


def _delete_page(kbs: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    try:
        page, kb, slug = _resolve_page(kbs, str(args.get("slug") or ""))
    except ValueError as exc:
        return _result(False, str(exc))
    if page is None or kb is None:
        return _result(False, f"Wiki page {slug!r} was not found")
    owner_tenant = int(kb.get("tenant_id") or 0)
    incoming = loads(page.get("in_links"), [])
    incoming = incoming if isinstance(incoming, list) else []
    display = slug.rsplit("/", 1)[-1].replace("-", " ")
    pipe = re.compile(r"\[\[" + re.escape(slug) + r"\|([^\]]+)\]\]", re.IGNORECASE)
    updated_pages: list[str] = []
    now = db.utcnow()
    with db.transaction() as conn:
        for source_slug in dict.fromkeys(str(value) for value in incoming if str(value)):
            source = conn.execute(
                "SELECT id, slug, content FROM wiki_pages WHERE knowledge_base_id=? "
                "AND tenant_id=? AND slug=? AND deleted_at IS NULL",
                (kb["id"], owner_tenant, source_slug),
            ).fetchone()
            if source is None:
                continue
            content = str(source["content"] or "")
            updated = content.replace(f"[[{slug}]]", display)
            updated = pipe.sub(r"\1", updated)
            if updated != content:
                conn.execute(
                    "UPDATE wiki_pages SET content=?, updated_at=? WHERE id=?",
                    (updated, now, source["id"]),
                )
                updated_pages.append(source_slug)
        conn.execute(
            "UPDATE wiki_pages SET deleted_at=?, updated_at=? WHERE id=?",
            (now, now, page["id"]),
        )
    _rebuild_links(str(kb["id"]), owner_tenant)
    return _result(
        True,
        f"Successfully deleted page [[{slug}]] and cleaned up {len(updated_pages)} incoming link page(s).",
        {
            "display_type": "wiki_delete_page",
            "slug": slug,
            "title": page.get("title") or "",
            "updated_count": len(updated_pages),
            "affected_pages": updated_pages,
        },
    )


def _issue_json(row: dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    data["suspected_knowledge_ids"] = loads(data.get("suspected_knowledge_ids"), [])
    return data


def _resolve_issue(
    kbs: list[dict[str, Any]], raw_ref: str
) -> dict[str, Any] | None:
    ref = str(raw_ref or "").strip()
    if not ref:
        raise ValueError("issue_id is required")
    ids = [str(kb["id"]) for kb in kbs]
    marks = ",".join("?" for _ in ids)
    row = db.fetch_one(
        f"SELECT * FROM wiki_page_issues WHERE id=? AND deleted_at IS NULL "
        f"AND knowledge_base_id IN ({marks})",
        (ref, *ids),
    )
    if row:
        return row
    matches = db.fetch_all(
        f"SELECT * FROM wiki_page_issues WHERE id LIKE ? AND deleted_at IS NULL "
        f"AND knowledge_base_id IN ({marks}) ORDER BY created_at LIMIT 2",
        (ref + "%", *ids),
    )
    if len(matches) > 1:
        raise ValueError(f"issue reference {ref!r} is ambiguous")
    return matches[0] if matches else None


def _flag_issue(
    kbs: list[dict[str, Any]], args: dict[str, Any], *, user_id: str
) -> dict[str, Any]:
    issue_type = str(args.get("issue_type") or "").strip()
    description = str(args.get("description") or "").strip()
    if issue_type not in _ISSUE_TYPES:
        return _result(False, f"issue_type is invalid; expected one of {sorted(_ISSUE_TYPES)}")
    if not description:
        return _result(False, "description is required")
    try:
        page, kb, slug = _resolve_page(kbs, str(args.get("slug") or ""))
    except ValueError as exc:
        return _result(False, str(exc))
    if page is None or kb is None:
        return _result(False, f"Wiki page {slug!r} was not found")
    suspected = args.get("suspected_knowledge_ids")
    if suspected is not None:
        try:
            refs, _ = _resolve_sources(kbs, suspected)
        except ValueError as exc:
            return _result(False, f"Invalid suspected_knowledge_ids: {exc}")
        suspected_ids = [ref["knowledge_id"] for ref in refs]
    else:
        suspected_ids = []
    issue_id = db.new_id()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO wiki_page_issues
            (id, tenant_id, knowledge_base_id, slug, issue_type, description,
             suspected_knowledge_ids, status, reported_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                issue_id, int(kb.get("tenant_id") or 0), kb["id"], slug, issue_type,
                description, dumps(suspected_ids), user_id or "agent", now, now,
            ),
        )
    return _result(True, f"Successfully flagged issue {issue_id} for {slug}.")


def _read_issue(kbs: list[dict[str, Any]], args: dict[str, Any]) -> dict[str, Any]:
    issue_ref = str(args.get("issue_id") or "").strip()
    slug = str(args.get("slug") or "").strip()
    if not issue_ref and not slug:
        return _result(False, "Either issue_id or slug is required")
    if issue_ref:
        try:
            row = _resolve_issue(kbs, issue_ref)
        except ValueError as exc:
            return _result(False, str(exc))
        if not row:
            return _result(False, f"Wiki issue {issue_ref!r} was not found")
        return _json_result(_issue_json(row))
    try:
        slug = _normalize_slug(slug)
    except ValueError as exc:
        return _result(False, str(exc))
    ids = [str(kb["id"]) for kb in kbs]
    marks = ",".join("?" for _ in ids)
    rows = db.fetch_all(
        f"SELECT * FROM wiki_page_issues WHERE knowledge_base_id IN ({marks}) "
        "AND slug=? AND status='pending' AND deleted_at IS NULL ORDER BY created_at DESC",
        (*ids, slug),
    )
    return _json_result({"slug": slug, "issues": [_issue_json(row) for row in rows]})


def _update_issue(
    kbs: list[dict[str, Any]], args: dict[str, Any]
) -> dict[str, Any]:
    status = str(args.get("status") or "").strip()
    if status not in _ISSUE_STATUSES:
        return _result(False, f"status is invalid; expected one of {sorted(_ISSUE_STATUSES)}")
    try:
        row = _resolve_issue(kbs, str(args.get("issue_id") or ""))
    except ValueError as exc:
        return _result(False, str(exc))
    if not row:
        return _result(False, "Wiki issue was not found")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE wiki_page_issues SET status=?, updated_at=? WHERE id=?",
            (status, db.utcnow(), row["id"]),
        )
    return _result(True, f"Successfully updated issue {row['id']} to status {status!r}.")


def execute(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    tenant_id: int,
    user_id: str,
    knowledge_base_ids: list[str] | None,
) -> dict[str, Any]:
    if tool_name not in TOOL_NAMES:
        return _result(False, f"Unknown Wiki tool: {tool_name}")
    write = tool_name in {
        "wiki_write_page",
        "wiki_replace_text",
        "wiki_rename_page",
        "wiki_delete_page",
        "wiki_flag_issue",
        "wiki_update_issue",
    }
    kbs = _candidate_kbs(tenant_id, user_id, knowledge_base_ids, write=write)
    if not kbs:
        scope = "editable" if write else "readable"
        return _result(False, f"No {scope} Wiki knowledge bases are available")
    try:
        if tool_name == "wiki_search":
            return _search(kbs, arguments)
        if tool_name == "wiki_read_page":
            return _read_page(kbs, arguments)
        if tool_name == "wiki_write_page":
            return _write_page(kbs, arguments, user_id=user_id)
        if tool_name == "wiki_replace_text":
            return _replace_text(kbs, arguments, user_id=user_id)
        if tool_name == "wiki_rename_page":
            return _rename_page(kbs, arguments, user_id=user_id)
        if tool_name == "wiki_delete_page":
            return _delete_page(kbs, arguments)
        if tool_name == "wiki_flag_issue":
            return _flag_issue(kbs, arguments, user_id=user_id)
        if tool_name == "wiki_read_issue":
            return _read_issue(kbs, arguments)
        if tool_name == "wiki_update_issue":
            return _update_issue(kbs, arguments)
    except (KeyError, TypeError, ValueError) as exc:
        return _result(False, str(exc))
    return _result(False, f"Unknown Wiki tool: {tool_name}")

"""Wiki pages, folders, links, revisions and health reporting."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request

from .. import db
from ..api_keys import authorize_knowledge_bases
from ..deps import AuthContext, parse_auth, require_role
from ..errors import AppError, conflict, not_found, validation
from ..services.access import readable_knowledge_base
from ..utils import dumps, loads, ok, pagination


router = APIRouter(tags=["wiki"])

_JSON_FIELDS = ("category_path", "source_refs", "in_links", "out_links", "page_metadata", "aliases")
_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _kb(ctx: AuthContext, kb_id: str, *, write: bool = False) -> dict[str, Any]:
    if ctx.api_key is not None:
        authorize_knowledge_bases(ctx.api_key, {kb_id})
    row = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND deleted_at IS NULL",
        (kb_id,),
    )
    if not row:
        raise not_found("Knowledge base not found")
    if int(row.get("tenant_id") or 0) == ctx.tenant_id:
        if write and str(row.get("creator_id") or "") != ctx.user_id:
            require_role(ctx, "admin")
        return row
    if not write and readable_knowledge_base(ctx.tenant_id, kb_id, ctx.role):
        return row
    raise not_found("Knowledge base not found")
    return row


def _slugify(value: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip().lower()).strip("-")
    return slug or "page"


def _unique_slug(kb_id: str, base: str, current_id: str = "") -> str:
    candidate = base
    suffix = 2
    while True:
        row = db.fetch_one(
            "SELECT id FROM wiki_pages WHERE knowledge_base_id=? AND slug=? AND deleted_at IS NULL",
            (kb_id, candidate),
        )
        if not row or row["id"] == current_id:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


def _page(kb_id: str, slug: str, tenant_id: int) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, slug, tenant_id),
    )
    if not row:
        raise not_found("Wiki page not found")
    return row


def _serialize(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    result = dict(row)
    for key in _JSON_FIELDS:
        result[key] = loads(result.get(key), [] if key != "page_metadata" else {})
    result["version"] = int(result.get("version") or 1)
    for key in ("depth", "sort_order"):
        result[key] = int(result.get(key) or 0)
    if detail:
        titles: dict[str, str] = {}
        for slug in result.get("in_links") or []:
            linked = db.fetch_one(
                "SELECT title FROM wiki_pages WHERE knowledge_base_id=? AND slug=? AND deleted_at IS NULL",
                (result["knowledge_base_id"], slug),
            )
            if linked:
                titles[str(slug)] = linked["title"]
        result["in_link_titles"] = titles
    return result


def _folder_json(row: dict[str, Any], page_count: int = 0, has_children: bool = False) -> dict[str, Any]:
    result = dict(row)
    for key in ("depth", "sort_order"):
        result[key] = int(result.get(key) or 0)
    result["page_count"] = page_count
    result["has_children"] = has_children
    return result


def _folder(kb_id: str, folder_id: str, tenant_id: int) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM wiki_folders WHERE id=? AND knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (folder_id, kb_id, tenant_id),
    )
    if not row:
        raise not_found("Wiki folder not found")
    return row


def _snapshot(page: dict[str, Any], *, source: str = "user", editor_id: str = "") -> None:
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_page_revisions
            (id, tenant_id, knowledge_base_id, page_id, slug, version, title,
             page_type, status, content, summary, aliases, edit_source,
             editor_id, edited_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                db.new_id(), page["tenant_id"], page["knowledge_base_id"], page["id"],
                page["slug"], page["version"], page.get("title") or "",
                page.get("page_type") or "summary", page.get("status") or "published",
                page.get("content") or "", page.get("summary") or "",
                page.get("aliases") or "[]", source, editor_id,
                db.utcnow(), db.utcnow(),
            ),
        )


def _links_for(content: str, pages: list[dict[str, Any]]) -> list[str]:
    by_key: dict[str, str] = {}
    for page in pages:
        by_key[page["slug"].lower()] = page["slug"]
        by_key[(page.get("title") or "").lower()] = page["slug"]
    result: list[str] = []
    for match in _LINK_RE.finditer(content or ""):
        target = by_key.get(match.group(1).strip().lower())
        if target and target not in result:
            result.append(target)
    return result


def _rebuild_links(kb_id: str, tenant_id: int) -> int:
    pages = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, tenant_id),
    )
    inbound: dict[str, list[str]] = {page["slug"]: [] for page in pages}
    updates: list[tuple[str, str, str]] = []
    for page in pages:
        outgoing = _links_for(page.get("content") or "", pages)
        updates.append((dumps(outgoing), dumps(inbound.setdefault(page["slug"], [])), page["id"]))
        for target in outgoing:
            inbound.setdefault(target, []).append(page["slug"])
    with db.transaction() as conn:
        for slug, sources in inbound.items():
            conn.execute(
                "UPDATE wiki_pages SET in_links=? WHERE knowledge_base_id=? AND slug=? AND deleted_at IS NULL",
                (dumps(sorted(set(sources))), kb_id, slug),
            )
        for outgoing, _, page_id in updates:
            conn.execute("UPDATE wiki_pages SET out_links=?, updated_at=? WHERE id=?", (outgoing, db.utcnow(), page_id))
    return sum(len(loads(item[0], [])) for item in updates)


@router.get("/knowledgebase/{kb_id}/wiki/pages")
def list_pages(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    page_no, size, offset = pagination(request.query_params.get("page"), request.query_params.get("page_size"), 200)
    clauses = ["knowledge_base_id=?", "tenant_id=?", "deleted_at IS NULL"]
    params: list[Any] = [kb_id, tenant_id]
    for key in ("page_type", "status"):
        if request.query_params.get(key):
            clauses.append(f"{key}=?")
            params.append(request.query_params[key])
    query = (request.query_params.get("query") or "").strip()
    if query:
        clauses.append("(lower(title) LIKE ? OR lower(summary) LIKE ? OR lower(content) LIKE ?)")
        needle = f"%{query.lower()}%"
        params.extend([needle, needle, needle])
    where = " AND ".join(clauses)
    total = int(db.fetch_one(f"SELECT COUNT(*) AS n FROM wiki_pages WHERE {where}", params)["n"])
    sort = request.query_params.get("sort_by") or "sort_order"
    if sort not in {"title", "updated_at", "created_at", "sort_order", "version"}:
        sort = "sort_order"
    order = "DESC" if (request.query_params.get("sort_order") or "asc").lower() == "desc" else "ASC"
    rows = db.fetch_all(
        f"SELECT * FROM wiki_pages WHERE {where} ORDER BY {sort} {order}, title LIMIT ? OFFSET ?",
        [*params, size, offset],
    )
    return ok({
        "pages": [_serialize(row) for row in rows],
        "total": total,
        "page": page_no,
        "page_size": size,
        "total_pages": (total + size - 1) // size,
    })


@router.post("/knowledgebase/{kb_id}/wiki/pages", status_code=201)
async def create_page(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    data = await body(request)
    title = str(data.get("title") or "").strip()
    if not title:
        raise validation("Wiki page title is required")
    slug = _unique_slug(kb_id, str(data.get("slug") or _slugify(title)).strip("/"))
    folder_id = str(data.get("folder_id") or "")
    category_path = data.get("category_path") if isinstance(data.get("category_path"), list) else []
    now = db.utcnow()
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]',
                    '[]', ?, ?, 1, 'user', ?, ?, ?)
            """,
            (
                page_id, ctx.tenant_id, kb_id, slug, title,
                str(data.get("page_type") or "summary"), str(data.get("status") or "published"),
                str(data.get("content") or ""), str(data.get("summary") or ""),
                str(data.get("parent_slug") or ""), folder_id, dumps(category_path),
                str(data.get("wiki_path") or slug), int(data.get("depth") or 0),
                int(data.get("sort_order") or 0), dumps(data.get("source_refs") or []),
                dumps(data.get("page_metadata") or {}), dumps(data.get("aliases") or []),
                ctx.user_id, now, now,
            ),
        )
    _rebuild_links(kb_id, ctx.tenant_id)
    return ok(_serialize(_page(kb_id, slug, ctx.tenant_id), detail=True))


@router.get("/knowledgebase/{kb_id}/wiki/pages/{slug:path}")
def get_page(kb_id: str, slug: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    return ok(_serialize(_page(kb_id, slug, int(kb["tenant_id"])), detail=True))


@router.put("/knowledgebase/{kb_id}/wiki/pages/{slug:path}")
async def update_page(kb_id: str, slug: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    current = _page(kb_id, slug, ctx.tenant_id)
    data = await body(request)
    expected = int(data.get("version") or 0)
    if expected > 0 and expected != int(current["version"]):
        error = AppError("Wiki page was edited by someone else", status=409, code=1005)
        error.body = lambda: {**AppError.body(error), "current_version": current["version"], "data": {"current_version": current["version"]}}
        raise error
    _snapshot(current, source="user", editor_id=ctx.user_id)
    updates: dict[str, Any] = {"version": int(current["version"]) + 1, "updated_at": db.utcnow(), "last_edit_source": "user", "last_editor_id": ctx.user_id}
    for key in ("title", "content", "summary", "page_type", "status", "parent_slug", "wiki_path"):
        if key in data:
            updates[key] = str(data.get(key) or "")
    for key in ("depth", "sort_order"):
        if key in data:
            updates[key] = int(data.get(key) or 0)
    if "aliases" in data:
        updates["aliases"] = dumps(data.get("aliases") or [])
    if "category_path" in data:
        updates["category_path"] = dumps(data.get("category_path") or [])
    if "page_metadata" in data:
        updates["page_metadata"] = dumps(data.get("page_metadata") or {})
    with db.transaction() as conn:
        db.update("wiki_pages", updates, "id=?", (current["id"],), conn)
    _rebuild_links(kb_id, ctx.tenant_id)
    return ok(_serialize(_page(kb_id, slug, ctx.tenant_id), detail=True))


@router.delete("/knowledgebase/{kb_id}/wiki/pages/{slug:path}")
def delete_page(kb_id: str, slug: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    current = _page(kb_id, slug, ctx.tenant_id)
    _snapshot(current, source="user", editor_id=ctx.user_id)
    with db.transaction() as conn:
        conn.execute("UPDATE wiki_pages SET deleted_at=?, updated_at=? WHERE id=?", (db.utcnow(), db.utcnow(), current["id"]))
    _rebuild_links(kb_id, ctx.tenant_id)
    return ok(message="Wiki page deleted")


@router.get("/knowledgebase/{kb_id}/wiki/folders")
def list_folders(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    parent_id = request.query_params.get("parent_id") or ""
    rows = db.fetch_all(
        "SELECT * FROM wiki_folders WHERE knowledge_base_id=? AND tenant_id=? AND parent_id=? AND deleted_at IS NULL ORDER BY sort_order, name",
        (kb_id, tenant_id, parent_id),
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        count = int(db.fetch_one(
            "SELECT COUNT(*) AS n FROM wiki_pages WHERE knowledge_base_id=? AND folder_id=? AND deleted_at IS NULL",
            (kb_id, row["id"]),
        )["n"])
        child = db.fetch_one(
            "SELECT 1 FROM wiki_folders WHERE knowledge_base_id=? AND parent_id=? AND deleted_at IS NULL LIMIT 1",
            (kb_id, row["id"]),
        )
        result.append(_folder_json(row, count, bool(child)))
    return ok({"parent_id": parent_id, "folders": result})


@router.post("/knowledgebase/{kb_id}/wiki/folders", status_code=201)
async def create_folder(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    data = await body(request)
    name = str(data.get("name") or "").strip()
    if not name:
        raise validation("Folder name is required")
    parent_id = str(data.get("parent_id") or "")
    parent = _folder(kb_id, parent_id, ctx.tenant_id) if parent_id else None
    path = f"{parent['path']}/{name}" if parent and parent.get("path") else name
    depth = int(parent["depth"]) + 1 if parent else 0
    folder_id, now = db.new_id(), db.utcnow()
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO wiki_folders(id, tenant_id, knowledge_base_id, parent_id, name, path, depth, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (folder_id, ctx.tenant_id, kb_id, parent_id, name, path, depth, now, now),
            )
    except Exception as exc:  # noqa: BLE001
        raise conflict("A folder with this name already exists") from exc
    return ok(_folder_json(_folder(kb_id, folder_id, ctx.tenant_id)))


@router.put("/knowledgebase/{kb_id}/wiki/folders/{folder_id}")
async def update_folder(kb_id: str, folder_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    folder = _folder(kb_id, folder_id, ctx.tenant_id)
    data = await body(request)
    updates: dict[str, Any] = {"updated_at": db.utcnow()}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            raise validation("Folder name is required")
        updates["name"] = name
    if data.get("move_parent"):
        parent_id = str(data.get("parent_id") or "")
        if parent_id == folder_id:
            raise validation("A folder cannot contain itself")
        parent = _folder(kb_id, parent_id, ctx.tenant_id) if parent_id else None
        updates["parent_id"] = parent_id
        updates["depth"] = int(parent["depth"]) + 1 if parent else 0
        updates["path"] = f"{parent['path']}/{updates.get('name', folder['name'])}" if parent else updates.get("name", folder["name"])
    with db.transaction() as conn:
        db.update("wiki_folders", updates, "id=?", (folder_id,), conn)
    return ok(_folder_json(_folder(kb_id, folder_id, ctx.tenant_id)))


@router.delete("/knowledgebase/{kb_id}/wiki/folders/{folder_id}")
def delete_folder(kb_id: str, folder_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    _folder(kb_id, folder_id, ctx.tenant_id)
    if db.fetch_one("SELECT 1 FROM wiki_folders WHERE knowledge_base_id=? AND parent_id=? AND deleted_at IS NULL", (kb_id, folder_id)):
        raise conflict("Folder is not empty")
    if db.fetch_one("SELECT 1 FROM wiki_pages WHERE knowledge_base_id=? AND folder_id=? AND deleted_at IS NULL", (kb_id, folder_id)):
        raise conflict("Folder is not empty")
    with db.transaction() as conn:
        conn.execute("UPDATE wiki_folders SET deleted_at=?, updated_at=? WHERE id=?", (db.utcnow(), db.utcnow(), folder_id))
    return ok(message="Wiki folder deleted")


@router.put("/knowledgebase/{kb_id}/wiki/move-page")
async def move_page(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    data = await body(request)
    page = _page(kb_id, str(data.get("slug") or ""), ctx.tenant_id)
    folder_id = str(data.get("folder_id") or "")
    folder = _folder(kb_id, folder_id, ctx.tenant_id) if folder_id else None
    with db.transaction() as conn:
        conn.execute(
            "UPDATE wiki_pages SET folder_id=?, wiki_path=?, depth=?, updated_at=? WHERE id=?",
            (folder_id, folder["path"] if folder else page["slug"], int(folder["depth"]) + 1 if folder else 0, db.utcnow(), page["id"]),
        )
    return ok(_serialize(_page(kb_id, page["slug"], ctx.tenant_id), detail=True))


@router.get("/knowledgebase/{kb_id}/wiki/revisions/{slug:path}")
def list_revisions(kb_id: str, slug: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    page = _page(kb_id, slug, int(kb["tenant_id"]))
    version = request.query_params.get("version")
    if version:
        row = db.fetch_one(
            "SELECT * FROM wiki_page_revisions WHERE page_id=? AND version=?",
            (page["id"], int(version)),
        )
        if not row:
            raise not_found("Wiki revision not found")
        row["aliases"] = loads(row.get("aliases"), [])
        return ok(row)
    limit = min(200, max(1, int(request.query_params.get("limit") or 50)))
    offset = max(0, int(request.query_params.get("offset") or 0))
    total = int(db.fetch_one("SELECT COUNT(*) AS n FROM wiki_page_revisions WHERE page_id=?", (page["id"],))["n"])
    rows = db.fetch_all(
        "SELECT * FROM wiki_page_revisions WHERE page_id=? ORDER BY version DESC LIMIT ? OFFSET ?",
        (page["id"], limit, offset),
    )
    for row in rows:
        row["aliases"] = loads(row.get("aliases"), [])
        row.pop("content", None)
    return ok({"revisions": rows, "total": total, "current_version": page["version"]})


@router.post("/knowledgebase/{kb_id}/wiki/revert")
async def revert_page(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    data = await body(request)
    page = _page(kb_id, str(data.get("slug") or ""), ctx.tenant_id)
    revision = db.fetch_one(
        "SELECT * FROM wiki_page_revisions WHERE page_id=? AND version=?",
        (page["id"], int(data.get("version") or 0)),
    )
    if not revision:
        raise not_found("Wiki revision not found")
    _snapshot(page, source="revert", editor_id=ctx.user_id)
    updates = {
        "title": revision["title"], "page_type": revision["page_type"],
        "status": revision["status"], "content": revision["content"],
        "summary": revision["summary"], "aliases": revision.get("aliases") or "[]",
        "version": int(page["version"]) + 1, "last_edit_source": "revert",
        "last_editor_id": ctx.user_id, "updated_at": db.utcnow(),
    }
    with db.transaction() as conn:
        db.update("wiki_pages", updates, "id=?", (page["id"],), conn)
    _rebuild_links(kb_id, ctx.tenant_id)
    return ok(_serialize(_page(kb_id, page["slug"], ctx.tenant_id), detail=True))


@router.get("/knowledgebase/{kb_id}/wiki/index")
def wiki_index(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    requested = [value for value in (request.query_params.get("types") or "").split(",") if value]
    rows = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL ORDER BY page_type, sort_order, title",
        (kb_id, tenant_id),
    )
    intro_row = next((row for row in rows if row["slug"] in {"index", "wiki-index"}), None)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if requested and row["page_type"] not in requested:
            continue
        groups.setdefault(row["page_type"], []).append({
            "slug": row["slug"], "title": row["title"], "summary": row["summary"],
            "parent_slug": row["parent_slug"], "category_path": loads(row.get("category_path"), []),
            "wiki_path": row["wiki_path"], "depth": row["depth"], "sort_order": row["sort_order"],
        })
    limit = min(500, max(1, int(request.query_params.get("limit") or 100)))
    return ok({
        "intro": intro_row["content"] if intro_row else "",
        "version": max((int(row["version"]) for row in rows), default=0),
        "groups": [{"type": key, "total": len(items), "items": items[:limit]} for key, items in groups.items()],
    })


def _source_knowledge_ids(page: dict[str, Any]) -> list[str]:
    refs = loads(page.get("source_refs"), [])
    if not isinstance(refs, list):
        return []
    result: list[str] = []
    for ref in refs:
        if isinstance(ref, dict):
            value = str(ref.get("knowledge_id") or "").strip()
        else:
            value = str(ref or "").strip().split("|", 1)[0].strip()
        if value:
            result.append(value)
    return result


def _familiar_knowledge_ids(tenant_id: int, user_id: str) -> set[str]:
    rows = db.fetch_all(
        "SELECT knowledge_id FROM memory_doc_affinity "
        "WHERE tenant_id=? AND subject_id=? AND hits>=2 "
        "ORDER BY hits DESC, last_used_at DESC LIMIT 200",
        (tenant_id, user_id),
    )
    return {str(row.get("knowledge_id") or "") for row in rows if row.get("knowledge_id")}


def _graph_overview_type_rank(page_type: str) -> int:
    if page_type in {"entity", "concept"}:
        return 0
    if page_type in {"synthesis", "comparison"}:
        return 1
    if page_type == "summary":
        return 2
    return 3


def _bfs_ego_slugs(
    by_slug: dict[str, dict[str, Any]],
    adjacency: dict[str, set[str]],
    center: str,
    depth: int,
    type_allow: set[str],
    limit: int,
) -> set[str]:
    center_page = by_slug.get(center)
    if not center_page:
        return set()
    if type_allow and center_page.get("page_type") not in type_allow:
        return set()
    visited = {center}
    frontier = [center]
    for _ in range(max(1, depth)):
        if limit > 0 and len(visited) >= limit:
            break
        following: list[str] = []
        for slug in frontier:
            for neighbor in sorted(adjacency.get(slug, set())):
                if neighbor in visited:
                    continue
                page = by_slug.get(neighbor)
                if not page:
                    continue
                if type_allow and page.get("page_type") not in type_allow:
                    continue
                visited.add(neighbor)
                following.append(neighbor)
                if limit > 0 and len(visited) >= limit:
                    break
            if limit > 0 and len(visited) >= limit:
                break
        frontier = following
        if not frontier:
            break
    return visited


def _build_wiki_graph(
    pages: list[dict[str, Any]],
    *,
    mode: str,
    center: str,
    depth: int,
    types: list[str],
    limit: int,
    familiar_ids: set[str],
) -> dict[str, Any]:
    type_allow = {item for item in types if item}
    has_type_filter = bool(type_allow)
    if "index" not in type_allow:
        pages = [page for page in pages if page.get("page_type") != "index"]

    by_slug = {str(page.get("slug") or ""): page for page in pages}
    familiar = {
        slug
        for slug, page in by_slug.items()
        if familiar_ids.intersection(_source_knowledge_ids(page))
    }
    adjacency: dict[str, set[str]] = {}

    def connect(left: str, right: str) -> bool:
        left_page = by_slug.get(left)
        right_page = by_slug.get(right)
        if not left_page or not right_page or left == right:
            return False
        if has_type_filter and (
            left_page.get("page_type") not in type_allow
            or right_page.get("page_type") not in type_allow
        ):
            return False
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
        return True

    for page in pages:
        slug = str(page.get("slug") or "")
        for target in loads(page.get("out_links"), []) or []:
            connect(slug, str(target))
        for source in loads(page.get("in_links"), []) or []:
            connect(str(source), slug)

    summary_of: dict[str, str] = {}
    for page in pages:
        if page.get("page_type") != "summary":
            continue
        slug = str(page.get("slug") or "")
        for knowledge_id in _source_knowledge_ids(page):
            previous = summary_of.get(knowledge_id)
            if previous is None or slug < previous:
                summary_of[knowledge_id] = slug
    source_edges: list[tuple[str, str]] = []
    for page in pages:
        if page.get("page_type") == "summary":
            continue
        slug = str(page.get("slug") or "")
        for knowledge_id in _source_knowledge_ids(page):
            summary = summary_of.get(knowledge_id)
            if summary and connect(summary, slug):
                source_edges.append((summary, slug))

    link_count = {slug: len(neighbors) for slug, neighbors in adjacency.items()}
    selected: set[str]
    if mode == "ego":
        if not center:
            raise ValueError("ego graph requires a center slug")
        if center not in by_slug:
            raise ValueError(f'ego center slug "{center}" not found')
        selected = _bfs_ego_slugs(by_slug, adjacency, center, depth, type_allow, limit)
    else:
        candidates = [
            page for page in pages
            if not has_type_filter or page.get("page_type") in type_allow
        ]
        candidates.sort(key=lambda page: str(page.get("slug") or ""))
        candidates.sort(key=lambda page: str(page.get("updated_at") or ""), reverse=True)
        candidates.sort(key=lambda page: _graph_overview_type_rank(str(page.get("page_type") or "")))
        candidates.sort(key=lambda page: str(page.get("slug") or "") not in familiar)
        candidates.sort(key=lambda page: -link_count.get(str(page.get("slug") or ""), 0))
        if limit > 0:
            candidates = candidates[:limit]
        selected = {str(page.get("slug") or "") for page in candidates}

    nodes: list[dict[str, Any]] = []
    for slug in selected:
        page = by_slug[slug]
        node = {
            "slug": slug,
            "title": page.get("title") or "",
            "page_type": page.get("page_type") or "",
            "link_count": link_count.get(slug, 0),
        }
        if slug in familiar:
            node["familiar"] = True
        nodes.append(node)
    nodes.sort(key=lambda node: (-int(node["link_count"]), str(node["slug"])))

    edges: list[dict[str, str]] = []
    drawn: set[tuple[str, str]] = set()
    for page in pages:
        slug = str(page.get("slug") or "")
        if slug not in selected:
            continue
        for target in loads(page.get("out_links"), []) or []:
            target = str(target)
            if target not in selected or target == slug:
                continue
            if (slug, target) in drawn:
                continue
            drawn.add((slug, target))
            edges.append({"source": slug, "target": target})
    for source, target in source_edges:
        if source not in selected or target not in selected:
            continue
        if (source, target) in drawn or (target, source) in drawn:
            continue
        drawn.add((source, target))
        edges.append({"source": source, "target": target})

    total = len(pages)
    if mode != "ego" and has_type_filter:
        total = sum(1 for page in pages if page.get("page_type") in type_allow)
    meta: dict[str, Any] = {
        "mode": mode,
        "total": total,
        "returned": len(nodes),
        "truncated": len(nodes) < total,
    }
    familiar_count = sum(1 for node in nodes if node.get("familiar"))
    if familiar_count:
        meta["familiar_count"] = familiar_count
    if mode == "ego":
        meta["center"] = center
        meta["depth"] = max(1, depth)
    return {"nodes": nodes, "edges": edges, "meta": meta}


@router.get("/knowledgebase/{kb_id}/wiki/graph")
def wiki_graph(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    mode = (request.query_params.get("mode") or "").strip() or "overview"
    if mode not in {"overview", "ego"}:
        raise validation("mode must be 'overview' or 'ego'")
    center = (request.query_params.get("center") or "").strip()
    if mode == "ego" and not center:
        raise validation("center is required when mode=ego")
    depth = 1
    raw_depth = request.query_params.get("depth")
    if raw_depth not in (None, ""):
        try:
            depth = int(raw_depth)
        except (TypeError, ValueError) as exc:
            raise validation("depth must be a positive integer") from exc
        if depth < 1:
            raise validation("depth must be a positive integer")
        depth = min(3, depth)
    limit = 500
    raw_limit = request.query_params.get("limit")
    if raw_limit not in (None, ""):
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise validation("limit must be a positive integer") from exc
        if limit < 1:
            raise validation("limit must be a positive integer")
        limit = min(2000, limit)
    types = [item.strip() for item in (request.query_params.get("types") or "").split(",") if item.strip()]
    rows = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, tenant_id),
    )
    try:
        graph = _build_wiki_graph(
            rows,
            mode=mode,
            center=center,
            depth=depth,
            types=types,
            limit=limit,
            familiar_ids=_familiar_knowledge_ids(tenant_id, ctx.user_id),
        )
    except ValueError as exc:
        raise AppError(str(exc), status=500, code=1500) from exc
    return ok(graph)


@router.get("/knowledgebase/{kb_id}/wiki/stats")
def wiki_stats(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    rows = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, tenant_id),
    )
    by_type: dict[str, int] = {}
    links = 0
    orphan = 0
    for row in rows:
        by_type[row["page_type"]] = by_type.get(row["page_type"], 0) + 1
        incoming = loads(row.get("in_links"), [])
        links += len(loads(row.get("out_links"), []))
        orphan += 1 if not incoming else 0
    recent = sorted(rows, key=lambda item: item.get("updated_at") or "", reverse=True)[:10]
    pending_issues = int(db.fetch_one(
        "SELECT COUNT(*) AS n FROM wiki_page_issues WHERE knowledge_base_id=? AND status='pending' AND deleted_at IS NULL",
        (kb_id,),
    )["n"])
    pending_tasks = int(db.fetch_one(
        "SELECT COUNT(*) AS n FROM task_pending_ops WHERE task_type='wiki:ingest' AND scope='knowledge_base' AND scope_id=?",
        (kb_id,),
    )["n"])
    return ok({
        "total_pages": len(rows), "pages_by_type": by_type, "total_links": links,
        "orphan_count": orphan, "recent_updates": [_serialize(row) for row in recent],
        "pending_tasks": pending_tasks, "pending_issues": pending_issues,
        "is_active": pending_tasks > 0,
    })


@router.get("/knowledgebase/{kb_id}/wiki/search")
def search_wiki(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    query = (request.query_params.get("q") or "").strip()
    limit = min(100, max(1, int(request.query_params.get("limit") or 20)))
    if not query:
        return ok([])
    rows = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL AND (lower(title) LIKE ? OR lower(summary) LIKE ? OR lower(content) LIKE ?) ORDER BY updated_at DESC LIMIT ?",
        (kb_id, tenant_id, f"%{query.lower()}%", f"%{query.lower()}%", f"%{query.lower()}%", limit),
    )
    return ok([_serialize(row) for row in rows])


@router.get("/knowledgebase/{kb_id}/wiki/issues")
def list_issues(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    clauses = ["knowledge_base_id=?", "tenant_id=?", "deleted_at IS NULL"]
    params: list[Any] = [kb_id, tenant_id]
    for key in ("slug", "status", "issue_type"):
        if request.query_params.get(key):
            clauses.append(f"{key}=?")
            params.append(request.query_params[key])
    rows = db.fetch_all("SELECT * FROM wiki_page_issues WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC", params)
    for row in rows:
        row["suspected_knowledge_ids"] = loads(row.get("suspected_knowledge_ids"), [])
    return ok(rows)


@router.put("/knowledgebase/{kb_id}/wiki/issues/{issue_id}/status")
async def update_issue_status(kb_id: str, issue_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    data = await body(request)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE wiki_page_issues SET status=?, updated_at=? WHERE id=? AND knowledge_base_id=? AND tenant_id=?",
            (str(data.get("status") or "resolved"), db.utcnow(), issue_id, kb_id, ctx.tenant_id),
        )
    row = db.fetch_one("SELECT * FROM wiki_page_issues WHERE id=? AND knowledge_base_id=? AND tenant_id=?", (issue_id, kb_id, ctx.tenant_id))
    if not row:
        raise not_found("Wiki issue not found")
    row["suspected_knowledge_ids"] = loads(row.get("suspected_knowledge_ids"), [])
    return ok(row)


@router.post("/knowledgebase/{kb_id}/wiki/rebuild-links")
def rebuild_links(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    count = _rebuild_links(kb_id, ctx.tenant_id)
    return ok({"links": count})


@router.post("/knowledgebase/{kb_id}/wiki/auto-fix")
def auto_fix(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    _kb(ctx, kb_id, write=True)
    require_role(ctx, "contributor")
    count = _rebuild_links(kb_id, ctx.tenant_id)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE wiki_page_issues SET status='resolved', updated_at=? WHERE knowledge_base_id=? AND tenant_id=? AND issue_type IN ('broken_link','missing_backlink') AND status='pending'",
            (db.utcnow(), kb_id, ctx.tenant_id),
        )
    return ok({"fixed": count})


@router.get("/knowledgebase/{kb_id}/wiki/lint")
def lint_wiki(kb_id: str, request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    kb = _kb(ctx, kb_id)
    tenant_id = int(kb["tenant_id"])
    rows = db.fetch_all(
        "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND tenant_id=? AND deleted_at IS NULL",
        (kb_id, tenant_id),
    )
    by_slug = {row["slug"] for row in rows}
    broken: list[dict[str, Any]] = []
    orphan: list[dict[str, Any]] = []
    for row in rows:
        for target in loads(row.get("out_links"), []):
            if target not in by_slug:
                broken.append({"slug": row["slug"], "target": target})
        if not loads(row.get("in_links"), []) and len(rows) > 1:
            orphan.append({"slug": row["slug"], "title": row["title"]})
    return ok({"healthy": not broken and not orphan, "broken_links": broken, "orphans": orphan, "page_count": len(rows)})

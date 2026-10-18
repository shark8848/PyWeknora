"""Track stable ``resource://`` handles claimed by knowledge entries."""

from __future__ import annotations

import hashlib
import re
import urllib.parse
from typing import Iterable

from .. import db


RESOURCE_PREFIX = "resource://"
RESOURCE_HANDLE_LENGTH = 22
RESOURCE_OWNER_KNOWLEDGE = "knowledge"
RESOURCE_RELATION_ATTACHMENT = "attachment"
RESOURCE_RELATION_EXTRACTED_IMAGE = "extracted_image"

_HANDLE_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)
_REFERENCE_RE = re.compile(
    re.escape(RESOURCE_PREFIX) + rf"[A-Za-z0-9_-]{{{RESOURCE_HANDLE_LENGTH}}}"
)
_RELEASABLE_RELATIONS = (
    RESOURCE_RELATION_ATTACHMENT,
    RESOURCE_RELATION_EXTRACTED_IMAGE,
)


def parse_handle(reference: str) -> str:
    value = str(reference or "").strip()
    if not value.startswith(RESOURCE_PREFIX):
        return ""
    handle = value[len(RESOURCE_PREFIX):].split("/", 1)[0]
    if len(handle) != RESOURCE_HANDLE_LENGTH or any(char not in _HANDLE_CHARS for char in handle):
        return ""
    return handle


def build_reference(handle: str) -> str:
    return RESOURCE_PREFIX + str(handle or "").strip()


def _provider_for(physical_path: str) -> str:
    value = str(physical_path or "").strip()
    if value.startswith("storage://"):
        _backend_id, _separator, value = value[len("storage://"):].partition("/")
    return urllib.parse.urlsplit(value).scheme.lower() or "local"


def register_path(
    tenant_id: int,
    physical_path: str,
    *,
    original_name: str = "",
    size: int = 0,
    kind: str = "file",
    mime_type: str = "",
) -> str:
    """Register an existing physical path and return its stable reference."""
    tenant_id = int(tenant_id or 0)
    value = str(physical_path or "").strip()
    if tenant_id <= 0 or not value:
        raise ValueError("resource registration requires tenant and physical path")
    location_hash = hashlib.sha256(value.encode()).hexdigest()
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT handle FROM resources WHERE tenant_id=? AND location_hash=? "
            "AND state='active' AND deleted_at IS NULL",
            (tenant_id, location_hash),
        ).fetchone()
        if existing:
            return build_reference(str(existing["handle"]))
        handle = db.new_id().replace("-", "")[:RESOURCE_HANDLE_LENGTH]
        now = db.utcnow()
        conn.execute(
            "INSERT INTO resources(id, handle, tenant_id, provider, physical_path, location_hash, "
            "kind, mime_type, original_name, size, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                db.new_id(),
                handle,
                tenant_id,
                _provider_for(value),
                value,
                location_hash,
                kind or "file",
                mime_type,
                original_name,
                max(0, int(size or 0)),
                now,
                now,
            ),
        )
        return build_reference(handle)


def scan_references(text: str) -> list[str]:
    """Return distinct canonical handles embedded in rich text."""
    value = str(text or "")
    if RESOURCE_PREFIX not in value:
        return []
    references: list[str] = []
    seen: set[str] = set()
    for match in _REFERENCE_RE.finditer(value):
        if match.end() < len(value) and value[match.end()] in _HANDLE_CHARS:
            continue
        reference = match.group(0)
        if reference not in seen:
            seen.add(reference)
            references.append(reference)
    return references


def bind_references(
    tenant_id: int,
    owner_id: str,
    references: Iterable[str],
    *,
    relation: str = RESOURCE_RELATION_ATTACHMENT,
) -> int:
    """Claim active resources for one knowledge entry.

    Unknown and cross-tenant handles are skipped. Claims are idempotent, so a
    retried create or clone cannot fail on an existing binding.
    """
    tenant_id = int(tenant_id or 0)
    owner_id = str(owner_id or "")
    if tenant_id <= 0 or not owner_id:
        return 0
    handles: list[str] = []
    for reference in references:
        handle = parse_handle(str(reference))
        if handle and handle not in handles:
            handles.append(handle)
    if not handles:
        return 0
    bound = 0
    now = db.utcnow()
    with db.transaction() as conn:
        for handle in handles:
            resource = conn.execute(
                "SELECT id FROM resources WHERE handle=? AND tenant_id=? "
                "AND state='active' AND deleted_at IS NULL",
                (handle, tenant_id),
            ).fetchone()
            if not resource:
                continue
            cursor = conn.execute(
                "INSERT OR IGNORE INTO resource_bindings"
                "(id, resource_id, tenant_id, owner_type, owner_id, relation, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    db.new_id(),
                    resource["id"],
                    tenant_id,
                    RESOURCE_OWNER_KNOWLEDGE,
                    owner_id,
                    relation,
                    now,
                ),
            )
            if int(cursor.rowcount or 0) > 0:
                bound += 1
    return bound


def bind_content_references(
    tenant_id: int,
    owner_id: str,
    text: str,
    *,
    relation: str = RESOURCE_RELATION_ATTACHMENT,
) -> int:
    return bind_references(tenant_id, owner_id, scan_references(text), relation=relation)


def list_references_by_owner(
    tenant_id: int,
    owner_ids: Iterable[str],
    *,
    relations: tuple[str, ...] = _RELEASABLE_RELATIONS,
) -> list[str]:
    """List active derived resources still claimed by any owner."""
    ids = [str(owner_id) for owner_id in owner_ids if str(owner_id or "")]
    if int(tenant_id or 0) <= 0 or not ids or not relations:
        return []
    marks = ",".join("?" for _ in relations)
    rows = db.fetch_all(
        "SELECT DISTINCT r.handle FROM resource_bindings AS b "
        "JOIN resources AS r ON r.id=b.resource_id "
        "WHERE b.tenant_id=? AND b.owner_type=? AND b.owner_id IN ("
        + ",".join("?" for _ in ids)
        + ") AND b.relation IN ("
        + marks
        + ") AND r.tenant_id=? AND r.state='active' AND r.deleted_at IS NULL",
        (tenant_id, RESOURCE_OWNER_KNOWLEDGE, *ids, *relations, tenant_id),
    )
    return [build_reference(str(row["handle"])) for row in rows]


def release_reference(reference: str, tenant_id: int, owner_ids: Iterable[str]) -> int:
    """Drop each owner's claim and return how many claims remain.

    A non-catalog reference returns ``-1`` so callers keep their previous
    unconditional delete behaviour. An unknown or foreign catalog handle also
    returns ``-1`` after logging is left to the caller.
    """
    handle = parse_handle(reference)
    if not handle:
        return -1
    ids = [str(owner_id) for owner_id in owner_ids if str(owner_id or "")]
    if not ids:
        return -1
    with db.transaction() as conn:
        resource = conn.execute(
            "SELECT id FROM resources WHERE handle=? AND tenant_id=? "
            "AND state='active' AND deleted_at IS NULL",
            (handle, int(tenant_id or 0)),
        ).fetchone()
        if not resource:
            raise LookupError("resource not found")
        conn.execute(
            "DELETE FROM resource_bindings WHERE resource_id=? AND owner_type=? "
            "AND owner_id IN (" + ",".join("?" for _ in ids) + ")",
            (resource["id"], RESOURCE_OWNER_KNOWLEDGE, *ids),
        )
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM resource_bindings WHERE resource_id=?",
            (resource["id"],),
        ).fetchone()
        return int(row["n"] if row else 0)


def mark_deleted(reference: str, tenant_id: int) -> bool:
    handle = parse_handle(reference)
    if not handle:
        return False
    now = db.utcnow()
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE resources SET state='deleted', deleted_at=?, updated_at=? "
            "WHERE handle=? AND tenant_id=? AND state='active'",
            (now, now, handle, int(tenant_id or 0)),
        )
        return int(cursor.rowcount or 0) == 1

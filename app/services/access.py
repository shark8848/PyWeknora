"""Read-only access resolution for cross-workspace shares."""

from __future__ import annotations

from typing import Any

from .. import db


_ORG_ROLE_LEVEL = {"viewer": 1, "editor": 2, "admin": 3}


def _role_level(role: str | None) -> int:
    return _ORG_ROLE_LEVEL.get(str(role or ""), 0)


def _effective_share_role(share_role: str | None, member_role: str | None, tenant_role: str | None) -> str:
    share_level = _role_level(share_role)
    member_level = _role_level(member_role)
    if not share_level or not member_level:
        return ""
    effective = share_role if share_level <= member_level else member_role
    # Mirrors WeKnora's three-dimensional cap: a tenant Viewer cannot gain
    # write access through an organization share.
    if tenant_role == "viewer" and _role_level(effective) >= _ORG_ROLE_LEVEL["editor"]:
        return "viewer"
    return str(effective or "")


def knowledge_base_share_permission(
    tenant_id: int,
    kb_id: str,
    tenant_role: str | None = None,
) -> str:
    """Return the highest effective organization-share role for a caller."""
    rows = db.fetch_all(
        """
        SELECT s.permission AS share_role, member.role AS member_role
        FROM kb_shares s
        JOIN organizations o ON o.id=s.organization_id AND o.deleted_at IS NULL
        JOIN organization_tenant_members source
          ON source.organization_id=s.organization_id AND source.tenant_id=s.source_tenant_id
        JOIN organization_tenant_members member
          ON member.organization_id=s.organization_id AND member.tenant_id=?
        JOIN knowledge_bases kb ON kb.id=s.knowledge_base_id AND kb.deleted_at IS NULL
        WHERE s.knowledge_base_id=? AND s.deleted_at IS NULL
        """,
        (tenant_id, kb_id),
    )
    highest = ""
    for row in rows:
        effective = _effective_share_role(row.get("share_role"), row.get("member_role"), tenant_role)
        if _role_level(effective) > _role_level(highest):
            highest = effective
    return highest


def shared_knowledge_base_ids(tenant_id: int, tenant_role: str | None = None) -> set[str]:
    rows = db.fetch_all(
        """
        SELECT DISTINCT s.knowledge_base_id
        FROM kb_shares s
        JOIN organizations o ON o.id=s.organization_id AND o.deleted_at IS NULL
        JOIN organization_tenant_members source
          ON source.organization_id=s.organization_id AND source.tenant_id=s.source_tenant_id
        JOIN organization_tenant_members member
          ON member.organization_id=s.organization_id AND member.tenant_id=?
        JOIN knowledge_bases kb ON kb.id=s.knowledge_base_id AND kb.deleted_at IS NULL
        WHERE s.deleted_at IS NULL AND s.permission IN ('viewer', 'editor', 'admin')
        """,
        (tenant_id,),
    )
    return {str(row["knowledge_base_id"]) for row in rows}


def readable_knowledge_base(
    tenant_id: int,
    kb_id: str,
    tenant_role: str | None = None,
) -> dict[str, Any] | None:
    row = db.fetch_one(
        "SELECT * FROM knowledge_bases WHERE id=? AND deleted_at IS NULL",
        (kb_id,),
    )
    if not row:
        return None
    if int(row.get("tenant_id") or 0) == tenant_id:
        return row
    return row if knowledge_base_share_permission(tenant_id, kb_id, tenant_role) else None


def readable_knowledge(
    tenant_id: int,
    knowledge_id: str,
    tenant_role: str | None = None,
) -> dict[str, Any] | None:
    row = db.fetch_one(
        "SELECT * FROM knowledges WHERE id=? AND deleted_at IS NULL",
        (knowledge_id,),
    )
    if not row:
        return None
    if int(row.get("tenant_id") or 0) == tenant_id:
        return row
    kb = readable_knowledge_base(tenant_id, str(row.get("knowledge_base_id") or ""), tenant_role)
    return row if kb else None

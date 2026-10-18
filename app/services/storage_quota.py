"""Atomic tenant storage accounting for knowledge writes."""

from __future__ import annotations

from typing import Any, Iterable

from .. import db
from ..errors import AppError


class StorageQuotaExceeded(AppError):
    def __init__(self) -> None:
        super().__init__("Storage quota exceeded", status=400, code=1010)


def knowledge_storage_size(file_size: int | None, text: str) -> int:
    return max(int(file_size or 0), len(str(text or "").encode("utf-8")))


def reserve_storage(conn: Any, tenant_id: int, amount: int) -> None:
    if amount <= 0:
        return
    cursor = conn.execute(
        """
        UPDATE tenants SET storage_used=storage_used+?, updated_at=?
        WHERE id=? AND deleted_at IS NULL
          AND (storage_quota<=0 OR storage_used+?<=storage_quota)
        """,
        (amount, db.utcnow(), tenant_id, amount),
    )
    if int(cursor.rowcount or 0) != 1:
        raise StorageQuotaExceeded()


def release_storage(conn: Any, tenant_id: int, amount: int) -> None:
    if amount <= 0:
        return
    conn.execute(
        "UPDATE tenants SET storage_used=MAX(0, storage_used-?), updated_at=? "
        "WHERE id=? AND deleted_at IS NULL",
        (amount, db.utcnow(), tenant_id),
    )


def adjust_storage(conn: Any, tenant_id: int, old_size: int, new_size: int) -> None:
    old_size = max(0, int(old_size or 0))
    new_size = max(0, int(new_size or 0))
    if new_size > old_size:
        reserve_storage(conn, tenant_id, new_size - old_size)
    elif new_size < old_size:
        release_storage(conn, tenant_id, old_size - new_size)


def release_knowledge_rows(conn: Any, rows: Iterable[dict[str, Any]]) -> None:
    by_tenant: dict[int, int] = {}
    for row in rows:
        tenant_id = int(row.get("tenant_id") or 0)
        size = max(0, int(row.get("storage_size") or 0))
        if tenant_id > 0 and size > 0:
            by_tenant[tenant_id] = by_tenant.get(tenant_id, 0) + size
    for tenant_id, amount in by_tenant.items():
        release_storage(conn, tenant_id, amount)

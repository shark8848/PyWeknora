"""Cross-process leases for IM long-connection ownership."""

from __future__ import annotations

import time

from .. import db


LEASE_SECONDS = 15.0


def acquire(channel_id: str, lease_key: str) -> bool:
    now = time.time()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO py_im_channel_leases(channel_id, lease_key, lease_until, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
              lease_key=excluded.lease_key,
              lease_until=excluded.lease_until,
              updated_at=excluded.updated_at
            WHERE py_im_channel_leases.lease_until <= ?
               OR py_im_channel_leases.lease_key = excluded.lease_key
            """,
            (channel_id, lease_key, now + LEASE_SECONDS, db.utcnow(), now),
        )
        return int(cursor.rowcount or 0) == 1


def renew(channel_id: str, lease_key: str) -> bool:
    now = time.time()
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE py_im_channel_leases SET lease_until=?, updated_at=?
            WHERE channel_id=? AND lease_key=? AND lease_until > ?
            """,
            (now + LEASE_SECONDS, db.utcnow(), channel_id, lease_key, now),
        )
        return int(cursor.rowcount or 0) == 1


def release(channel_id: str, lease_key: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM py_im_channel_leases WHERE channel_id=? AND lease_key=?",
            (channel_id, lease_key),
        )

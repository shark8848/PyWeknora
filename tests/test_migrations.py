"""SQLite migration compatibility checks."""

from __future__ import annotations

import sqlite3

from app import config


def _run_migration(conn: sqlite3.Connection, name: str) -> None:
    conn.executescript((config.MIGRATIONS_DIR / name).read_text(encoding="utf-8"))


def test_replayable_table_migrations_tolerate_existing_objects() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        for name in (
            "000014_browser_authorization.up.sql",
            "000031_model_catalog_config.up.sql",
        ):
            _run_migration(conn, name)
            _run_migration(conn, name)

        assert conn.execute(
            "SELECT version, overlay, history FROM model_catalog_configs WHERE id=1"
        ).fetchone() == (0, '{"providers":{}}', "[]")
    finally:
        conn.close()

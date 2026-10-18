"""SQLite persistence and the original WeKnora schema migrations."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config


_init_lock = threading.Lock()
_initialized = False


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def init_db(seed: bool = True) -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        config.ensure_dirs()
        conn = connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS _pyweknora_migrations "
                "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_faq_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id INTEGER NOT NULL,
                    knowledge_base_id VARCHAR(36) NOT NULL,
                    standard_question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    similar_questions TEXT NOT NULL DEFAULT '[]',
                    tag_id INTEGER,
                    is_enabled INTEGER NOT NULL DEFAULT 1,
                    is_recommended INTEGER NOT NULL DEFAULT 0,
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_py_faq_kb ON py_faq_entries(tenant_id, knowledge_base_id, deleted_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_task_results (
                    id VARCHAR(36) PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    kind VARCHAR(32) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}',
                    result TEXT NOT NULL DEFAULT '{}',
                    error TEXT NOT NULL DEFAULT '',
                    worker_token VARCHAR(64) NOT NULL DEFAULT '',
                    lease_until TEXT NOT NULL DEFAULT '',
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retry INTEGER NOT NULL DEFAULT 0,
                    retry_after TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            task_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(py_task_results)")
            }
            if "worker_token" not in task_columns:
                conn.execute(
                    "ALTER TABLE py_task_results ADD COLUMN worker_token VARCHAR(64) NOT NULL DEFAULT ''"
                )
            if "lease_until" not in task_columns:
                conn.execute(
                    "ALTER TABLE py_task_results ADD COLUMN lease_until TEXT NOT NULL DEFAULT ''"
                )
            if "retry_count" not in task_columns:
                conn.execute(
                    "ALTER TABLE py_task_results ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )
            if "max_retry" not in task_columns:
                conn.execute(
                    "ALTER TABLE py_task_results ADD COLUMN max_retry INTEGER NOT NULL DEFAULT 0"
                )
            if "retry_after" not in task_columns:
                conn.execute(
                    "ALTER TABLE py_task_results ADD COLUMN retry_after TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                "UPDATE py_task_results SET max_retry=3 WHERE kind IN "
                "('kb_clone','knowledge_move','knowledge_reparse') AND max_retry=0"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_py_task_results_worker_lease "
                "ON py_task_results(status, lease_until)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_pending_approvals (
                    id VARCHAR(36) PRIMARY KEY,
                    kind VARCHAR(16) NOT NULL,
                    tenant_id INTEGER NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    session_id VARCHAR(64) NOT NULL,
                    assistant_message_id VARCHAR(64) NOT NULL,
                    request_id VARCHAR(64) NOT NULL,
                    service_id VARCHAR(64) NOT NULL,
                    service_name TEXT NOT NULL,
                    mcp_tool_name TEXT NOT NULL,
                    registered_tool_name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    requested_at INTEGER NOT NULL,
                    status VARCHAR(16) NOT NULL DEFAULT 'pending',
                    decision TEXT NOT NULL DEFAULT '{}',
                    resolved_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_py_pending_approvals_scope "
                "ON py_pending_approvals(tenant_id, status, requested_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_mcp_oauth_states (
                    state VARCHAR(128) PRIMARY KEY,
                    data TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_mcp_oauth_attempts (
                    id VARCHAR(128) PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    user_id VARCHAR(64) NOT NULL,
                    service_id VARCHAR(64) NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_py_mcp_oauth_attempts_expiry "
                "ON py_mcp_oauth_attempts(expires_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_memory_settings (
                    tenant_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    auto_confirm INTEGER NOT NULL DEFAULT 0,
                    retention_days INTEGER NOT NULL DEFAULT 365,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_skill_catalog (
                    id VARCHAR(36) PRIMARY KEY,
                    tenant_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    version TEXT NOT NULL DEFAULT '1.0.0',
                    source TEXT NOT NULL DEFAULT 'upload',
                    manifest TEXT NOT NULL DEFAULT '{}',
                    files TEXT NOT NULL DEFAULT '{}',
                    installed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_im_sessions (
                    channel_id VARCHAR(36) NOT NULL,
                    tenant_id INTEGER NOT NULL,
                    external_user_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    thread_id TEXT NOT NULL DEFAULT '',
                    session_id VARCHAR(36) NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, external_user_id, chat_id, thread_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_im_messages (
                    channel_id VARCHAR(36) NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    session_id VARCHAR(36) NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, dedupe_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_workspace_policies (
                    tenant_id INTEGER PRIMARY KEY,
                    scripts_disabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS py_model_catalog (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    version INTEGER NOT NULL DEFAULT 1,
                    baseline TEXT NOT NULL DEFAULT '',
                    overlay TEXT NOT NULL DEFAULT '{"providers":{}}',
                    history TEXT NOT NULL DEFAULT '[]',
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO py_model_catalog(id, version, baseline, overlay, history, updated_by, updated_at) VALUES (1, 1, '', '{\"providers\":{}}', '[]', '', ?)",
                (utcnow(),),
            )
            applied = {
                row[0]
                for row in conn.execute("SELECT name FROM _pyweknora_migrations")
            }
            for path in sorted(config.MIGRATIONS_DIR.glob("*.up.sql")):
                if path.name in applied:
                    continue
                conn.executescript(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO _pyweknora_migrations(name, applied_at) VALUES (?, ?)",
                    (path.name, utcnow()),
                )
                conn.commit()
        finally:
            conn.close()
        if seed:
            seed_defaults()
        _initialized = True


def seed_defaults() -> None:
    from .security import hash_password

    now = utcnow()
    with transaction() as conn:
        row = conn.execute("SELECT id FROM tenants ORDER BY id LIMIT 1").fetchone()
        if row:
            tenant_id = int(row["id"])
        else:
            cur = conn.execute(
                """
                INSERT INTO tenants
                (name, description, retriever_engines, status, business, storage_quota,
                 storage_used, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "默认空间",
                    "PyWeKnora 默认工作空间",
                    json.dumps({"engines": []}, ensure_ascii=False),
                    "active",
                    "knowledge_management",
                    10 * 1024 * 1024 * 1024,
                    0,
                    now,
                    now,
                ),
            )
            tenant_id = int(cur.lastrowid)

        user = conn.execute(
            "SELECT id FROM users WHERE email = ? AND deleted_at IS NULL",
            (config.DEFAULT_ADMIN_EMAIL,),
        ).fetchone()
        if user:
            user_id = user["id"]
        else:
            user_id = new_id()
            conn.execute(
                """
                INSERT INTO users
                (id, username, email, password_hash, avatar, tenant_id, is_active,
                 can_access_all_tenants, preferences, created_at, updated_at,
                 is_system_admin)
                VALUES (?, ?, ?, ?, '', ?, 1, 0, ?, ?, ?, 1)
                """,
                (
                    user_id,
                    config.DEFAULT_ADMIN_USERNAME,
                    config.DEFAULT_ADMIN_EMAIL,
                    hash_password(config.DEFAULT_ADMIN_PASSWORD),
                    tenant_id,
                    "{}",
                    now,
                    now,
                ),
            )

        member = conn.execute(
            "SELECT id FROM tenant_members WHERE user_id=? AND tenant_id=? AND deleted_at IS NULL",
            (user_id, tenant_id),
        ).fetchone()
        if not member:
            conn.execute(
                """
                INSERT INTO tenant_members
                (user_id, tenant_id, role, status, joined_at, created_at, updated_at)
                VALUES (?, ?, 'owner', 'active', ?, ?, ?)
                """,
                (user_id, tenant_id, now, now, now),
            )

        for key, value, kind, category, description in (
            ("auth.registration_mode", config.REGISTRATION_MODE, "string", "auth", "Registration mode"),
            ("auth.complex_password", "true" if config.COMPLEX_PASSWORD else "false", "bool", "auth", "Complex password policy"),
            ("tenant.self_service_creation", "true", "bool", "tenant", "Allow self-service workspace creation"),
            ("tenant.auto_accept_invitation", "false", "bool", "tenant", "Automatically accept invitations"),
        ):
            conn.execute(
                """
                INSERT INTO system_settings
                (key, value, value_type, category, description, is_secret,
                 requires_restart, last_modified_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, 0, '', ?, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, value, kind, category, description, now, now),
            )

        chat_model_id, embedding_model_id = _seed_environment_models(conn, tenant_id, now)

        if not conn.execute("SELECT 1 FROM knowledge_bases WHERE deleted_at IS NULL LIMIT 1").fetchone():
            kb_id = new_id()
            conn.execute(
                """
                INSERT INTO knowledge_bases
                (id, name, description, tenant_id, type, chunking_config,
                 image_processing_config, embedding_model_id, summary_model_id,
                 cos_config, vlm_config, is_temporary, is_pinned, created_at,
                 updated_at, creator_id, indexing_strategy)
                VALUES (?, ?, ?, ?, 'document', ?, '{}', '', '', '{}', '{}',
                        0, 0, ?, ?, ?, ?)
                """,
                (
                    kb_id,
                    "示例知识库",
                    "预置的 WeKnora 示例资料，可直接提问。",
                    tenant_id,
                    json.dumps({"chunk_size": 512, "chunk_overlap": 50}, ensure_ascii=False),
                    now,
                    now,
                    user_id,
                    json.dumps({"vector": True, "keyword": True, "wiki": False, "graph": False}),
                ),
            )
            if embedding_model_id or chat_model_id:
                conn.execute(
                    "UPDATE knowledge_bases SET embedding_model_id=?, summary_model_id=? WHERE id=?",
                    (embedding_model_id, chat_model_id, kb_id),
                )
            _seed_sample_documents(conn, tenant_id, kb_id, user_id)


def _seed_environment_models(
    conn: sqlite3.Connection, tenant_id: int, now: str
) -> tuple[str, str]:
    """Register environment-configured endpoints as ordinary editable models."""
    entries = []
    if config.LLM_BASE_URL:
        entries.append(
            (
                "builtin-env-chat",
                config.LLM_MODEL,
                config.LLM_MODEL,
                "KnowledgeQA",
                {
                    "base_url": config.LLM_BASE_URL,
                    "api_key": config.LLM_API_KEY,
                    "provider": "openai-compatible",
                },
            )
        )
    if config.EMBEDDING_BASE_URL:
        entries.append(
            (
                "builtin-env-embedding",
                config.EMBEDDING_MODEL,
                config.EMBEDDING_MODEL,
                "Embedding",
                {
                    "base_url": config.EMBEDDING_BASE_URL,
                    "api_key": config.EMBEDDING_API_KEY,
                    "provider": "openai-compatible",
                },
            )
        )
    chat_id = ""
    embedding_id = ""
    for model_id, name, display_name, model_type, parameters in entries:
        conn.execute(
            """
            INSERT INTO models(id, tenant_id, name, display_name, type, source,
              description, parameters, is_default, is_builtin, managed_by, status,
              created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'remote', 'Configured from environment variables',
              ?, 1, 0, 'env', 'active', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, display_name=excluded.display_name,
              parameters=excluded.parameters, status='active', updated_at=excluded.updated_at
            WHERE models.managed_by='env'
            """,
            (model_id, tenant_id, name, display_name, model_type, json.dumps(parameters), now, now),
        )
        if model_type == "KnowledgeQA":
            chat_id = model_id
        else:
            embedding_id = model_id
    return chat_id, embedding_id


def _seed_sample_documents(
    conn: sqlite3.Connection, tenant_id: int, kb_id: str, user_id: str
) -> None:
    from .services.chunking import split_text

    sample_dir = config.BASE_DIR / "sample-data"
    if not sample_dir.exists():
        return
    for path in sorted(sample_dir.glob("*")):
        if path.suffix.lower() not in {".md", ".txt", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        now = utcnow()
        knowledge_id = new_id()
        conn.execute(
            """
            INSERT INTO knowledges
            (id, tenant_id, knowledge_base_id, type, title, description, source,
             parse_status, enable_status, embedding_model_id, file_name, file_type,
             file_size, file_hash, storage_size, metadata, custom_metadata,
             summary_status, channel, created_at, updated_at, processed_at,
             folder_path, pending_subtasks_count, profile)
            VALUES (?, ?, ?, 'file', ?, '', ?, 'completed', 'enabled', '', ?, ?,
                    ?, '', ?, '{}', '{}', 'none', 'web', ?, ?, ?, '', 0, '{}')
            """,
            (
                knowledge_id,
                tenant_id,
                kb_id,
                path.stem,
                path.name,
                path.name,
                path.suffix.lstrip(".").lower(),
                len(text.encode("utf-8")),
                len(text.encode("utf-8")),
                now,
                now,
                now,
            ),
        )
        for idx, piece in enumerate(split_text(text)):
            conn.execute(
                """
                INSERT INTO chunks
                (id, tenant_id, knowledge_base_id, knowledge_id, content,
                 source_content, content_revision, index_status, last_editor_id,
                 context_header, chunk_index, is_enabled, start_at, end_at,
                 chunk_type, status, flags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, 'indexed', '', '', ?, 1, 0, ?,
                        'text', 0, 0, ?, ?)
                """,
                (
                    new_id(),
                    tenant_id,
                    kb_id,
                    knowledge_id,
                    piece,
                    piece,
                    idx,
                    len(piece),
                    now,
                    now,
                ),
            )


def fetch_all(
    sql: str, params: Iterable[Any] = (), conn: sqlite3.Connection | None = None
) -> list[dict[str, Any]]:
    own = conn is None
    conn = conn or connect()
    try:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    finally:
        if own:
            conn.close()


def fetch_one(
    sql: str, params: Iterable[Any] = (), conn: sqlite3.Connection | None = None
) -> dict[str, Any] | None:
    own = conn is None
    conn = conn or connect()
    try:
        row = conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None
    finally:
        if own:
            conn.close()


def insert(table: str, values: dict[str, Any], conn: sqlite3.Connection) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )


def update(table: str, values: dict[str, Any], where: str, params: Iterable[Any], conn: sqlite3.Connection) -> None:
    assignments = ", ".join(f"{key}=?" for key in values)
    conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {where}",
        tuple(values.values()) + tuple(params),
    )


def json_value(raw: Any, default: Any = None) -> Any:
    if raw in (None, ""):
        return {} if default is None else default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {} if default is None else default


def json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)

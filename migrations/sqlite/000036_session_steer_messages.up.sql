CREATE TABLE IF NOT EXISTS session_steer_messages (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id BIGINT NOT NULL,
    session_id VARCHAR(36) NOT NULL,
    assistant_message_id VARCHAR(36) NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    delivery VARCHAR(16) NOT NULL DEFAULT 'after',
    mentioned_items TEXT NOT NULL DEFAULT '[]',
    channel VARCHAR(32) NOT NULL DEFAULT 'web',
    status VARCHAR(16) NOT NULL DEFAULT 'queued',
    user_message_id VARCHAR(36) NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_steer_messages_run
    ON session_steer_messages (tenant_id, session_id, assistant_message_id, status, created_at);


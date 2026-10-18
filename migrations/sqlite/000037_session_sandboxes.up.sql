CREATE TABLE IF NOT EXISTS py_session_sandboxes (
    tenant_id INTEGER NOT NULL,
    session_id VARCHAR(36) NOT NULL,
    config_id VARCHAR(36) NOT NULL,
    provider VARCHAR(16) NOT NULL,
    sandbox_id VARCHAR(128) NOT NULL,
    traffic_token TEXT NOT NULL DEFAULT '',
    envd_token TEXT NOT NULL DEFAULT '',
    domain VARCHAR(255) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (tenant_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_py_session_sandboxes_sandbox
    ON py_session_sandboxes(provider, sandbox_id);

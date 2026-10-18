CREATE TABLE IF NOT EXISTS py_desktop_last_sandboxes (
    session_id VARCHAR(36) PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    sandbox_id VARCHAR(128) NOT NULL,
    expires_at REAL NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_py_desktop_last_sandboxes_expiry
    ON py_desktop_last_sandboxes(expires_at);

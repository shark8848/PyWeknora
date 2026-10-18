CREATE TABLE IF NOT EXISTS py_sandbox_tickets (
    jti VARCHAR(64) PRIMARY KEY,
    token_type VARCHAR(32) NOT NULL,
    tenant_id INTEGER NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(36) NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_py_sandbox_tickets_expiry
    ON py_sandbox_tickets(expires_at);

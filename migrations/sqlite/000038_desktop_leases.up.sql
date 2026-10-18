CREATE TABLE IF NOT EXISTS py_desktop_leases (
    session_id VARCHAR(36) PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    node_id VARCHAR(64) NOT NULL,
    lease_key VARCHAR(64) NOT NULL,
    lease_until REAL NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_py_desktop_leases_expiry
    ON py_desktop_leases(lease_until);

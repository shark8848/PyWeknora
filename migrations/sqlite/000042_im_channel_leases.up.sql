CREATE TABLE IF NOT EXISTS py_im_channel_leases (
    channel_id VARCHAR(36) PRIMARY KEY,
    lease_key VARCHAR(64) NOT NULL,
    lease_until REAL NOT NULL,
    updated_at DATETIME NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_py_im_channel_leases_expiry
    ON py_im_channel_leases(lease_until);

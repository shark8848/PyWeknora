CREATE TABLE IF NOT EXISTS tenant_skill_guidance (
    tenant_id INTEGER NOT NULL,
    sandbox_config_id VARCHAR(36) NOT NULL,
    skill_id VARCHAR(36) NOT NULL,
    steer_id VARCHAR(36) NOT NULL,
    install_message_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (tenant_id, skill_id, steer_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_skill_guidance_run
    ON tenant_skill_guidance (tenant_id, skill_id, install_message_id, created_at);

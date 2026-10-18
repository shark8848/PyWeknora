"""Runtime configuration for the Python WeKnora implementation."""

from __future__ import annotations

import os
import secrets
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("WEKNORA_DATA_DIR", BASE_DIR / "data")).resolve()
FILES_DIR = Path(os.getenv("WEKNORA_FILES_DIR", DATA_DIR / "files")).resolve()
MIGRATIONS_DIR = Path(os.getenv("WEKNORA_MIGRATIONS_DIR", BASE_DIR / "migrations" / "sqlite"))
WEB_DIR = Path(os.getenv("WEKNORA_WEB_DIR", BASE_DIR / "static" / "web")).resolve()
DB_PATH = Path(os.getenv("WEKNORA_DB_PATH", DATA_DIR / "weknora.db")).resolve()

APP_NAME = "WeKnora"
APP_VERSION = "0.8.2"
API_PREFIX = "/api/v1"

ACCESS_TOKEN_TTL = int(os.getenv("WEKNORA_ACCESS_TOKEN_TTL", str(24 * 3600)))
REFRESH_TOKEN_TTL = int(os.getenv("WEKNORA_REFRESH_TOKEN_TTL", str(30 * 24 * 3600)))

DEFAULT_ADMIN_EMAIL = os.getenv("WEKNORA_ADMIN_EMAIL", "admin@weknora.local")
DEFAULT_ADMIN_PASSWORD = os.getenv("WEKNORA_ADMIN_PASSWORD", "WeKnora@2026")
DEFAULT_ADMIN_USERNAME = os.getenv("WEKNORA_ADMIN_USERNAME", "admin")

REGISTRATION_MODE = os.getenv("WEKNORA_REGISTRATION_MODE", "self_serve")
COMPLEX_PASSWORD = os.getenv("WEKNORA_COMPLEX_PASSWORD", "0").lower() in {"1", "true", "yes"}
LITE_MODE = os.getenv("WEKNORA_LITE", "0").lower() in {"1", "true", "yes"}
ENABLE_CROSS_TENANT_ACCESS = os.getenv(
    "WEKNORA_TENANT_ENABLE_CROSS_TENANT_ACCESS", "0"
).lower() in {"1", "true", "yes", "on"}

# Optional OpenAI-compatible chat endpoint. When unset, the built-in extractive
# answerer keeps the product fully usable offline.
LLM_BASE_URL = os.getenv("WEKNORA_LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.getenv("WEKNORA_LLM_API_KEY", "")
LLM_MODEL = os.getenv("WEKNORA_LLM_MODEL", "gpt-4o-mini")
EMBEDDING_BASE_URL = os.getenv("WEKNORA_EMBEDDING_BASE_URL", "").rstrip("/")
EMBEDDING_API_KEY = os.getenv("WEKNORA_EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.getenv("WEKNORA_EMBEDDING_MODEL", "bge-m3")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FILES_DIR.mkdir(parents=True, exist_ok=True)


def jwt_secret() -> str:
    """Return a stable local secret, generating one on first use."""
    configured = os.getenv("WEKNORA_JWT_SECRET")
    if configured:
        return configured
    ensure_dirs()
    path = DATA_DIR / "secret.key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return value

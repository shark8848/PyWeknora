#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

exec "${PYTHON:-python3}" -m uvicorn app.main:app \
  --host "${WEKNORA_HOST:-0.0.0.0}" \
  --port "${WEKNORA_PORT:-8080}" \
  "$@"

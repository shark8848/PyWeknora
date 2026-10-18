# PyWeKnora

Python implementation of the WeKnora knowledge management and RAG service. The
repository reuses the existing built Web UI in `static/web`; no frontend build is
required to run the application.

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
bash scripts/start.sh
```

Open `http://localhost:8080/`. The same server exposes:

- Web UI: `/` (served from `static/web`)
- Health check: `/health`
- Swagger UI: `/swagger`
- OpenAPI JSON: `/swagger/openapi.json`

The first start creates a SQLite database under `data/` and seeds a local admin
account plus sample knowledge:

```text
admin@weknora.local / WeKnora@2026
```

Set `WEKNORA_ADMIN_EMAIL` and `WEKNORA_ADMIN_PASSWORD` before first start to use
different credentials.

## Configuration

Common environment variables:

- `WEKNORA_DATA_DIR`: data directory, default `data/`
- `WEKNORA_DB_PATH`: SQLite file, default `$WEKNORA_DATA_DIR/weknora.db`
- `WEKNORA_WEB_DIR`: built Web UI directory, default `static/web/`
- `WEKNORA_HOST` / `WEKNORA_PORT`: bind address, defaults `0.0.0.0:8080`
- `WEKNORA_LLM_BASE_URL`, `WEKNORA_LLM_API_KEY`, `WEKNORA_LLM_MODEL`: optional
  OpenAI-compatible chat endpoint
- `WEKNORA_EMBEDDING_BASE_URL`, `WEKNORA_EMBEDDING_API_KEY`,
  `WEKNORA_EMBEDDING_MODEL`: optional OpenAI-compatible embeddings endpoint

Without remote model configuration, retrieval and answers use the built-in
offline keyword/vector implementation.

## Test

```bash
python -m pytest -q tests/test_smoke.py
```

The smoke suite uses a temporary database and covers the existing UI, login,
memory recall, streaming chat, Wiki ingestion, and read-only shared knowledge
bases.

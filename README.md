# PyWeKnora

[中文](#中文) | [English](#english)

> A Python reimplementation of the WeKnora backend, originally written in Go.

---

## 中文

### 项目简介

PyWeKnora 是 [WeKnora](https://github.com/Tencent/WeKnora) 后端服务的 Python
重实现。WeKnora 的原始后端使用 Go 构建；本项目以 Python 和 FastAPI 重新实现其
服务端能力，并尽量保持既有 API 契约、数据语义和前端交互一致。

本仓库复用 WeKnora 已构建的 Web UI（`static/web/`），因此无需重新编译前端即可
启动完整应用。项目目标不是简单代理 Go 服务，而是在 Python 中实现认证、知识管理、
检索、RAG 对话、Agent、MCP、Wiki、数据源同步等后端逻辑。

### 主要能力

- **知识管理**：知识库、文档、分块、FAQ、标签、上传、解析、重解析、复制、移动和批量操作。
- **检索与 RAG**：关键词与向量混合检索、上下文构建、流式对话、引用和推荐问题。
- **账户与权限**：JWT 登录、租户、组织、角色权限、API Key、共享知识库和存储配额。
- **Agent 与工具**：内置/自定义 Agent、工具调用、MCP 服务端、长期记忆和 Wiki 工具。
- **模型集成**：模型目录、多 provider 配置、OpenAI 兼容的 Chat/Embedding 接口，以及未配置远程模型时的本地离线回退。
- **扩展集成**：数据源同步、对象存储、签名文件代理、IM 渠道、Docker/Cube/E2B 沙箱和终端能力。
- **可观测与运维**：健康检查、Swagger/OpenAPI、SQLite 迁移、后台任务恢复和可选 Redis 分布式模型限流。

### 技术架构

| 层次 | 实现 |
| --- | --- |
| HTTP API | FastAPI、Pydantic、Uvicorn |
| 路由与业务 | `app/routers/`、`app/services/` |
| 持久化 | SQLite；`app/db.py` 与 `migrations/sqlite/` |
| 前端 | 复用 `static/web/` 中的已构建 SPA |
| 模型调用 | OpenAI 兼容接口、Anthropic Messages、Ollama 等 provider 适配 |
| 可选依赖 | Redis（跨进程模型并发限流）、对象存储、外部向量库配置 |

默认使用 SQLite，适合本地开发、单机部署和功能验证。API 前缀为 `/api/v1`，业务错误
响应保持 WeKnora/Go 端常见的 JSON envelope 形状。

### 快速开始

要求 Python 3.11 或更高版本。

```bash
git clone git@github.com:shark8848/PyWeknora.git
cd PyWeknora

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

bash scripts/start.sh
```

也可以直接启动：

```bash
python -m app --host 0.0.0.0 --port 8080
```

启动后访问：

- Web UI：<http://localhost:8080/>
- 健康检查：<http://localhost:8080/health>
- Swagger UI：<http://localhost:8080/swagger>
- OpenAPI JSON：<http://localhost:8080/swagger/openapi.json>

首次启动会在 `data/` 下创建 SQLite 数据库，并初始化本地管理员和示例数据：

```text
admin@weknora.local / WeKnora@2026
```

公开部署前请通过 `WEKNORA_ADMIN_EMAIL` 和 `WEKNORA_ADMIN_PASSWORD` 设置新的管理员
凭据，并配置固定的 `WEKNORA_JWT_SECRET`。

### 配置

常用环境变量：

| 变量 | 说明 |
| --- | --- |
| `WEKNORA_HOST` / `WEKNORA_PORT` | 监听地址与端口，默认 `0.0.0.0:8080` |
| `WEKNORA_DATA_DIR` | 数据目录，默认 `data/` |
| `WEKNORA_DB_PATH` | SQLite 文件，默认 `$WEKNORA_DATA_DIR/weknora.db` |
| `WEKNORA_WEB_DIR` | 前端构建目录，默认 `static/web/` |
| `WEKNORA_ADMIN_EMAIL` / `WEKNORA_ADMIN_PASSWORD` | 首次启动时创建的管理员凭据 |
| `WEKNORA_JWT_SECRET` | JWT 签名密钥；未设置时会在数据目录生成并持久化 |
| `WEKNORA_LLM_BASE_URL` / `WEKNORA_LLM_API_KEY` / `WEKNORA_LLM_MODEL` | OpenAI 兼容 Chat 接口 |
| `WEKNORA_EMBEDDING_BASE_URL` / `WEKNORA_EMBEDDING_API_KEY` / `WEKNORA_EMBEDDING_MODEL` | OpenAI 兼容 Embedding 接口 |
| `REDIS_ADDR` | 可选，启用跨进程模型并发限流 |

完整的模型 provider、存储、搜索、SSRF 白名单和其他配置可在管理界面或数据库设置中配置。

### 测试

```bash
pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q app tests
```

只运行核心 smoke 测试：

```bash
python -m pytest -q tests/test_smoke.py
```

测试使用临时数据目录和临时数据库，不会写入真实的 `data/weknora.db`。

### 目录结构

```text
app/
  routers/        HTTP API 路由
  services/       业务逻辑、模型、存储、连接器和后台任务
  main.py         FastAPI 应用入口与静态资源托管
  db.py           SQLite 访问与迁移执行
migrations/sqlite/ 数据库迁移
static/web/       已构建的 WeKnora Web UI
tests/            单元、集成和 API 回归测试
sample-data/      可导入的示例知识数据
scripts/          启动脚本
```

### 与原始 WeKnora 的关系

- 本项目是以 Python 重写后端，不替代或修改原始 Go 仓库。
- Web UI、API 路径和主要响应形状尽量与既有 WeKnora 保持一致，便于前端和调用方迁移。
- 兼容性审计、已实现范围和已知差异记录在 [`Agent.md`](Agent.md) 与 [`process.md`](process.md)。
- 原始 WeKnora 项目的版权与第三方组件许可见 [`LICENSE`](LICENSE) 和 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

---

## English

### Introduction

PyWeKnora is a Python reimplementation of the WeKnora backend. The original
WeKnora backend is written in Go; this project rebuilds its server-side
capabilities in Python and FastAPI while aiming to preserve the existing API
contracts, data semantics, and frontend behavior.

The repository reuses the pre-built WeKnora Web UI in `static/web/`, so the full
application can be started without rebuilding the frontend. This is not a proxy
in front of the Go service: authentication, knowledge management, retrieval,
RAG chat, agents, MCP, Wiki, and datasource synchronization are implemented in
Python.

### Highlights

- **Knowledge management**: knowledge bases, documents, chunks, FAQ, tags, upload, parsing, re-parsing, cloning, moving, and batch operations.
- **Retrieval and RAG**: hybrid keyword/vector retrieval, context construction, streaming chat, citations, and suggested questions.
- **Accounts and access control**: JWT authentication, tenants, organizations, roles, API keys, shared knowledge bases, and storage quotas.
- **Agents and tools**: built-in/custom agents, tool calling, an MCP server, long-term memory, and Wiki tools.
- **Model integration**: model catalog, multiple provider configurations, OpenAI-compatible Chat/Embedding endpoints, and an offline fallback when no remote model is configured.
- **Integrations**: datasource synchronization, object storage, signed file proxying, IM channels, Docker/Cube/E2B sandboxes, and terminal support.
- **Operations**: health checks, Swagger/OpenAPI, SQLite migrations, background task recovery, and optional Redis-based distributed model concurrency limiting.

### Architecture

| Layer | Implementation |
| --- | --- |
| HTTP API | FastAPI, Pydantic, Uvicorn |
| Routing and business logic | `app/routers/`, `app/services/` |
| Persistence | SQLite via `app/db.py` and `migrations/sqlite/` |
| Frontend | Pre-built SPA served from `static/web/` |
| Model access | OpenAI-compatible APIs, Anthropic Messages, Ollama, and provider adapters |
| Optional infrastructure | Redis, object storage, and external vector-store configuration |

SQLite is used by default, making the project suitable for local development,
single-node deployments, and functional validation. The API prefix is
`/api/v1`, and business errors follow the JSON envelope shape used by
WeKnora/Go.

### Quick Start

Python 3.11 or newer is required.

```bash
git clone git@github.com:shark8848/PyWeknora.git
cd PyWeknora

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

bash scripts/start.sh
```

Alternatively:

```bash
python -m app --host 0.0.0.0 --port 8080
```

Endpoints:

- Web UI: <http://localhost:8080/>
- Health check: <http://localhost:8080/health>
- Swagger UI: <http://localhost:8080/swagger>
- OpenAPI JSON: <http://localhost:8080/swagger/openapi.json>

On first start, a SQLite database is created under `data/`, together with a
local administrator and sample data:

```text
admin@weknora.local / WeKnora@2026
```

Before exposing the service publicly, set a new administrator credential with
`WEKNORA_ADMIN_EMAIL` and `WEKNORA_ADMIN_PASSWORD`, and configure a stable
`WEKNORA_JWT_SECRET`.

### Configuration

Common environment variables:

| Variable | Description |
| --- | --- |
| `WEKNORA_HOST` / `WEKNORA_PORT` | Bind address and port, defaults to `0.0.0.0:8080` |
| `WEKNORA_DATA_DIR` | Data directory, defaults to `data/` |
| `WEKNORA_DB_PATH` | SQLite file, defaults to `$WEKNORA_DATA_DIR/weknora.db` |
| `WEKNORA_WEB_DIR` | Built frontend directory, defaults to `static/web/` |
| `WEKNORA_ADMIN_EMAIL` / `WEKNORA_ADMIN_PASSWORD` | Administrator credentials created on first start |
| `WEKNORA_JWT_SECRET` | JWT signing secret; generated and persisted in the data directory when unset |
| `WEKNORA_LLM_BASE_URL` / `WEKNORA_LLM_API_KEY` / `WEKNORA_LLM_MODEL` | OpenAI-compatible Chat endpoint |
| `WEKNORA_EMBEDDING_BASE_URL` / `WEKNORA_EMBEDDING_API_KEY` / `WEKNORA_EMBEDDING_MODEL` | OpenAI-compatible Embedding endpoint |
| `REDIS_ADDR` | Optional; enables cross-process model concurrency limiting |

Additional model providers, storage backends, search providers, SSRF allowlists,
and other settings can be configured through the admin UI or database settings.

### Testing

```bash
pip install -e ".[dev]"
python -m pytest -q
python -m compileall -q app tests
```

Run only the core smoke suite:

```bash
python -m pytest -q tests/test_smoke.py
```

Tests use temporary data directories and databases; they do not write to the
real `data/weknora.db`.

### Project Layout

```text
app/
  routers/       HTTP API routes
  services/      Business logic, models, storage, connectors, and workers
  main.py        FastAPI entry point and static asset hosting
  db.py          SQLite access and migration runner
migrations/sqlite/ SQLite migrations
static/web/       Pre-built WeKnora Web UI
tests/            Unit, integration, and API regression tests
sample-data/      Importable sample knowledge data
scripts/          Startup scripts
```

### Relationship to WeKnora

- This project is a Python backend rewrite; it does not replace or modify the original Go repository.
- Web UI routes, API paths, and major response shapes are kept close to WeKnora to simplify frontend and client migration.
- Compatibility audits, implemented scope, and known differences are documented in [`Agent.md`](Agent.md) and [`process.md`](process.md).
- Copyright and third-party component terms are covered by [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

"""FastAPI application factory and static Web UI hosting."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .deps import parse_auth
from .errors import AppError, app_error_handler, not_found, validation
from .routers import agents, auth, chat, embed, infra, knowledge, mcp_endpoint, memory, misc, organizations, system, tenants, wiki
from .services import browser_skill, file_proxy, im_runtime


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    db.init_db()
    knowledge.recover_pending_task_workers()
    await im_runtime.start()
    try:
        yield
    finally:
        await im_runtime.shutdown()
        browser_skill.shutdown()


app = FastAPI(
    title="PyWeKnora API",
    version=config.APP_VERSION,
    docs_url="/swagger",
    redoc_url="/swagger/redoc",
    openapi_url="/swagger/openapi.json",
    lifespan=lifespan,
)

app.add_exception_handler(AppError, app_error_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

for router in (
    auth.router,
    tenants.router,
    organizations.router,
    knowledge.router,
    chat.router,
    embed.router,
    memory.router,
    agents.router,
    infra.router,
    system.router,
    wiki.router,
    misc.router,
):
    app.include_router(router, prefix=config.API_PREFIX)

app.include_router(mcp_endpoint.router)


@app.get("/health", include_in_schema=False)
@app.get(f"{config.API_PREFIX}/health", include_in_schema=False)
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "success": True,
        "service": config.APP_NAME,
        "version": config.APP_VERSION,
        "database": str(config.DB_PATH),
    }


@app.get("/files", include_in_schema=False)
def tenant_file(request: Request):
    ctx = parse_auth(request)
    assert ctx is not None
    reference = str(request.query_params.get("file_path") or "").strip()
    if not reference:
        raise validation("missing required parameter: file_path")
    return file_proxy.file_response(reference, ctx.tenant_id)


def _safe_web_path(path: str) -> Path | None:
    root = config.WEB_DIR.resolve()
    candidate = (root / path).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


if config.WEB_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=config.WEB_DIR / "assets"), name="assets")
    icons = config.WEB_DIR / "tdesign-icons"
    if icons.is_dir():
        app.mount("/tdesign-icons", StaticFiles(directory=icons), name="tdesign-icons")

config.FILES_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/{path:path}", include_in_schema=False)
def web_ui(path: str, request: Request):
    if path.startswith("api/") or path == "api":
        raise not_found("API endpoint not found")
    if path == "files" or path.startswith("files/"):
        raise not_found("File endpoint not found")
    candidate = _safe_web_path(path) if path else config.WEB_DIR / "index.html"
    if candidate and candidate.is_file():
        return FileResponse(candidate)
    index = config.WEB_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        {
            "success": False,
            "message": "Web UI is not installed. Set WEKNORA_WEB_DIR or place the built UI in static/web.",
        },
        status_code=404,
    )

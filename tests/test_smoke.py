"""End-to-end smoke tests for the Python WeKnora server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import io
import os
import re
import secrets
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.parse
import uuid
import zipfile
from pathlib import Path

_DATA_DIR = tempfile.mkdtemp(prefix="pyweknora-tests.")
os.environ["WEKNORA_DATA_DIR"] = _DATA_DIR

import anyio
import anyio.to_thread
import fastapi.routing
import httpx
import pytest


async def _run_sync(func, *args, **kwargs):
    kwargs.pop("limiter", None)
    return func(*args, **kwargs)


async def _direct(func, *args, **kwargs):
    return func(*args, **kwargs)


# The sandbox does not allow the worker-thread pool used by Starlette for sync
# endpoints. Run them inline so the ASGI application remains testable.
anyio.to_thread.run_sync = _run_sync
fastapi.routing.run_in_threadpool = _direct

from app import db  # noqa: E402
from app import config as app_config  # noqa: E402
from app.main import app  # noqa: E402
from app.security import decode_token, issue_token  # noqa: E402
from app.services import agent_tools, agents, approvals, browser_skill  # noqa: E402
from app.services import wiki_agent_tools  # noqa: E402
from app.services import docker_sandbox  # noqa: E402
from app.services import docker_templates  # noqa: E402
from app.services import llm as llm_service  # noqa: E402
from app.services import mcp as mcp_client  # noqa: E402
from app.services import mcp_oauth  # noqa: E402
from app.services import im as im_service  # noqa: E402
from app.services import im_attachments, im_handler  # noqa: E402
from app.services import remote_sandbox  # noqa: E402
from app.services.sandbox_terminal import SandboxTerminalClosed  # noqa: E402
from app.services import session_sandbox  # noqa: E402
from app.services import wechat as wechat_service  # noqa: E402
from app.routers import infra as infra_router  # noqa: E402
from app.routers import knowledge as knowledge_router  # noqa: E402
from app.routers import system as system_router  # noqa: E402
from app.routers import chat as chat_router  # noqa: E402
from app.routers import wiki as wiki_router  # noqa: E402
from app.services import web_search as web_search_service  # noqa: E402
from app.services import wiki_ingest  # noqa: E402


ADMIN = {"email": "admin@weknora.local", "password": "WeKnora@2026"}

RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>PyWeKnora test feed</title>
    <link>http://example.test/</link>
    <description>Feed used by the smoke tests</description>
    <item>
      <guid>article-1</guid>
      <title>Release notes</title>
      <link>http://example.test/articles/1</link>
      <description><![CDATA[<p>The data source sync writes this article into the knowledge base.</p>]]></description>
    </item>
  </channel>
</rss>
"""

HTML_PAGE = """<!doctype html>
<html>
  <head><title>Operations page</title></head>
  <body>
    <script>ignored()</script>
    <main><h1>Operations</h1><p>Check the health endpoint before reading this page.</p></main>
  </body>
</html>
"""


def _docx_with_image(title: str, caption: str, image: bytes) -> bytes:
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>{title}</w:t></w:r></w:p>
    <w:p><w:r><w:drawing><wp:inline>
      <wp:docPr id="1" name="Picture 1" descr="{caption}"/>
      <a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic>
    </wp:inline></w:drawing></w:r></w:p>
  </w:body>
</w:document>
""".encode()
    relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/chart.png"/>
</Relationships>
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/media/chart.png", image)
    return output.getvalue()


def _pdf_with_image() -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (12, 12), (10, 20, 30)).save(output, format="PDF")
    return output.getvalue()


def _wav() -> bytes:
    import wave

    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\x00\x00" * 400)
    return output.getvalue()


def _run(case):
    db.init_db()
    return anyio.run(case)


async def _client():
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


async def _drain_im_background_tasks() -> None:
    while im_handler._background_tasks:
        await asyncio.gather(
            *list(im_handler._background_tasks), return_exceptions=True
        )
        await asyncio.sleep(0)


async def _login(client: httpx.AsyncClient, credentials: dict[str, str] = ADMIN) -> dict:
    response = await client.post("/api/v1/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    return response.json()


def _headers(login: dict) -> dict[str, str]:
    return {"Authorization": "Bearer " + login["token"]}


def _api_key_headers(token: str, tenant_id: int | None = None) -> dict[str, str]:
    headers = {"X-API-Key": token}
    if tenant_id is not None:
        headers["X-Tenant-ID"] = str(tenant_id)
    return headers


async def _login_tenant_member(
    client: httpx.AsyncClient, tenant_id: int, role: str = "viewer"
) -> dict[str, str]:
    suffix = uuid.uuid4().hex[:10]
    credentials = {
        "username": role + "-" + suffix,
        "email": f"{role}-{suffix}@example.com",
        "password": "ViewerPass123",
    }
    registered = await client.post("/api/v1/auth/register", json=credentials)
    assert registered.status_code == 201, registered.text
    user_id = registered.json()["user"]["id"]
    with db.transaction() as conn:
        conn.execute("UPDATE users SET tenant_id=? WHERE id=?", (tenant_id, user_id))
        conn.execute(
            "UPDATE tenant_members SET tenant_id=?, role=? WHERE user_id=?",
            (tenant_id, role, user_id),
        )
    login = await _login(
        client, {"email": credentials["email"], "password": credentials["password"]}
    )
    assert login["active_tenant"]["id"] == tenant_id
    return _headers(login)


async def _login_viewer(client: httpx.AsyncClient, tenant_id: int) -> dict[str, str]:
    return await _login_tenant_member(client, tenant_id, "viewer")


def _events(response: httpx.Response) -> list[dict]:
    events = []
    for block in response.text.split("\n\n"):
        data = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), "")
        if data:
            events.append(json.loads(data))
    return events


async def _wait_for_pending(kind: str):
    for _ in range(500):
        pending = next(
            (
                item
                for item in approvals._pending.values()
                if item.kind == kind and not item.future.done()
            ),
            None,
        )
        if pending is not None:
            return pending
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {kind} approval")


async def _wait_for_summary(
    knowledge_id: str,
    *,
    status: set[str] | None = None,
    description: str | None = None,
) -> dict:
    for _ in range(500):
        row = db.fetch_one(
            "SELECT description, summary_status FROM knowledges WHERE id=?",
            (knowledge_id,),
        )
        if (
            row
            and (status is None or row["summary_status"] in status)
            and (description is None or row["description"] == description)
        ):
            return row
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for summary of {knowledge_id}")


def test_registration_modes_apply_immediately_and_gate_both_entrypoints() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            previous = db.fetch_one(
                "SELECT * FROM system_settings WHERE key='auth.registration_mode'"
            )
            token = uuid.uuid4().hex
            now = db.utcnow()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO tenant_invitations(tenant_id, invitee_user_id, invited_by, role, status, expires_at, token, created_at, updated_at) VALUES (?, '', ?, 'viewer', 'pending', ?, ?, ?, ?)",
                    (tenant_id, login["user"]["id"], now, token, now, now),
                )
            try:
                invalid = await client.put(
                    "/api/v1/system/admin/settings/auth.registration_mode",
                    headers=headers,
                    json={"value": "closed"},
                )
                assert invalid.status_code == 400, invalid.text

                for mode in ("self_serve", "invite_register", "invite_only"):
                    updated = await client.put(
                        "/api/v1/system/admin/settings/auth.registration_mode",
                        headers=headers,
                        json={"value": mode},
                    )
                    assert updated.status_code == 200, updated.text
                    public_config = await client.get("/api/v1/auth/config")
                    assert public_config.json()["registration_mode"] == mode

                    suffix = uuid.uuid4().hex[:10]
                    public = await client.post(
                        "/api/v1/auth/register",
                        json={
                            "username": "public-" + suffix,
                            "email": f"public-{suffix}@example.com",
                            "password": "PublicPass123",
                        },
                    )
                    assert public.status_code == (201 if mode == "self_serve" else 403)

                    invited = await client.post(
                        "/api/v1/auth/register-by-invite",
                        json={
                            "token": token if mode == "invite_register" else "missing",
                            "username": "invited-" + suffix,
                            "email": f"invited-{suffix}@example.com",
                            "password": "InvitedPass123",
                        },
                    )
                    expected = 403 if mode == "invite_only" else (201 if mode == "invite_register" else 410)
                    assert invited.status_code == expected, invited.text
            finally:
                with db.transaction() as conn:
                    if previous:
                        conn.execute(
                            "UPDATE system_settings SET value=? WHERE key='auth.registration_mode'",
                            (previous["value"],),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM system_settings WHERE key='auth.registration_mode'"
                        )

    _run(case)


def test_existing_web_ui_and_login() -> None:
    async def case() -> None:
        async with await _client() as client:
            index = await client.get("/")
            assert index.status_code == 200
            assert 'id="app"' in index.text
            assert "/assets/main-" in index.text

            config = await client.get("/config.js")
            assert config.status_code == 200
            assert "__RUNTIME_CONFIG__" in config.text

            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json()["status"] == "ok"

            login = await _login(client)
            assert login["user"]["email"] == ADMIN["email"]
            assert login["token"] and login["active_tenant"]["id"]

            me = await client.get("/api/v1/auth/me", headers=_headers(login))
            assert me.status_code == 200
            assert me.json()["data"]["user"]["email"] == ADMIN["email"]

            knowledge_bases = await client.get("/api/v1/knowledge-bases", headers=_headers(login))
            assert knowledge_bases.status_code == 200
            assert knowledge_bases.json()["data"]

    _run(case)


def test_go_wire_shapes_for_core_surfaces(monkeypatch) -> None:
    monkeypatch.delenv("BROWSERSKILL_BINARY", raising=False)

    async def case() -> None:
        async with await _client() as client:
            anonymous = await client.get("/api/v1/me/browser")
            assert anonymous.status_code == 401
            assert anonymous.json() == {"error": "Unauthorized: missing authentication"}

            login = await _login(client)
            headers = _headers(login)
            assert login["user"]["can_access_all_tenants"] is False
            assert "role" not in login["active_tenant"]

            me = await client.get("/api/v1/auth/me", headers=headers)
            assert me.status_code == 200, me.text
            payload = me.json()["data"]
            assert set(payload["user"]) == {
                "id", "username", "email", "avatar", "tenant_id", "is_active",
                "can_access_all_tenants", "is_system_admin", "preferences",
                "created_at", "updated_at",
            }
            assert payload["preference_defaults"]["browser_search_instructions"] == (
                "Default search engine: Bing.\n"
                "Search URL: https://www.bing.com/search?q={query}"
            )
            assert set(payload["tenant"]) == {
                "id", "name", "description", "status", "retriever_engines", "business",
                "storage_quota", "storage_used", "context_config", "created_at",
                "updated_at", "deleted_at",
            }
            assert payload["tenant"]["retriever_engines"] == {"engines": []}

            browser = await client.get("/api/v1/me/browser", headers=headers)
            assert browser.status_code == 200, browser.text
            assert set(browser.json()["data"]) == {
                "extension_available", "action_elapsed_ms", "stopping", "idle",
                "needs_help", "enabled", "selected", "connected", "paused",
            }
            disabled = await client.post(
                "/api/v1/me/browser", headers=headers, json={"action": "bogus"}
            )
            assert disabled.status_code == 503
            assert disabled.json() == {"error": "local browser is unavailable"}

            for path in ("/api/v1/system/info", "/api/v1/system/capabilities"):
                response = await client.get(path, headers=headers)
                assert response.status_code == 200, response.text
                assert set(response.json()) == {"code", "msg", "data"}
                assert response.json()["code"] == 0
                assert response.json()["msg"] == "success"
            info = await client.get("/api/v1/system/info", headers=headers)
            assert "python_version" not in info.json()["data"]
            assert "db_migration_error" not in info.json()["data"]

            parsers = await client.get("/api/v1/system/parser-engines", headers=headers)
            assert all("UnavailableReason" in engine for engine in parsers.json()["data"])

            admins = await client.get("/api/v1/system/admin/list", headers=headers)
            assert set(admins.json()) == {"admins", "total"}
            users = await client.get("/api/v1/system/admin/users", headers=headers)
            assert "success" not in users.json()
            settings = await client.get("/api/v1/system/admin/settings", headers=headers)
            assert isinstance(settings.json(), list)
            audit = await client.get("/api/v1/system/admin/audit-log", headers=headers)
            assert set(audit.json()) == {"success", "data", "next_cursor"}

            tenants = await client.get("/api/v1/tenants", headers=headers)
            assert set(tenants.json()["data"]) == {"items"}
            for path in ("/api/v1/tenants/all", "/api/v1/tenants/search"):
                denied = await client.get(path, headers=headers)
                assert denied.status_code == 403, denied.text
                assert denied.json() == {
                    "success": False,
                    "error": {
                        "code": 1002,
                        "message": "Cross-workspace access is disabled",
                        "details": None,
                    },
                }
            sessions = await client.get("/api/v1/sessions", headers=headers)
            assert set(sessions.json()) == {"success", "data", "total", "page", "page_size"}
            invitations = await client.get("/api/v1/me/invitations", headers=headers)
            assert set(invitations.json()["data"]) == {"invitations", "total"}

            invalid = await client.post("/api/v1/tenants", headers=headers, json={})
            assert invalid.status_code == 400
            assert invalid.json() == {
                "success": False,
                "error": {"code": 1010, "message": "Workspace name is required", "details": None},
            }

    _run(case)


def test_system_user_status_contract_and_guards(monkeypatch) -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            admin_id = str(login["user"]["id"])
            tenant_id = int(login["active_tenant"]["id"])
            endpoint = "/api/v1/system/admin/users/status"

            self_disable = await client.post(
                endpoint, headers=headers, json={"user_id": admin_id, "is_active": False}
            )
            assert self_disable.status_code == 400, self_disable.text
            assert self_disable.json() == {"error": "cannot disable your own account"}

            self_enable = await client.post(
                endpoint, headers=headers, json={"user_id": admin_id, "is_active": True}
            )
            assert self_enable.status_code == 200, self_enable.text
            assert set(self_enable.json()) == {"message", "user", "is_active"}
            assert self_enable.json()["user"]["id"] == admin_id
            assert self_enable.json()["is_active"] is True

            suffix = uuid.uuid4().hex[:10]
            credentials = {
                "username": "status-" + suffix,
                "email": f"status-{suffix}@example.com",
                "password": "StatusPass123",
            }
            registered = await client.post("/api/v1/auth/register", json=credentials)
            assert registered.status_code == 201, registered.text
            target_id = str(registered.json()["user"]["id"])
            target_login = await _login(
                client, {"email": credentials["email"], "password": credentials["password"]}
            )
            target_headers = _headers(target_login)

            listing = await client.get(
                "/api/v1/system/admin/users?offset=bad&limit=bad", headers=headers
            )
            assert listing.status_code == 200, listing.text
            assert set(listing.json()) == {"users", "offset", "limit", "has_more"}
            assert listing.json()["offset"] == 0
            assert listing.json()["limit"] == 50
            assert listing.json()["users"]
            assert all("password_hash" not in user for user in listing.json()["users"])
            capped = await client.get(
                "/api/v1/system/admin/users?limit=999", headers=headers
            )
            assert capped.status_code == 200, capped.text
            assert capped.json()["limit"] == 200
            first_page = await client.get(
                "/api/v1/system/admin/users?limit=1", headers=headers
            )
            assert first_page.status_code == 200, first_page.text
            assert len(first_page.json()["users"]) == 1
            assert first_page.json()["has_more"] is True

            for payload in (
                {"is_active": True},
                {"user_id": target_id},
                {"user_id": target_id, "is_active": None},
                {"user_id": target_id, "is_active": "false"},
                {"user_id": 123, "is_active": True},
            ):
                invalid = await client.post(endpoint, headers=headers, json=payload)
                assert invalid.status_code == 400, invalid.text
                assert invalid.json() == {"error": "Invalid request"}
            still_active = await client.get("/api/v1/auth/me", headers=target_headers)
            assert still_active.status_code == 200, still_active.text

            missing = await client.post(
                endpoint, headers=headers, json={"user_id": "missing-user", "is_active": True}
            )
            assert missing.status_code == 404, missing.text
            assert missing.json() == {"error": "User not found"}

            original_admin_ids = {
                str(row["id"])
                for row in db.fetch_all(
                    "SELECT id FROM users WHERE is_system_admin=1 AND deleted_at IS NULL", ()
                )
            }
            try:
                with db.transaction() as conn:
                    conn.execute("UPDATE users SET is_system_admin=0 WHERE deleted_at IS NULL")
                    conn.execute("UPDATE users SET is_system_admin=1 WHERE id=?", (target_id,))
                with monkeypatch.context() as patch:
                    patch.setattr(
                        system_router,
                        "_system_admin",
                        lambda request: SimpleNamespace(
                            user_id="last-admin-actor", tenant_id=tenant_id, role="owner"
                        ),
                    )
                    last_admin = await client.post(
                        endpoint,
                        headers=headers,
                        json={"user_id": target_id, "is_active": False},
                    )
                    assert last_admin.status_code == 400, last_admin.text
                    assert last_admin.json() == {
                        "error": "cannot disable the last system administrator"
                    }
            finally:
                with db.transaction() as conn:
                    conn.execute("UPDATE users SET is_system_admin=0 WHERE deleted_at IS NULL")
                    for admin_user_id in original_admin_ids:
                        conn.execute(
                            "UPDATE users SET is_system_admin=1 WHERE id=?", (admin_user_id,)
                        )
            disabled = await client.post(
                endpoint, headers=headers, json={"user_id": target_id, "is_active": False}
            )
            assert disabled.status_code == 200, disabled.text
            assert set(disabled.json()) == {"message", "user", "is_active"}
            assert disabled.json()["message"] == "User status updated"
            assert disabled.json()["is_active"] is False
            assert disabled.json()["user"]["id"] == target_id
            assert disabled.json()["user"]["is_active"] is False
            assert "password_hash" not in disabled.json()["user"]

            revoked = await client.get("/api/v1/auth/me", headers=target_headers)
            assert revoked.status_code == 401, revoked.text
            reenabled = await client.post(
                endpoint, headers=headers, json={"user_id": target_id, "is_active": True}
            )
            assert reenabled.status_code == 200, reenabled.text
            assert reenabled.json()["is_active"] is True
            still_revoked = await client.get("/api/v1/auth/me", headers=target_headers)
            assert still_revoked.status_code == 401, still_revoked.text

    _run(case)


def test_builtin_agents_match_go_catalog() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])

            listed = await client.get("/api/v1/agents", headers=headers)
            assert listed.status_code == 200, listed.text
            data = listed.json()["data"]
            assert [item["id"] for item in data] == [
                "builtin-quick-answer",
                "builtin-smart-reasoning",
                "builtin-wiki-researcher",
                "builtin-data-analyst",
            ]
            assert all(item["tenant_id"] == tenant_id for item in data)

            config_keys = {
                "agent_mode", "system_prompt", "context_template", "model_id",
                "rerank_model_id", "temperature", "max_completion_tokens", "thinking",
                "citation_enabled", "max_iterations", "allowed_tools",
                "mcp_selection_mode", "mcp_services", "skills_selection_mode",
                "selected_skills", "kb_selection_mode", "knowledge_bases",
                "retrieve_kb_only_when_mentioned", "retain_retrieval_history",
                "image_upload_enabled", "vlm_model_id", "audio_upload_enabled",
                "asr_model_id", "image_storage_provider", "supported_file_types",
                "attachment_image_understanding", "data_analysis_enabled",
                "faq_priority_enabled", "faq_direct_answer_threshold",
                "faq_score_boost", "web_search_enabled", "web_search_max_results",
                "web_fetch_enabled", "multi_turn_enabled", "history_turns",
                "embedding_top_k", "keyword_threshold", "vector_threshold",
                "rerank_top_k", "rerank_threshold", "enable_query_expansion",
                "enable_rewrite", "rewrite_prompt_system", "rewrite_prompt_user",
                "fallback_strategy", "fallback_response", "fallback_prompt",
                "question_suggestions",
            }
            by_id = {item["id"]: item for item in data}
            assert set(by_id["builtin-quick-answer"]["config"]) == config_keys | {
                "system_prompt_id", "context_template_id"
            }
            assert set(by_id["builtin-smart-reasoning"]["config"]) == config_keys | {"agent_type"}
            quick = by_id["builtin-quick-answer"]["config"]
            assert quick["max_iterations"] == 10
            assert quick["allowed_tools"] is None
            assert quick["system_prompt"].startswith("You are WeKnora")
            assert quick["context_template"].startswith("## Reference materials")
            assert set(quick["question_suggestions"]) == {"starters", "follow_ups"}

            assert by_id["builtin-smart-reasoning"]["config"]["max_iterations"] == 50
            assert by_id["builtin-wiki-researcher"]["config"]["allowed_tools"] == [
                "wiki_search", "wiki_read_page", "read_document", "wiki_flag_issue"
            ]
            assert by_id["builtin-data-analyst"]["config"]["supported_file_types"] == ["csv", "xlsx"]

            english = await client.get(
                "/api/v1/agents",
                headers={**headers, "Accept-Language": "en-US,en;q=0.9"},
            )
            assert english.status_code == 200, english.text
            english_quick = next(
                item for item in english.json()["data"]
                if item["id"] == "builtin-quick-answer"
            )
            assert english_quick["name"] == "Quick Answer"
            assert english_quick["description"] == (
                "Knowledge base RAG Q&A for fast and accurate answers"
            )

            for internal_id in ("builtin-wiki-fixer", "builtin-skill-installer"):
                internal = await client.get(f"/api/v1/agents/{internal_id}", headers=headers)
                assert internal.status_code == 200, internal.text
                assert internal.json()["data"]["tenant_id"] == tenant_id
            assert "builtin-wiki-fixer" not in {item["id"] for item in data}
            assert "builtin-skill-installer" not in {item["id"] for item in data}

    _run(case)


def test_model_provider_catalog_matches_go() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            response = await client.get("/api/v1/models/providers", headers=headers)
            assert response.status_code == 200, response.text
            providers = response.json()["data"]
            assert [item["value"] for item in providers] == [
                "generic", "weknoracloud", "aliyun", "zhipu", "volcengine",
                "hunyuan", "siliconflow", "minimax", "moonshot", "mimo",
                "modelscope", "qianfan", "qiniu", "longcat", "lkeap",
                "deepseek", "openai", "azure_openai", "anthropic", "gemini",
                "openrouter", "litellm", "requesty", "jina", "nvidia",
                "novita", "pinecone", "gpustack", "huggingface_tei",
            ]
            required_keys = {
                "value", "label", "description",
                "icon", "api", "auth", "requiresAuth", "defaultUrls",
                "modelTypes", "thinking", "order",
            }
            optional_keys = {
                "labels", "descriptions", "extraFields", "website", "models",
                "credentialLabels",
            }
            assert all(
                required_keys <= set(item) <= required_keys | optional_keys
                for item in providers
            )
            default_url_keys = {"chat", "embedding", "rerank", "vllm", "asr"}
            assert all(
                set(item["defaultUrls"]) <= default_url_keys
                for item in providers
            )

            by_id = {item["value"]: item for item in providers}
            assert set(by_id["generic"]["defaultUrls"]) == default_url_keys
            assert by_id["generic"]["labels"] == {"zh-CN": "自定义 (OpenAI兼容接口)"}
            assert by_id["openai"]["defaultUrls"]["chat"] == "https://api.openai.com/v1"
            assert by_id["pinecone"]["modelTypes"] == ["rerank"]
            assert by_id["azure_openai"]["extraFields"][0]["key"] == "api_version"
            assert by_id["aliyun"]["models"]
            assert by_id["volcengine"]["credentialLabels"]

            chat = await client.get("/api/v1/models/providers?model_type=chat", headers=headers)
            assert {"generic", "openai", "anthropic"} <= {
                item["value"] for item in chat.json()["data"]
            }
            rerank = await client.get("/api/v1/models/providers?model_type=rerank", headers=headers)
            assert {"generic", "jina", "pinecone"} <= {
                item["value"] for item in rerank.json()["data"]
            }

    _run(case)


def test_model_catalog_resolution_and_anthropic_messages(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []
    image = "data:image/png;base64," + base64.b64encode(b"png").decode()

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else None
        calls.append({"url": url, "headers": dict(headers or {}), "body": payload})
        if len(calls) == 1:
            response = {
                "id": "msg-1",
                "content": [
                    {"type": "text", "text": "checking"},
                    {"type": "tool_use", "id": "tool-1", "name": "knowledge_search", "input": {"query": "x"}},
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 2,
                    "cache_creation_input_tokens": 7,
                },
            }
        else:
            response = {
                "id": "msg-2",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
        return 200, {}, json.dumps(response).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            resolved = await client.get(
                "/api/v1/models/catalog/resolve",
                headers=headers,
                params={"provider": "anthropic", "model": "claude-sonnet-5-5", "model_type": "chat"},
            )
            assert resolved.status_code == 200, resolved.text
            data = resolved.json()["data"]
            assert data["cataloged"] is True
            assert data["api"] == "anthropic-messages"
            assert data["base_url"] == "https://api.anthropic.com/v1"
            assert data["remote_model"] == "claude-sonnet-5-5"
            assert data["model"]["type"] == "KnowledgeQA"
            assert data["model"]["context_window"] == 1_000_000
            assert data["model"]["max_output_tokens"] == 128_000
            assert data["capabilities"]["cataloged"] is True
            assert data["capabilities"]["input"] == ["text", "image"]
            assert data["capabilities"]["thinking_levels"] == [
                "off", "auto", "minimal", "low", "medium", "high", "xhigh", "max"
            ]
            assert data["capabilities"]["max_tokens_field"] == "max_tokens"
            assert data["model"]["compat"] == {
                "supports_temperature": False,
                "supports_top_p": False,
                "thinking_mode": "adaptive",
                "supports_effort": True,
                "thinking_off_type": "between_tools",
            }

            overridden = await client.post(
                "/api/v1/models/catalog/resolve",
                headers=headers,
                json={
                    "provider": "anthropic",
                    "model": "claude-sonnet-5-5",
                    "model_type": "chat",
                    "spec": {"context_window": 12345},
                },
            )
            assert overridden.status_code == 200, overridden.text
            assert overridden.json()["data"]["capabilities"]["context_window"] == 12345

            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "claude-sonnet-5-5",
                    "display_name": "Claude Sonnet 5.5",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {
                        "provider": "anthropic",
                        "base_url": "https://api.anthropic.com/v1",
                        "api_key": "anthropic-key",
                    },
                },
            )
            assert created.status_code == 201, created.text
            saved_capabilities = created.json()["data"]["capabilities"]
            assert saved_capabilities["cataloged"] is True
            assert saved_capabilities["api"] == "anthropic-messages"
            assert saved_capabilities["context_window"] == 1_000_000
            with db.transaction() as conn:
                conn.execute("DELETE FROM models WHERE id=?", (created.json()["data"]["id"],))

            runtime = {
                "name": "claude-sonnet-5-5",
                "provider": "anthropic",
                "api": "anthropic-messages",
                "base_url": "https://api.anthropic.com/v1",
                "api_key": "anthropic-key",
                "headers": {"x-api-key": "anthropic-key"},
            }
            result = model_runtime.chat_completion(
                runtime,
                [
                    {"role": "system", "content": "system prompt"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {"type": "image_url", "image_url": {"url": image}},
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "previous",
                        "tool_calls": [{
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "knowledge_search", "arguments": '{"query":"old"}'},
                        }],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "content": "tool output"},
                ],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "knowledge_search",
                        "description": "search",
                        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                    },
                }],
                temperature=0.3,
                top_p=0.9,
                max_tokens=64,
            )
            assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
            assert calls[0]["headers"]["x-api-key"] == "anthropic-key"
            assert calls[0]["headers"]["anthropic-version"] == "2023-06-01"
            body = calls[0]["body"]
            assert body["system"] == "system prompt"
            assert body["max_tokens"] == 64
            assert "temperature" not in body and "top_p" not in body
            assert body["messages"][0]["content"] == [
                {"type": "text", "text": "look"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": image.split(",", 1)[1]},
                },
            ]
            assert body["messages"][1]["content"][1] == {
                "type": "tool_use", "id": "call-1", "name": "knowledge_search", "input": {"query": "old"}
            }
            assert body["messages"][2]["content"] == [{
                "type": "tool_result", "tool_use_id": "call-1", "content": "tool output"
            }]
            assert body["tools"][0]["input_schema"]["properties"]["query"]["type"] == "string"
            assert result["choices"][0]["message"]["content"] == "checking"
            assert result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"query": "x"}'
            assert result["choices"][0]["finish_reason"] == "tool_calls"
            assert result["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}

            model_runtime.chat_completion(
                {**runtime, "name": "claude-sonnet-4-6"},
                [{"role": "user", "content": "hello"}],
                temperature=0.3,
            )
            assert calls[1]["body"]["temperature"] == 0.3

    _run(case)


def test_anthropic_catalog_compat_controls_adaptive_runtime(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []

    def fake_http(_method, _url, *, headers=None, body=None, timeout=120):
        calls.append(json.loads(body.decode()) if body else {})
        return 200, {}, json.dumps({
            "id": "msg-catalog",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    def runtime(model: str, effort: str) -> dict:
        return model_runtime.runtime_config(
            {
                "id": "catalog-model",
                "name": model,
                "type": "KnowledgeQA",
                "source": "remote",
                "parameters": {
                    "provider": "anthropic",
                    "base_url": "https://api.anthropic.com/v1",
                    "reasoning_effort": effort,
                },
            },
            "KnowledgeQA",
        )

    model_runtime.chat_completion(
        runtime("claude-sonnet-5-5", "high"),
        [{"role": "user", "content": "Hi"}],
        temperature=0.3,
        top_p=0.9,
        max_tokens=64,
    )
    assert calls[0]["thinking"] == {"type": "adaptive"}
    assert calls[0]["output_config"] == {"effort": "high"}
    assert "temperature" not in calls[0] and "top_p" not in calls[0]

    model_runtime.chat_completion(
        runtime("claude-sonnet-5-5", "off"),
        [{"role": "user", "content": "Hi"}],
        max_tokens=64,
    )
    assert calls[1]["thinking"] == {"type": "between_tools"}
    assert "output_config" not in calls[1]

    model_runtime.chat_completion(
        runtime("claude-opus-5-5", "off"),
        [{"role": "user", "content": "Hi"}],
        max_tokens=64,
    )
    assert "thinking" not in calls[2]
    assert "output_config" not in calls[2]


def test_anthropic_thinking_request_knobs_and_reasoning_replay(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else None
        calls.append({"url": url, "headers": dict(headers or {}), "body": payload})
        if len(calls) == 1:
            response = {
                "id": "msg-thinking",
                "content": [
                    {"type": "thinking", "thinking": "plan the call", "signature": "sig-1"},
                    {"type": "text", "text": "calling"},
                    {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {"q": "x"}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        else:
            response = {
                "id": f"msg-{len(calls)}",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        return 200, {}, json.dumps(response).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)
    budget_runtime = {
        "name": "claude-sonnet-4-6",
        "api": "anthropic-messages",
        "base_url": "https://api.anthropic.com/v1",
        "headers": {"x-api-key": "anthropic-key"},
        "reasoning_effort": "high",
        "max_tokens": 1000,
    }
    result = model_runtime.chat_completion(
        budget_runtime,
        [{"role": "user", "content": "use a tool"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "lookup",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }],
        temperature=0.3,
    )
    assert calls[0]["body"]["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert calls[0]["body"]["max_tokens"] == 17408
    assert "temperature" not in calls[0]["body"]
    message = result["choices"][0]["message"]
    assert message["reasoning_content"] == "plan the call"
    assert message["reasoning_metadata"] == {
        "anthropic_thinking_blocks": [
            {"type": "thinking", "thinking": "plan the call", "signature": "sig-1"}
        ]
    }

    model_runtime.chat_completion(
        {
            "name": "claude-sonnet-5-5",
            "api": "anthropic-messages",
            "base_url": "https://api.anthropic.com/v1",
            "headers": {"x-api-key": "anthropic-key"},
            "reasoning_effort": "low",
            "thinking_levels": ["off", "auto", "minimal", "low", "medium", "high"],
            "compat": {"thinking_mode": "adaptive", "supports_effort": True},
        },
        [{"role": "user", "content": "think adaptively"}],
        max_tokens=64,
    )
    assert calls[1]["body"]["thinking"] == {"type": "adaptive"}
    assert calls[1]["body"]["output_config"] == {"effort": "low"}
    assert calls[1]["body"]["max_tokens"] == 64

    model_runtime.chat_completion(
        budget_runtime,
        [
            message,
            {"role": "tool", "tool_call_id": "tool-1", "content": "tool result"},
        ],
        max_tokens=64,
    )
    replay = calls[2]["body"]["messages"][0]["content"]
    assert replay[0] == {"type": "thinking", "thinking": "plan the call", "signature": "sig-1"}
    assert replay[1] == {"type": "text", "text": "calling"}
    assert replay[2]["type"] == "tool_use"
    assert calls[2]["body"]["messages"][1]["content"] == [{
        "type": "tool_result", "tool_use_id": "tool-1", "content": "tool result"
    }]

    model_runtime.chat_completion(
        {
            "name": "claude-sonnet-5-5",
            "api": "anthropic-messages",
            "base_url": "https://api.anthropic.com/v1",
            "headers": {"x-api-key": "anthropic-key"},
            "thinking": False,
            "thinking_levels": ["off", "auto", "minimal", "low", "medium", "high"],
            "compat": {"thinking_mode": "adaptive", "thinking_off_type": "between_tools"},
        },
        [{"role": "user", "content": "no thinking"}],
        max_tokens=64,
    )
    assert calls[3]["body"]["thinking"] == {"type": "between_tools"}


def test_agent_persistence_and_serialization_match_go() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            expected_keys = {
                "id", "name", "description", "avatar", "is_builtin",
                "tenant_id", "created_by", "config", "created_at",
                "updated_at", "deleted_at",
            }

            created = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Serialization agent",
                    "description": "custom",
                    "config": {"agent_mode": "quick-answer"},
                    "runnable_by_viewer": False,
                },
            )
            assert created.status_code == 201, created.text
            created_data = created.json()["data"]
            custom_id = created_data["id"]
            assert set(created_data) == expected_keys

            fetched = await client.get(f"/api/v1/agents/{custom_id}", headers=headers)
            assert fetched.status_code == 200, fetched.text
            assert set(fetched.json()["data"]) == expected_keys

            listed = await client.get("/api/v1/agents", headers=headers)
            assert listed.status_code == 200, listed.text
            listed_custom = next(
                item for item in listed.json()["data"] if item["id"] == custom_id
            )
            assert set(listed_custom) == expected_keys | {"creator_name"}
            assert listed_custom["creator_name"] == login["user"]["username"]

            for agent_id in (custom_id, "builtin-quick-answer"):
                with db.transaction() as conn:
                    conn.execute(
                        "DELETE FROM custom_agents WHERE id=? AND tenant_id=?",
                        (agent_id, tenant_id),
                    )
            stored_config = {
                "agent_mode": "quick-answer",
                "max_iterations": 99,
                "stored_marker": "from-db",
            }
            with db.transaction() as conn:
                db.insert(
                    "custom_agents",
                    {
                        "id": "builtin-quick-answer",
                        "name": "stored name",
                        "description": "stored description",
                        "avatar": "stored-avatar",
                        "is_builtin": 1,
                        "tenant_id": tenant_id,
                        "created_by": "",
                        "runnable_by_viewer": 0,
                        "config": json.dumps(stored_config),
                        "created_at": db.utcnow(),
                        "updated_at": db.utcnow(),
                        "deleted_at": None,
                    },
                    conn,
                )

            try:
                stored = await client.get(
                    "/api/v1/agents/builtin-quick-answer", headers=headers
                )
                assert stored.status_code == 200, stored.text
                stored_data = stored.json()["data"]
                assert set(stored_data) == expected_keys
                assert stored_data["name"] == "快速问答"
                assert stored_data["config"] == stored_config

                stored_list = await client.get(
                    "/api/v1/agents?creator=mine", headers=headers
                )
                assert stored_list.status_code == 200, stored_list.text
                stored_listed = next(
                    item for item in stored_list.json()["data"]
                    if item["id"] == "builtin-quick-answer"
                )
                assert stored_listed["config"] == stored_config
                assert set(stored_listed) == expected_keys

                copied = await client.post(
                    "/api/v1/agents/builtin-quick-answer/copy", headers=headers
                )
                assert copied.status_code == 201, copied.text
                copied_data = copied.json()["data"]
                assert set(copied_data) == expected_keys
                assert copied_data["name"] == "快速问答 副本"
                assert copied_data["config"] == stored_config
                custom_id = copied_data["id"]
            finally:
                with db.transaction() as conn:
                    conn.execute(
                        "DELETE FROM custom_agents WHERE tenant_id=? AND id IN (?, ?)",
                        (tenant_id, custom_id, "builtin-quick-answer"),
                    )

    _run(case)


def test_agent_editor_catalogs_match_go() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            placeholders = await client.get(
                "/api/v1/agents/placeholders", headers=headers
            )
            assert placeholders.status_code == 200, placeholders.text
            placeholder_data = placeholders.json()["data"]
            assert set(placeholder_data) == {
                "all", "system_prompt", "agent_system_prompt",
                "context_template", "rewrite_system_prompt",
                "rewrite_prompt", "fallback_prompt",
            }

            def names(key: str) -> list[str]:
                return [item["name"] for item in placeholder_data[key]]

            assert names("all") == [
                "query", "contexts", "current_time", "current_week",
                "conversation", "yesterday", "answer", "knowledge_bases",
                "web_search_status", "language",
            ]
            assert names("system_prompt") == [
                "query", "contexts", "current_time", "current_week", "language",
            ]
            assert names("agent_system_prompt") == [
                "knowledge_bases", "web_search_status", "current_time", "language",
            ]
            assert names("context_template") == names("system_prompt")
            assert names("rewrite_system_prompt") == [
                "query", "conversation", "current_time", "yesterday", "language",
            ]
            assert names("rewrite_prompt") == names("rewrite_system_prompt")
            assert names("fallback_prompt") == ["query", "language"]
            assert placeholder_data["all"][2]["description"].startswith(
                "当前日期（ISO 格式：2006-01-02）"
            )

            presets = await client.get("/api/v1/agents/type-presets", headers=headers)
            assert presets.status_code == 200, presets.text
            preset_data = presets.json()["data"]
            assert [item["id"] for item in preset_data] == [
                "rag-qa", "wiki-qa", "hybrid-rag-wiki", "data-analysis", "custom",
            ]
            by_id = {item["id"]: item for item in preset_data}
            assert set(by_id["rag-qa"]) == {"id", "i18n", "config"}
            assert set(by_id["data-analysis"]) == {
                "id", "i18n", "config", "kb_filter",
            }
            assert set(by_id["custom"]) == {"id", "i18n"}
            assert set(by_id["rag-qa"]["i18n"]) == {"default", "zh-CN"}
            assert by_id["rag-qa"]["i18n"]["zh-CN"]["label"] == "RAG 问答"
            assert by_id["rag-qa"]["config"] == {
                "system_prompt_id": "progressive_rag_agent",
                "temperature": 0.7,
                "max_iterations": 30,
                "allowed_tools": [
                    "search_knowledge", "read_document", "list_documents"
                ],
                "faq_priority_enabled": True,
                "kb_selection_mode": "all",
            }
            assert by_id["data-analysis"]["config"] == {
                "system_prompt_id": "data_analyst",
                "temperature": 0.3,
                "max_iterations": 30,
                "allowed_tools": ["data_schema", "data_analysis"],
                "supported_file_types": ["csv", "xlsx"],
                "kb_selection_mode": "all",
            }
            assert by_id["data-analysis"]["kb_filter"] == {"none_of": ["faq"]}

            english = await client.get(
                "/api/v1/agents/type-presets",
                headers={**headers, "Accept-Language": "en-US,en;q=0.9"},
            )
            assert english.status_code == 200, english.text
            english_rag = next(
                item for item in english.json()["data"] if item["id"] == "rag-qa"
            )
            assert set(english_rag["i18n"]) == {"default"}
            assert english_rag["i18n"]["default"]["label"] == "RAG Q&A"

    _run(case)


def test_agent_suggested_questions_match_go_sources() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            curated = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Curated suggestions",
                    "config": {
                        "question_suggestions": {
                            "starters": {
                                "enabled": True,
                                "mode": "curated",
                                "items": ["Question A", "Question B", "Question C"],
                                "count": 3,
                            }
                        }
                    },
                },
            )
            assert curated.status_code == 201, curated.text
            curated_id = curated.json()["data"]["id"]

            response = await client.get(
                f"/api/v1/agents/{curated_id}/suggested-questions",
                headers=headers,
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"] == {
                "questions": [
                    {"question": "Question A", "source": "agent_config"},
                    {"question": "Question B", "source": "agent_config"},
                    {"question": "Question C", "source": "agent_config"},
                ]
            }
            limited = await client.get(
                f"/api/v1/agents/{curated_id}/suggested-questions?limit=2",
                headers=headers,
            )
            assert [item["question"] for item in limited.json()["data"]["questions"]] == [
                "Question A", "Question B",
            ]

            disabled = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Disabled suggestions",
                    "config": {
                        "question_suggestions": {
                            "starters": {"enabled": False, "mode": "hybrid"}
                        }
                    },
                },
            )
            disabled_id = disabled.json()["data"]["id"]
            disabled_response = await client.get(
                f"/api/v1/agents/{disabled_id}/suggested-questions",
                headers=headers,
            )
            assert disabled_response.json()["data"] == {"questions": []}

            knowledge_base = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Suggestion knowledge base"},
            )
            assert knowledge_base.status_code == 201, knowledge_base.text
            kb_id = knowledge_base.json()["data"]["id"]
            knowledge = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Suggestion source", "content": "Suggestion source content."},
            )
            assert knowledge.status_code == 201, knowledge.text
            knowledge_id = knowledge.json()["data"]["id"]
            chunks = await client.get(f"/api/v1/chunks/{knowledge_id}", headers=headers)
            chunk_id = chunks.json()["data"][0]["id"]
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE chunks SET metadata=? WHERE id=?",
                    (json.dumps({"generated_questions": ["Generated question?"]}), chunk_id),
                )

            knowledge_agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Knowledge suggestions",
                    "config": {
                        "kb_selection_mode": "selected",
                        "knowledge_bases": [kb_id],
                        "question_suggestions": {
                            "starters": {"enabled": True, "mode": "knowledge", "count": 6}
                        },
                    },
                },
            )
            knowledge_agent_id = knowledge_agent.json()["data"]["id"]
            knowledge_response = await client.get(
                f"/api/v1/agents/{knowledge_agent_id}/suggested-questions",
                headers=headers,
            )
            assert knowledge_response.json()["data"] == {
                "questions": [{
                    "question": "Generated question?",
                    "source": "document",
                    "knowledge_base_id": kb_id,
                    "knowledge_id": knowledge_id,
                }]
            }

            missing = await client.get(
                "/api/v1/agents/missing-agent/suggested-questions",
                headers=headers,
            )
            assert missing.status_code == 404, missing.text
            invalid_tags = await client.get(
                f"/api/v1/agents/{curated_id}/suggested-questions?tag_scopes=invalid",
                headers=headers,
            )
            assert invalid_tags.status_code == 400, invalid_tags.text
            assert invalid_tags.json()["error"]["message"] == "tag_scopes must be valid JSON"

    _run(case)


def test_agent_suggested_question_tag_scopes_and_api_key_scope(monkeypatch) -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])

            async def create_kb(name: str) -> str:
                response = await client.post(
                    "/api/v1/knowledge-bases", headers=headers, json={"name": name}
                )
                assert response.status_code == 201, response.text
                return response.json()["data"]["id"]

            async def create_question(
                kb_id: str, title: str, question: str, tag_ids: list[str] | None = None
            ) -> str:
                knowledge = await client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                    headers=headers,
                    json={
                        "title": title,
                        "content": title + " content",
                        "tag_ids": tag_ids or [],
                    },
                )
                assert knowledge.status_code == 201, knowledge.text
                knowledge_id = knowledge.json()["data"]["id"]
                chunks = await client.get(
                    f"/api/v1/chunks/{knowledge_id}", headers=headers
                )
                assert chunks.status_code == 200, chunks.text
                chunk_id = chunks.json()["data"][0]["id"]
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE chunks SET metadata=? WHERE id=?",
                        (json.dumps({"generated_questions": [question]}), chunk_id),
                    )
                return knowledge_id

            allowed_kb_id = await create_kb("Tag allowed KB")
            denied_kb_id = await create_kb("Tag denied KB")

            allowed_tag = await client.post(
                f"/api/v1/knowledge-bases/{allowed_kb_id}/tags",
                headers=headers,
                json={"name": "Allowed tag", "color": "#111111"},
            )
            assert allowed_tag.status_code == 201, allowed_tag.text
            allowed_tag_data = allowed_tag.json()["data"]
            denied_tag = await client.post(
                f"/api/v1/knowledge-bases/{denied_kb_id}/tags",
                headers=headers,
                json={"name": "Denied tag", "color": "#222222"},
            )
            assert denied_tag.status_code == 201, denied_tag.text
            denied_tag_data = denied_tag.json()["data"]

            allowed_tagged_id = await create_question(
                allowed_kb_id,
                "Allowed tagged document",
                "Allowed tagged question?",
                [allowed_tag_data["id"]],
            )
            await create_question(
                allowed_kb_id,
                "Allowed untagged document",
                "Allowed untagged question?",
            )
            denied_tagged_id = await create_question(
                denied_kb_id,
                "Denied tagged document",
                "Denied tagged question?",
                [denied_tag_data["id"]],
            )

            for kb_id, question, tag_seq in (
                (allowed_kb_id, "Allowed tagged FAQ?", allowed_tag_data["seq_id"]),
                (allowed_kb_id, "Allowed untagged FAQ?", None),
                (denied_kb_id, "Denied tagged FAQ?", denied_tag_data["seq_id"]),
            ):
                created = await client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/faq/entry",
                    headers=headers,
                    json={
                        "standard_question": question,
                        "answer": question,
                        "tag_id": tag_seq,
                        "is_recommended": True,
                    },
                )
                assert created.status_code == 201, created.text

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Tag scope suggestions",
                    "config": {
                        "kb_selection_mode": "selected",
                        "knowledge_bases": [allowed_kb_id, denied_kb_id],
                        "question_suggestions": {
                            "starters": {"enabled": True, "mode": "knowledge", "count": 20}
                        },
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["data"]["id"]

            with db.transaction() as conn:
                db.insert(
                    "wiki_pages",
                    {
                        "id": db.new_id(),
                        "tenant_id": tenant_id,
                        "knowledge_base_id": allowed_kb_id,
                        "slug": "allowed-locale-topic",
                        "title": "Allowed Locale Topic",
                        "page_type": "concept",
                        "created_at": db.utcnow(),
                        "updated_at": db.utcnow(),
                    },
                    conn,
                )
            embed = await client.post(
                f"/api/v1/agents/{agent_id}/embed-channels",
                headers=headers,
                json={"name": "Suggestion locale embed"},
            )
            assert embed.status_code == 201, embed.text
            embed_data = embed.json()["data"]

            unfiltered = await client.get(
                f"/api/v1/agents/{agent_id}/suggested-questions", headers=headers
            )
            assert unfiltered.status_code == 200, unfiltered.text
            assert {
                item["question"] for item in unfiltered.json()["data"]["questions"]
            } == {
                "Allowed tagged question?",
                "Allowed untagged question?",
                "Denied tagged question?",
                "Allowed tagged FAQ?",
                "Allowed untagged FAQ?",
                "Denied tagged FAQ?",
            }

            scopes = json.dumps([
                {
                    "knowledge_base_id": allowed_kb_id,
                    "tag_ids": [allowed_tag_data["id"]],
                }
            ])
            scoped = await client.get(
                f"/api/v1/agents/{agent_id}/suggested-questions",
                headers=headers,
                params={"tag_scopes": scopes},
            )
            assert scoped.status_code == 200, scoped.text
            assert {
                item["question"] for item in scoped.json()["data"]["questions"]
            } == {"Allowed tagged question?", "Allowed tagged FAQ?"}

            unknown_scope = json.dumps([
                {"knowledge_base_id": allowed_kb_id, "tag_ids": ["missing-tag"]}
            ])
            invalid = await client.get(
                f"/api/v1/agents/{agent_id}/suggested-questions",
                headers=headers,
                params={"tag_scopes": unknown_scope},
            )
            assert invalid.status_code == 200, invalid.text
            assert invalid.json()["data"] == {"questions": []}

            key = await client.post(
                f"/api/v1/tenants/{tenant_id}/api-keys",
                headers=headers,
                json={
                    "name": "suggestion scope",
                    "capabilities": ["read_agents", "retrieve"],
                    "knowledge_base_ids": [allowed_kb_id],
                },
            )
            assert key.status_code == 201, key.text
            key_headers = _api_key_headers(key.json()["data"]["token"])

            denied_kb = await client.get(
                f"/api/v1/agents/{agent_id}/suggested-questions",
                headers=key_headers,
                params={"knowledge_base_ids": denied_kb_id},
            )
            assert denied_kb.status_code == 403, denied_kb.text

            denied_document = await client.get(
                f"/api/v1/agents/{agent_id}/suggested-questions",
                headers=key_headers,
                params={"knowledge_ids": denied_tagged_id},
            )
            assert denied_document.status_code == 403, denied_document.text
            assert denied_document.json()["error"]["message"] == (
                "API key scope does not allow knowledge_ids without a verified knowledge base"
            )

            denied_tags = await client.get(
                f"/api/v1/agents/{agent_id}/suggested-questions",
                headers=key_headers,
                params={"tag_scopes": scopes},
            )
            assert denied_tags.status_code == 403, denied_tags.text
            assert denied_tags.json()["error"]["message"] == (
                "API key scope does not allow tag_ids without a verified knowledge base"
            )

            implicit = await client.get(
                f"/api/v1/agents/{agent_id}/suggested-questions", headers=key_headers
            )
            assert implicit.status_code == 200, implicit.text
            implicit_questions = implicit.json()["data"]["questions"]
            assert {
                item["question"] for item in implicit_questions
            } == {
                "Allowed tagged question?",
                "Allowed untagged question?",
                "Allowed tagged FAQ?",
                "Allowed untagged FAQ?",
            }
            assert all(
                item["knowledge_base_id"] == allowed_kb_id
                for item in implicit_questions
            )
            assert allowed_tagged_id in {
                item.get("knowledge_id") for item in implicit_questions
            }

            monkeypatch.setenv("WEKNORA_LANGUAGE", "en-US")
            embed_questions = await client.get(
                f"/api/v1/embed/{embed_data['id']}/suggested-questions",
                headers={
                    "Authorization": "Embed " + embed_data["publish_token"],
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
            )
            assert embed_questions.status_code == 200, embed_questions.text
            embed_text = {
                item["question"]
                for item in embed_questions.json()["data"]["questions"]
            }
            assert "What is Allowed Locale Topic?" in embed_text
            assert "什么是Allowed Locale Topic？" not in embed_text

    _run(case)


def test_source_swagger_operations_are_covered() -> None:
    spec = json.loads((Path(__file__).parents[1] / "docs" / "swagger.json").read_text())
    base_path = spec.get("basePath", "").rstrip("/")

    def normalize(path: str) -> str:
        if path.startswith("/api/v1/"):
            path = path.removeprefix("/api/v1")
        return re.sub(r"\{[^}]+\}", "{}", base_path + path)

    expected = {
        (normalize(path), method.lower())
        for path, operations in spec["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put", "delete", "patch"}
    }
    actual = {
        (re.sub(r"\{[^}]+\}", "{}", path), method.lower())
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "put", "delete", "patch"}
    }
    assert expected - actual == set()


def test_memory_is_recalled_and_persisted_in_chat() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            session = await client.post("/api/v1/sessions", headers=headers, json={"title": "memory smoke"})
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            first = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "记住：我的项目代号是北辰"},
            )
            assert first.status_code == 200, first.text
            assert any(event.get("response_type") == "complete" for event in _events(first))

            second = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "我的项目代号是什么？"},
            )
            assert second.status_code == 200, second.text
            events = _events(second)
            recalled = [event for event in events if event.get("response_type") == "memory_recalled"]
            assert recalled
            assert any("北辰" in item["content"] for item in recalled[0]["data"]["memories"])

            items = await client.get("/api/v1/memory/items", headers=headers)
            assert items.status_code == 200
            assert any("北辰" in item["content"] for item in items.json()["data"])

            messages = await client.get(f"/api/v1/messages/{session_id}/load", headers=headers)
            assert messages.status_code == 200
            assistant = [item for item in messages.json()["data"] if item["role"] == "assistant"]
            assert assistant and assistant[-1]["used_memories"]

    _run(case)


def test_auto_memory_captures_declarative_facts() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            config = await client.put(
                "/api/v1/tenants/kv/memory-config",
                headers=headers,
                json={"enabled": True, "write_mode": "auto"},
            )
            assert config.status_code == 200, config.text

            session = await client.post("/api/v1/sessions", headers=headers, json={"title": "auto memory"})
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            first = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "我的项目代号是北辰星"},
            )
            assert first.status_code == 200, first.text
            assert any(event.get("response_type") == "complete" for event in _events(first))

            items = await client.get("/api/v1/memory/items", headers=headers)
            assert items.status_code == 200
            assert any("北辰星" in item["content"] for item in items.json()["data"])

            second = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "我的项目代号是什么？"},
            )
            assert second.status_code == 200, second.text
            recalled = [event for event in _events(second) if event.get("response_type") == "memory_recalled"]
            assert recalled
            assert any("北辰星" in item["content"] for item in recalled[0]["data"]["memories"])

            restore = await client.put(
                "/api/v1/tenants/kv/memory-config",
                headers=headers,
                json={"enabled": True, "write_mode": "explicit_only"},
            )
            assert restore.status_code == 200, restore.text

    _run(case)


def test_mcp_service_discovers_resources_and_calls_tools(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request(url, payload, headers, timeout):
        assert url == "https://mcp.test/rpc"
        method = str((payload or {}).get("method") or "")
        calls.append(method)
        if method == "notifications/initialized":
            return 202, {}, b"", "application/json"
        if method == "tools/list":
            assert headers.get("Mcp-Session-Id") == "session-1"
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "Look up a release",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "x-mcp-header": "Name"}
                            },
                        },
                    }
                ]
            }
        elif method == "resources/list":
            result = {
                "resources": [
                    {"uri": "docs://release", "name": "Release notes", "mimeType": "text/plain"},
                ]
            }
        elif method == "tools/call":
            assert (payload or {}).get("params", {}).get("arguments") == {"name": "v1"}
            assert "Mcp-Param-Name" not in headers
            result = {
                "content": [{"type": "text", "text": "Release v1 is healthy"}],
                "structuredContent": {"release": "v1", "healthy": True},
            }
        else:
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "Fake MCP", "version": "1.2.0", "description": "Test server"},
                "instructions": "Use lookup before answering release questions.",
            }
        body = json.dumps({"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}).encode()
        return 200, {"Mcp-Session-Id": "session-1"}, body, "application/json"

    monkeypatch.setattr(mcp_client, "_request", fake_request)

    direct = mcp_client.MCPClient(
        {"id": "direct", "transport_type": "http-streamable", "url": "https://mcp.test/rpc"}
    )
    with direct:
        result = direct.call_tool("lookup", {"name": "v1"})
    assert result["content"][0]["text"] == "Release v1 is healthy"
    assert "initialize" in calls and "tools/call" in calls

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            created = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={
                    "name": "Fake MCP",
                    "transport_type": "http-streamable",
                    "url": "https://mcp.test/rpc",
                    "advanced_config": {"timeout": 5},
                },
            )
            assert created.status_code == 201, created.text
            service_id = created.json()["data"]["id"]

            tools = await client.get(f"/api/v1/mcp-services/{service_id}/tools", headers=headers)
            assert tools.status_code == 200, tools.text
            assert tools.json()["data"][0]["name"] == "lookup"
            assert tools.json()["data"][0]["enabled"] is True

            tested = await client.post(f"/api/v1/mcp-services/{service_id}/test", headers=headers)
            assert tested.status_code == 200, tested.text
            assert tested.json()["data"]["success"] is True
            assert tested.json()["data"]["resources"][0]["uri"] == "docs://release"

            refreshed = await client.post(f"/api/v1/mcp-services/{service_id}/metadata/refresh", headers=headers)
            assert refreshed.status_code == 200, refreshed.text
            assert refreshed.json()["data"]["server_name"] == "Fake MCP"
            assert refreshed.json()["data"]["instructions"].startswith("Use lookup")

            metadata = await client.get(f"/api/v1/mcp-services/{service_id}/metadata", headers=headers)
            assert metadata.status_code == 200, metadata.text
            assert metadata.json()["data"]["stale"] is False
            assert metadata.json()["data"]["tool_count"] == 1

            tenant_id = login["active_tenant"]["id"]
            user_id = login["user"]["id"]
            catalog = agent_tools.load_mcp_tools(tenant_id, user_id, [service_id])
            assert [tool["function"]["name"] for tool in catalog] == ["mcp_fake_mcp_lookup"]
            executed = agent_tools.execute_tool(
                catalog[0],
                {"name": "v1"},
                tenant_id=tenant_id,
                user_id=user_id,
            )
            assert executed["success"] is True
            assert executed["output"] == (
                'Release v1 is healthy\n\nStructured content:\n{"healthy":true,"release":"v1"}'
            )
            assert executed["raw"]["structuredContent"] == {"release": "v1", "healthy": True}

            policy = await client.put(
                f"/api/v1/mcp-services/{service_id}/tools/lookup",
                headers=headers,
                json={"enabled": True, "require_approval": True},
            )
            assert policy.status_code == 200, policy.text
            assert policy.json()["data"]["enabled"] is True
            assert policy.json()["data"]["require_approval"] is True
            with pytest.raises(agent_tools.ToolApprovalRequired):
                agent_tools.execute_tool(
                    catalog[0],
                    {"name": "v1"},
                    tenant_id=tenant_id,
                    user_id=user_id,
                )

            disabled = await client.put(
                f"/api/v1/mcp-services/{service_id}/tools/lookup",
                headers=headers,
                json={"enabled": False, "require_approval": False},
            )
            assert disabled.status_code == 200, disabled.text
            assert agent_tools.load_mcp_tools(tenant_id, user_id, [service_id]) == []

            resources = await client.get(f"/api/v1/mcp-services/{service_id}/resources", headers=headers)
            assert resources.status_code == 200, resources.text
            assert resources.json()["data"][0]["name"] == "Release notes"

            listing = await client.get("/api/v1/mcp-services", headers=headers)
            assert listing.status_code == 200, listing.text
            listed = next(item for item in listing.json()["data"] if item["id"] == service_id)
            assert listed["tool_count"] == 1

    _run(case)


def test_mcp_connection_test_uses_advertised_directory_capabilities(monkeypatch) -> None:
    modes = {
        "https://mcp.test/resource-only": {"resources": {}},
        "https://mcp.test/tool-only": {"tools": {}},
        "https://mcp.test/no-capabilities": {},
        "https://mcp.test/broken-tools": {"tools": {}},
    }

    def fake_request(url, payload, headers, timeout):
        method = str((payload or {}).get("method") or "")
        if method == "notifications/initialized":
            return 202, {}, b"", "application/json"
        if method == "tools/list":
            if url in {
                "https://mcp.test/resource-only",
                "https://mcp.test/no-capabilities",
                "https://mcp.test/broken-tools",
            }:
                body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": (payload or {}).get("id"),
                    "error": {"code": -32603, "message": "directory unavailable"},
                }).encode()
                return 200, {}, body, "application/json"
            result = {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]}
        elif method == "resources/list":
            if url in {"https://mcp.test/tool-only", "https://mcp.test/no-capabilities"}:
                body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": (payload or {}).get("id"),
                    "error": {"code": -32603, "message": "directory unavailable"},
                }).encode()
                return 200, {}, body, "application/json"
            result = {"resources": [{"uri": "docs://release", "name": "Release notes"}]}
        else:
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": modes[url],
                "serverInfo": {"name": "Directory test", "version": "1"},
            }
        body = json.dumps({"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}).encode()
        return 200, {}, body, "application/json"

    monkeypatch.setattr(mcp_client, "_request", fake_request)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            async def connect_test(name: str, url: str) -> dict:
                created = await client.post(
                    "/api/v1/mcp-services",
                    headers=headers,
                    json={"name": name, "transport_type": "http-streamable", "url": url},
                )
                assert created.status_code == 201, created.text
                tested = await client.post(
                    f"/api/v1/mcp-services/{created.json()['data']['id']}/test",
                    headers=headers,
                )
                assert tested.status_code == 200, tested.text
                return tested.json()["data"]

            resource_only = await connect_test(
                "Resource only", "https://mcp.test/resource-only"
            )
            assert resource_only["success"] is True
            assert resource_only["tools"] == []
            assert resource_only["resources"][0]["uri"] == "docs://release"

            tool_only = await connect_test("Tool only", "https://mcp.test/tool-only")
            assert tool_only["success"] is True
            assert tool_only["tools"][0]["name"] == "lookup"
            assert tool_only["resources"] == []

            no_capabilities = await connect_test(
                "No capabilities", "https://mcp.test/no-capabilities"
            )
            assert no_capabilities["success"] is True
            assert no_capabilities["tools"] == []
            assert no_capabilities["resources"] == []

            broken_tools = await connect_test(
                "Broken advertised tools", "https://mcp.test/broken-tools"
            )
            assert broken_tools["success"] is False
            assert "directory unavailable" in broken_tools["message"]

    _run(case)


def test_mcp_client_prefers_modern_discovery(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request(url, payload, headers, timeout):
        assert url == "https://modern-mcp.test/rpc"
        method = str((payload or {}).get("method") or "")
        calls.append(method)
        params = (payload or {}).get("params") or {}
        assert params["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert headers["MCP-Protocol-Version"] == "2026-07-28"
        assert headers["Mcp-Method"] == method
        if method == "server/discover":
            result = {
                "supportedVersions": ["2026-07-28"],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "Modern MCP", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{
                    "name": "lookup",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "x-mcp-header": "Name"}
                        },
                    },
                }],
                "resultType": "complete",
                "cacheScope": "private",
                "ttlMs": 0,
            }
        elif method == "tools/call":
            assert headers["Mcp-Name"] == "lookup"
            assert headers["Mcp-Param-Name"] == "v1"
            assert params["arguments"] == {"name": "v1"}
            result = {"content": [{"type": "text", "text": "modern ok"}]}
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        body = json.dumps(
            {"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}
        ).encode()
        return 200, {}, body, "application/json"

    monkeypatch.setattr(mcp_client, "_request", fake_request)
    client = mcp_client.MCPClient(
        {"id": "modern", "transport_type": "http-streamable", "url": "https://modern-mcp.test/rpc"}
    )
    with client:
        assert client.protocol_version == "2026-07-28"
        assert client.initialize_result["capabilities"] == {"tools": {"listChanged": False}}
        assert [tool["name"] for tool in client.list_tools()] == ["lookup"]
        result = client.call_tool("lookup", {"name": "v1"})
        assert result["content"][0]["text"] == "modern ok"

    assert calls == ["server/discover", "tools/list", "tools/call"]


def test_mcp_client_modern_discovery_failure_policy(monkeypatch) -> None:
    for scenario in ("retry-modern", "persistent-fallback", "method-not-found"):
        calls: list[str] = []
        discoveries = 0

        def fake_request(url, payload, headers, timeout):
            nonlocal discoveries
            method = str((payload or {}).get("method") or "")
            calls.append(method)
            if method == "server/discover":
                discoveries += 1
                if scenario == "retry-modern" and discoveries == 1:
                    return 503, {}, b"unavailable", "text/plain"
                if scenario == "persistent-fallback":
                    return 503, {}, b"unavailable", "text/plain"
                if scenario == "method-not-found":
                    body = json.dumps({
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }).encode()
                    return 200, {}, body, "application/json"
                result = {"supportedVersions": ["2026-07-28"], "capabilities": {}}
            elif method == "initialize":
                result = {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "serverInfo": {"name": "Legacy", "version": "1"},
                }
            elif method == "notifications/initialized":
                return 202, {}, b"", "application/json"
            else:
                raise AssertionError(f"unexpected MCP method: {method}")
            body = json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], "result": result}
            ).encode()
            return 200, {}, body, "application/json"

        monkeypatch.setattr(mcp_client, "_request", fake_request)
        client = mcp_client.MCPClient(
            {
                "id": "discovery",
                "transport_type": "http-streamable",
                "url": "https://discovery-mcp.test/rpc",
            }
        )
        with client:
            if scenario == "retry-modern":
                assert client.protocol_version == "2026-07-28"
            else:
                assert client.protocol_version == "2025-11-25"
        if scenario == "method-not-found":
            assert calls.count("server/discover") == 1
        else:
            assert calls.count("server/discover") == 2


def test_mcp_client_generates_modern_param_headers(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_request(url, payload, headers, timeout):
        method = str((payload or {}).get("method") or "")
        calls.append((method, dict(headers)))
        if method == "server/discover":
            result = {"supportedVersions": ["2026-07-28"], "capabilities": {}}
        elif method == "tools/list":
            result = {
                "tools": [{
                    "name": "lookup",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "region": {"type": "string", "x-mcp-header": "Region"},
                            "debug": {"type": "boolean", "x-mcp-header": "Debug"},
                            "limit": {"type": "integer", "x-mcp-header": "Limit"},
                            "wide": {"type": "integer", "x-mcp-header": "Wide"},
                            "config": {
                                "type": "object",
                                "properties": {
                                    "mode": {"type": "string", "x-mcp-header": "Mode"}
                                },
                            },
                        },
                    },
                }]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            raise AssertionError(f"unexpected MCP method: {method}")
        body = json.dumps(
            {"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}
        ).encode()
        return 200, {}, body, "application/json"

    monkeypatch.setattr(mcp_client, "_request", fake_request)
    client = mcp_client.MCPClient(
        {"id": "params", "transport_type": "http-streamable", "url": "https://params-mcp.test/rpc"}
    )
    with client:
        result = client.call_tool(
            "lookup",
            {
                "region": " padded ",
                "debug": True,
                "limit": 7,
                "wide": 9007199254740992,
                "config": {"mode": "fast"},
            },
        )
    assert result["content"][0]["text"] == "ok"
    assert [method for method, _headers in calls] == [
        "server/discover", "tools/list", "tools/call"
    ]
    call_headers = calls[-1][1]
    encoded_region = call_headers["Mcp-Param-Region"]
    assert encoded_region.startswith("=?base64?") and encoded_region.endswith("?=")
    assert base64.b64decode(
        encoded_region[len("=?base64?"):-len("?=")]
    ).decode() == " padded "
    assert call_headers["Mcp-Param-Debug"] == "true"
    assert call_headers["Mcp-Param-Limit"] == "7"
    assert call_headers["Mcp-Param-Mode"] == "fast"
    assert "Mcp-Param-Wide" not in call_headers


def test_mcp_service_create_preserves_enabled_state() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            defaulted = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={
                    "name": "Default enabled MCP",
                    "transport_type": "http-streamable",
                    "url": "https://mcp.test/default",
                },
            )
            assert defaulted.status_code == 201, defaulted.text
            assert defaulted.json()["data"]["enabled"] is True

            disabled = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={
                    "name": "Explicitly disabled MCP",
                    "transport_type": "http-streamable",
                    "url": "https://mcp.test/disabled",
                    "enabled": False,
                },
            )
            assert disabled.status_code == 201, disabled.text
            disabled_id = disabled.json()["data"]["id"]
            assert disabled.json()["data"]["enabled"] is False

            fetched = await client.get(
                f"/api/v1/mcp-services/{disabled_id}", headers=headers
            )
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["data"]["enabled"] is False

            listing = await client.get("/api/v1/mcp-services", headers=headers)
            assert listing.status_code == 200, listing.text
            by_name = {item["name"]: item for item in listing.json()["data"]}
            assert by_name["Default enabled MCP"]["enabled"] is True
            assert by_name["Explicitly disabled MCP"]["enabled"] is False

    _run(case)


def test_mcp_oauth_discovery_registration_callback_and_runtime_token(monkeypatch) -> None:
    token_forms: list[dict] = []

    def fake_http_json(method, url, *, headers=None, payload=None, form=None, timeout=15):
        if method == "GET" and url == "https://auth.test/.well-known/oauth-authorization-server":
            return {
                "issuer": "https://auth.test",
                "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token",
                "registration_endpoint": "https://auth.test/register",
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            }
        if method == "POST" and url == "https://auth.test/register":
            assert payload["redirect_uris"] == ["http://testserver/api/v1/mcp-oauth/callback"]
            assert payload["grant_types"] == ["authorization_code", "refresh_token"]
            return {"client_id": "oauth-client", "client_secret": "oauth-secret"}
        if method == "POST" and url == "https://auth.test/token":
            token_forms.append(dict(form or {}))
            return {
                "access_token": "runtime-access-token",
                "refresh_token": "runtime-refresh-token",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        raise AssertionError((method, url))

    monkeypatch.setattr(mcp_oauth, "_http_json", fake_http_json)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = login["active_tenant"]["id"]
            user_id = login["user"]["id"]

            created = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={
                    "name": "OAuth MCP",
                    "transport_type": "http-streamable",
                    "url": "https://mcp.test/rpc",
                    "auth_config": {
                        "auth_type": "oauth",
                        "scopes": ["read", "write"],
                        "auth_server_metadata_url": "https://auth.test/.well-known/oauth-authorization-server",
                    },
                },
            )
            assert created.status_code == 201, created.text
            service_id = created.json()["data"]["id"]

            authorization = await client.post(
                f"/api/v1/mcp-services/{service_id}/oauth/authorize-url",
                headers=headers,
                json={
                    "redirect_uri": "http://testserver/api/v1/mcp-oauth/callback",
                    "frontend_redirect": "/settings/mcp",
                },
            )
            assert authorization.status_code == 200, authorization.text
            data = authorization.json()["data"]
            parsed = urllib.parse.urlparse(data["authorization_url"])
            query = urllib.parse.parse_qs(parsed.query)
            assert parsed.netloc == "auth.test"
            assert query["client_id"] == ["oauth-client"]
            assert query["scope"] == ["read write"]
            assert query["code_challenge_method"] == ["S256"]
            assert query["resource"] == ["https://mcp.test/rpc"]

            pending = await client.get(
                f"/api/v1/mcp-services/{service_id}/oauth/status",
                headers=headers,
                params={"authorization_attempt": data["authorization_attempt"]},
            )
            assert pending.json()["data"] == {"authorized": False, "state": "pending"}

            callback = await client.get(
                "/api/v1/mcp-oauth/callback",
                params={"code": "provider-code", "state": query["state"][0]},
                follow_redirects=False,
            )
            assert callback.status_code == 302
            assert callback.headers["location"] == "/settings/mcp#mcp_oauth_result=success"
            assert token_forms[-1]["grant_type"] == "authorization_code"
            assert token_forms[-1]["code_verifier"]

            completed = await client.get(
                f"/api/v1/mcp-services/{service_id}/oauth/status",
                headers=headers,
                params={"authorization_attempt": data["authorization_attempt"]},
            )
            assert completed.json()["data"] == {"authorized": True, "state": "authorized"}
            status = await client.get(f"/api/v1/mcp-services/{service_id}/oauth/status", headers=headers)
            assert status.json()["data"]["authorized"] is True
            assert status.json()["data"]["refresh_available"] is True

            pending_approval = approvals.request_oauth(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id="oauth-session",
                assistant_message_id="assistant-1",
                request_id="request-1",
                service_id=service_id,
                service_name="OAuth MCP",
                mcp_tool_name="lookup",
                tool_call_id="call-1",
            )
            resolved = await client.post(
                f"/api/v1/agent/mcp-oauth-resolutions/{pending_approval.id}",
                headers=headers,
                json={"service_id": service_id, "decision": "authorize"},
            )
            assert resolved.status_code == 200, resolved.text
            cancelled_approval = approvals.request_oauth(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id="oauth-session",
                assistant_message_id="assistant-1",
                request_id="request-2",
                service_id=service_id,
                service_name="OAuth MCP",
                mcp_tool_name="lookup",
                tool_call_id="call-2",
            )
            cancelled = await client.post(
                f"/api/v1/agent/mcp-oauth-resolutions/{cancelled_approval.id}",
                headers=headers,
                json={"service_id": service_id, "decision": "cancel"},
            )
            assert cancelled.status_code == 200, cancelled.text

            service = db.fetch_one("SELECT * FROM mcp_services WHERE id=?", (service_id,))
            assert mcp_client.oauth_access_token(tenant_id, user_id, service_id) == "runtime-access-token"
            assert mcp_client._auth_headers(service, "runtime-access-token")["Authorization"] == (
                "Bearer runtime-access-token"
            )

    _run(case)

def test_mcp_tool_text_preserves_structured_results(monkeypatch) -> None:
    structured = {"order": {"id": "订单-42", "paid": False}, "total": 0}
    payload = json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    assert mcp_client.tool_text({"content": [], "structuredContent": structured}) == (
        "Structured content:\n" + payload
    )
    assert mcp_client.tool_text({
        "content": [{"type": "text", "text": "Order found"}],
        "structuredContent": structured,
    }) == "Order found\n\nStructured content:\n" + payload
    assert mcp_client.tool_text({
        "content": [{"type": "text", "text": payload}],
        "structuredContent": structured,
    }) == payload
    assert mcp_client.tool_text({
        "content": [{"type": "text", "text": json.dumps(structured, indent=2)}],
        "structuredContent": structured,
    }) == json.dumps(structured, indent=2)

    image_result = {
        "content": [
            {"type": "text", "text": "Chart rendered"},
            {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="},
        ],
    }
    assert mcp_client.tool_text(image_result) == "Chart rendered\n[Image: image/png]"
    assert mcp_client.tool_images(image_result) == ["data:image/png;base64,aGVsbG8="]
    assert mcp_client.tool_text({
        "content": [
            {"type": "text", "text": "downloaded"},
            {
                "type": "resource",
                "resource": {
                    "uri": "repo://octo/app/main.go",
                    "mimeType": "text/x-go",
                    "text": "package main",
                },
            },
            {
                "type": "resource",
                "uri": "repo://octo/app/logo.png",
                "mimeType": "image/png",
                "data": "iVBORw0KGgo=",
            },
            {
                "type": "resource_link",
                "uri": "repo://octo/app/big.log",
                "name": "big.log",
                "mimeType": "text/plain",
            },
        ],
    }) == "\n".join([
        "downloaded",
        "[Resource: repo://octo/app/main.go (text/x-go)]",
        "package main",
        "[Resource: repo://octo/app/logo.png (image/png)]",
        "[Resource link: repo://octo/app/big.log (text/plain)]",
    ])
    monkeypatch.setattr(mcp_client, "MAX_MCP_IMAGE_BYTES", 6)
    assert mcp_client.tool_images({
        "content": [
            {"type": "image", "mimeType": "image/svg+xml", "data": "aGVsbG8="},
            {"type": "image", "mimeType": "image/png", "data": "a" * 10},
        ],
    }) == []


def test_mcp_endpoint_catalog_defaults_and_normalization() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])

            catalog = await client.get("/api/v1/mcp-endpoints/tools", headers=headers)
            assert catalog.status_code == 200, catalog.text
            data = catalog.json()["data"]
            assert data["groups"] == ["retrieve", "chat", "wiki", "ingest"]
            assert [tool["name"] for tool in data["tools"]] == [
                "list_knowledge_bases", "search_knowledge", "grep_chunks", "list_documents",
                "read_document", "ask", "wiki_search", "wiki_read_page", "wiki_index",
                "add_document", "update_document", "delete_document",
            ]
            assert data["default_tools"] == [
                "list_knowledge_bases", "search_knowledge", "grep_chunks", "list_documents",
                "read_document", "ask", "wiki_search", "wiki_read_page", "wiki_index",
            ]
            assert [tool["name"] for tool in data["tools"] if tool["destructive"]] == [
                "add_document", "update_document", "delete_document",
            ]

            missing_name = await client.post("/api/v1/mcp-endpoints", headers=headers, json={})
            assert missing_name.status_code == 400, missing_name.text

            created = await client.post(
                "/api/v1/mcp-endpoints", headers=headers, json={"name": "Default endpoint"}
            )
            assert created.status_code == 201, created.text
            endpoint = created.json()["data"]
            assert endpoint["name"] == "Default endpoint"
            assert endpoint["enabled"] is True
            assert endpoint["tools"] == data["default_tools"]
            assert endpoint["rate_limit_per_minute"] == 60
            assert endpoint["knowledge_base_ids"] == []
            assert endpoint["default_agent_id"] == ""
            assert endpoint["path"] == "/mcp/" + endpoint["id"]
            assert endpoint["token"].startswith("mcp_")
            assert endpoint["token_hint"] == endpoint["token"][-4:]
            assert "token_hash" not in endpoint and "deleted_at" not in endpoint

            explicit = await client.post(
                "/api/v1/mcp-endpoints",
                headers=headers,
                json={
                    "name": "Explicit endpoint",
                    "enabled": False,
                    "tools": ["unknown", "delete_document", "search_knowledge", "search_knowledge"],
                    "rate_limit_per_minute": 0,
                    "default_agent_id": "builtin-quick-answer",
                },
            )
            assert explicit.status_code == 201, explicit.text
            explicit_data = explicit.json()["data"]
            assert explicit_data["enabled"] is False
            assert explicit_data["tools"] == ["search_knowledge", "delete_document"]
            assert explicit_data["rate_limit_per_minute"] == 60
            assert explicit_data["default_agent_id"] == "builtin-quick-answer"

            updated = await client.put(
                f"/api/v1/mcp-endpoints/{endpoint['id']}",
                headers=headers,
                json={
                    "tools": ["wiki_index", "unknown", "grep_chunks"],
                    "rate_limit_per_minute": 12,
                    "enabled": True,
                },
            )
            assert updated.status_code == 200, updated.text
            updated_data = updated.json()["data"]
            assert updated_data["tools"] == ["grep_chunks", "wiki_index"]
            assert updated_data["rate_limit_per_minute"] == 12
            assert updated_data["enabled"] is True

            for payload in (
                {"tools": ["unknown"]},
                {"rate_limit_per_minute": 6001},
                {"rate_limit_per_minute": -1},
            ):
                invalid = await client.put(
                    f"/api/v1/mcp-endpoints/{endpoint['id']}", headers=headers, json=payload
                )
                assert invalid.status_code == 400, (payload, invalid.text)

            for payload in (
                {"name": "Bad KB", "knowledge_base_ids": ["missing"]},
                {"name": "Bad agent", "default_agent_id": "missing"},
            ):
                invalid = await client.post("/api/v1/mcp-endpoints", headers=headers, json=payload)
                assert invalid.status_code == 404, (payload, invalid.text)

            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "MCP endpoint scope"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            async def create_key(**payload: Any) -> str:
                response = await client.post(
                    f"/api/v1/tenants/{tenant_id}/api-keys", headers=headers, json=payload
                )
                assert response.status_code == 201, response.text
                return str(response.json()["data"]["token"])

            channel_key = _api_key_headers(await create_key(
                name="mcp channels only", capabilities=["manage_channels"]
            ))
            assert (await client.get("/api/v1/mcp-endpoints/tools", headers=channel_key)).status_code == 200
            denied = await client.post(
                "/api/v1/mcp-endpoints", headers=channel_key, json={"name": "Broad endpoint"}
            )
            assert denied.status_code == 403, denied.text

            scoped_key = _api_key_headers(await create_key(
                name="mcp scoped",
                capabilities=["manage_channels", "retrieve", "chat", "read_agents"],
                knowledge_base_ids=[kb_id],
            ))
            unbounded = await client.post(
                "/api/v1/mcp-endpoints", headers=scoped_key, json={"name": "Unbounded endpoint"}
            )
            assert unbounded.status_code == 403, unbounded.text
            scoped = await client.post(
                "/api/v1/mcp-endpoints",
                headers=scoped_key,
                json={"name": "Scoped endpoint", "knowledge_base_ids": [kb_id]},
            )
            assert scoped.status_code == 201, scoped.text
            scoped_id = scoped.json()["data"]["id"]
            assert (await client.post(
                f"/api/v1/mcp-endpoints/{scoped_id}/rotate-token", headers=scoped_key
            )).status_code == 200
            ingest_denied = await client.put(
                f"/api/v1/mcp-endpoints/{scoped_id}",
                headers=scoped_key,
                json={"tools": ["add_document"]},
            )
            assert ingest_denied.status_code == 403, ingest_denied.text
            ingest_create = await client.post(
                "/api/v1/mcp-endpoints",
                headers=scoped_key,
                json={
                    "name": "Ingest endpoint",
                    "knowledge_base_ids": [kb_id],
                    "tools": ["add_document"],
                },
            )
            assert ingest_create.status_code == 403, ingest_create.text

    _run(case)


def test_mcp_endpoint_public_streamable_http() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Public MCP scope"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={
                    "title": "MCP public document",
                    "content": "The zebra-alpha-42 marker identifies the public MCP test passage.",
                },
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]
            source_chunk = db.fetch_one(
                "SELECT id FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index LIMIT 1",
                (knowledge_id,),
            )
            assert source_chunk is not None
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE chunks SET source_locators=? WHERE id=?",
                    (json.dumps([{"type": "page", "page": 1}]), source_chunk["id"]),
                )

            created = await client.post(
                "/api/v1/mcp-endpoints",
                headers=headers,
                json={
                    "name": "Public endpoint",
                    "knowledge_base_ids": [kb_id],
                    "tools": [
                        "list_knowledge_bases",
                        "search_knowledge",
                        "grep_chunks",
                        "list_documents",
                        "read_document",
                        "delete_document",
                    ],
                },
            )
            assert created.status_code == 201, created.text
            endpoint = created.json()["data"]
            endpoint_id = endpoint["id"]
            token = endpoint["token"]
            path = "/mcp/" + endpoint_id
            auth = {"Authorization": "Bearer " + token}

            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}
            }}
            missing = await client.post(path, json=request)
            assert missing.status_code == 401, missing.text
            assert missing.headers["www-authenticate"] == 'Bearer realm="weknora-mcp"'
            assert missing.json()["error"] == "unauthorized: missing bearer token"

            invalid = await client.post(
                path, headers={"Authorization": "Bearer mcp_invalid"}, json=request
            )
            assert invalid.status_code == 401, invalid.text
            assert invalid.headers["www-authenticate"] == 'Bearer realm="weknora-mcp"'

            query_token = await client.post(
                path + "?token=" + token, json=request
            )
            assert query_token.status_code == 401, query_token.text

            initialized = await client.post(path, headers=auth, json=request)
            assert initialized.status_code == 200, initialized.text
            init_result = initialized.json()["result"]
            assert init_result["protocolVersion"] == "2025-03-26"
            assert init_result["capabilities"] == {"tools": {"listChanged": False}}
            assert init_result["serverInfo"] == {"name": "weknora", "version": "1.0.0"}
            assert "list_knowledge_bases" in init_result["instructions"]

            notification = await client.post(
                path,
                headers=auth,
                json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            )
            assert notification.status_code == 202, notification.text
            assert notification.content == b""

            ping = await client.post(
                path,
                headers=auth,
                json={"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}},
            )
            assert ping.status_code == 200, ping.text
            assert ping.json()["result"] == {}

            listed = await client.post(
                path,
                headers=auth,
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            )
            assert listed.status_code == 200, listed.text
            tools = listed.json()["result"]["tools"]
            assert [tool["name"] for tool in tools] == [
                "delete_document", "grep_chunks", "list_documents",
                "list_knowledge_bases", "read_document", "search_knowledge",
            ]
            assert all(tool["inputSchema"]["type"] == "object" for tool in tools)

            hidden = await client.post(
                path,
                headers=auth,
                json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                    "name": "wiki_index", "arguments": {"knowledge_base_id": kb_id}
                }},
            )
            assert hidden.status_code == 200, hidden.text
            assert hidden.json()["error"]["code"] == -32602
            assert "not enabled" in hidden.json()["error"]["message"]

            async def call(name: str, arguments: dict[str, Any], request_id: int = 10) -> dict[str, Any]:
                response = await client.post(
                    path,
                    headers=auth,
                    json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                        "name": name, "arguments": arguments,
                    }},
                )
                assert response.status_code == 200, response.text
                return response.json()["result"]

            kb_result = await call("list_knowledge_bases", {})
            assert kb_result.get("isError") is not True
            assert kb_result["structuredContent"]["total"] == 1
            assert kb_result["structuredContent"]["knowledge_bases"][0]["id"] == kb_id

            search = await call("search_knowledge", {"query": "zebra-alpha-42"})
            assert search.get("isError") is not True
            search_data = search["structuredContent"]
            assert search_data["count"] >= 1
            assert search_data["results"][0]["knowledge_id"] == knowledge_id
            assert "zebra-alpha-42" in search["content"][0]["text"]

            grep = await call("grep_chunks", {"query": "zebra-alpha-4[0-9]"})
            assert grep.get("isError") is not True
            assert grep["structuredContent"]["count"] >= 1

            documents = await call("list_documents", {"knowledge_base_id": kb_id})
            assert documents["structuredContent"]["total"] == 1
            assert documents["structuredContent"]["documents"][0]["id"] == knowledge_id

            read = await call("read_document", {"knowledge_id": knowledge_id})
            assert read.get("isError") is not True
            assert read["structuredContent"]["document"]["id"] == knowledge_id
            assert "zebra-alpha-42" in read["structuredContent"]["chunks"][0]["content"]
            assert read["structuredContent"]["source_chunk_id"] == source_chunk["id"]
            assert read["structuredContent"]["chunk_ids"] == [source_chunk["id"]]

            other_kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Outside MCP scope"}
            )
            assert other_kb.status_code == 201, other_kb.text
            outside = await call(
                "search_knowledge",
                {"query": "zebra-alpha-42", "knowledge_base_ids": [other_kb.json()["data"]["id"]]},
            )
            assert outside["isError"] is True
            assert "outside this endpoint's scope" in outside["content"][0]["text"]

            fetched = await client.get(
                f"/api/v1/mcp-endpoints/{endpoint_id}", headers=headers
            )
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["data"]["last_used_at"]

            get_response = await client.get(path, headers=auth)
            assert get_response.status_code == 405, get_response.text
            deleted_session = await client.delete(path, headers=auth)
            assert deleted_session.status_code == 204, deleted_session.text

            disabled = await client.put(
                f"/api/v1/mcp-endpoints/{endpoint_id}", headers=headers, json={"enabled": False}
            )
            assert disabled.status_code == 200, disabled.text
            disabled_call = await client.post(path, headers=auth, json=request)
            assert disabled_call.status_code == 403, disabled_call.text
            assert disabled_call.json()["error"] == "mcp endpoint disabled"
            assert (await client.put(
                f"/api/v1/mcp-endpoints/{endpoint_id}", headers=headers, json={"enabled": True}
            )).status_code == 200

            limited = await client.post(
                "/api/v1/mcp-endpoints",
                headers=headers,
                json={
                    "name": "Rate limited endpoint",
                    "knowledge_base_ids": [kb_id],
                    "tools": ["list_knowledge_bases"],
                    "rate_limit_per_minute": 1,
                },
            )
            assert limited.status_code == 201, limited.text
            limited_data = limited.json()["data"]
            limited_path = "/mcp/" + limited_data["id"]
            limited_auth = {"Authorization": "Bearer " + limited_data["token"]}

            async def limited_call(request_id: int) -> dict[str, Any]:
                response = await client.post(
                    limited_path,
                    headers=limited_auth,
                    json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                        "name": "list_knowledge_bases", "arguments": {},
                    }},
                )
                assert response.status_code == 200, response.text
                return response.json()["result"]

            assert (await limited_call(1)).get("isError") is not True
            throttled = await limited_call(2)
            assert throttled["isError"] is True
            assert "rate limit" in throttled["content"][0]["text"]

    _run(case)


def test_mcp_endpoint_modern_protocol_discovery() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/mcp-endpoints",
                headers=headers,
                json={
                    "name": "Modern protocol endpoint",
                    "tools": ["list_knowledge_bases"],
                },
            )
            assert created.status_code == 201, created.text
            endpoint = created.json()["data"]
            path = "/mcp/" + endpoint["id"]
            auth = {"Authorization": "Bearer " + endpoint["token"]}
            meta = {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            }

            async def modern_raw(
                method: str, request_id: int, params: dict[str, Any]
            ) -> httpx.Response:
                return await client.post(
                    path,
                    headers={
                        **auth,
                        "MCP-Protocol-Version": "2026-07-28",
                        "Mcp-Method": method,
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": {"_meta": meta, **params},
                    },
                )

            async def modern(method: str, request_id: int, params: dict[str, Any]) -> Any:
                response = await modern_raw(method, request_id, params)
                assert response.status_code == 200, response.text
                return response.json()["result"]

            discovered = await modern("server/discover", 1, {})
            assert discovered["supportedVersions"] == ["2026-07-28"]
            assert discovered["capabilities"] == {"tools": {"listChanged": False}}
            assert discovered["resultType"] == "complete"
            assert discovered["cacheScope"] == "private"
            assert discovered["ttlMs"] == 0
            assert discovered["_meta"]["io.modelcontextprotocol/serverInfo"] == {
                "name": "weknora", "version": "1.0.0"
            }

            listed = await modern("tools/list", 2, {})
            assert [tool["name"] for tool in listed["tools"]] == ["list_knowledge_bases"]
            assert listed["resultType"] == "complete"
            assert listed["cacheScope"] == "private"
            assert listed["ttlMs"] == 0
            assert listed["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "weknora"

            called = await modern(
                "tools/call", 3, {"name": "list_knowledge_bases", "arguments": {}}
            )
            assert called["resultType"] == "complete"
            assert called["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "weknora"

            for index, method in enumerate([
                "initialize",
                "ping",
                "logging/setLevel",
                "resources/subscribe",
                "resources/unsubscribe",
            ], start=10):
                removed = await modern_raw(method, index, {})
                assert removed.status_code == 404, removed.text
                error = removed.json()["error"]
                assert error["code"] == -32601
                assert "removed in protocol version" in error["message"]

            hidden = await modern_raw(
                "tools/call", 20, {"name": "delete_document", "arguments": {}}
            )
            assert hidden.status_code == 400, hidden.text
            assert hidden.json()["error"]["code"] == -32602

            unknown = await modern_raw("unknown/method", 21, {})
            assert unknown.status_code == 404, unknown.text
            assert unknown.json()["error"]["code"] == -32601

            modern_get = await client.get(
                path, headers={**auth, "MCP-Protocol-Version": "2026-07-28"}
            )
            assert modern_get.status_code == 405, modern_get.text
            assert modern_get.headers["allow"] == "POST"
            modern_delete = await client.delete(
                path, headers={**auth, "MCP-Protocol-Version": "2026-07-28"}
            )
            assert modern_delete.status_code == 405, modern_delete.text
            assert modern_delete.headers["allow"] == "POST"

            legacy_list = await client.post(
                path,
                headers=auth,
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            )
            assert legacy_list.status_code == 200, legacy_list.text
            assert "resultType" not in legacy_list.json()["result"]

            legacy_discover = await client.post(
                path,
                headers=auth,
                json={"jsonrpc": "2.0", "id": 5, "method": "server/discover", "params": {}},
            )
            assert legacy_discover.status_code == 200, legacy_discover.text
            assert legacy_discover.json()["error"]["code"] == -32601
            assert "requires protocol version" in legacy_discover.json()["error"]["message"]

            initialized = await client.post(
                path,
                headers=auth,
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "legacy-test", "version": "1"},
                    },
                },
            )
            assert initialized.status_code == 200, initialized.text
            assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"

    _run(case)


def test_mcp_endpoint_wiki_and_ingest_tools() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "MCP wiki and ingest",
                    "indexing_strategy": {
                        "vector_enabled": True,
                        "keyword_enabled": True,
                        "wiki_enabled": True,
                        "graph_enabled": False,
                    },
                },
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Wiki source", "content": "Wiki source passage about orbital mechanics."},
            )
            assert manual.status_code == 201, manual.text

            created = await client.post(
                "/api/v1/mcp-endpoints",
                headers=headers,
                json={
                    "name": "MCP wiki and ingest endpoint",
                    "knowledge_base_ids": [kb_id],
                    "tools": [
                        "wiki_search", "wiki_read_page", "wiki_index",
                        "add_document", "update_document", "delete_document",
                    ],
                },
            )
            assert created.status_code == 201, created.text
            endpoint = created.json()["data"]
            path = "/mcp/" + endpoint["id"]
            auth = {"Authorization": "Bearer " + endpoint["token"]}

            async def call(name: str, arguments: dict[str, Any], request_id: int = 1) -> dict[str, Any]:
                response = await client.post(
                    path,
                    headers=auth,
                    json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                        "name": name, "arguments": arguments,
                    }},
                )
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                assert result.get("isError") is not True, result
                return result

            index = await call("wiki_index", {"knowledge_base_id": kb_id})
            groups = index["structuredContent"]["groups"]
            page = next(item for group in groups for item in group["items"])
            assert page["slug"]

            read_page = await call("wiki_read_page", {"slug": page["slug"]})
            assert read_page["structuredContent"]["content"]
            assert "orbital mechanics" in read_page["structuredContent"]["content"]

            wiki_search = await call("wiki_search", {"query": "orbital"})
            assert wiki_search["structuredContent"]["count"] == 1
            assert wiki_search["structuredContent"]["results"][0]["slug"] == page["slug"]

            added = await call(
                "add_document",
                {
                    "knowledge_base_id": kb_id,
                    "title": "Ingested through MCP",
                    "content": "The first MCP ingest body.",
                },
            )
            knowledge_id = added["structuredContent"]["document"]["id"]
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL",
                (knowledge_id,),
            )

            updated = await call(
                "update_document",
                {
                    "knowledge_id": knowledge_id,
                    "title": "Updated through MCP",
                    "content": "The replacement MCP ingest body.",
                },
            )
            assert updated["structuredContent"]["document"]["title"] == "Updated through MCP"
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND content LIKE '%replacement%' "
                "AND deleted_at IS NULL",
                (knowledge_id,),
            )

            deleted = await call("delete_document", {"knowledge_id": knowledge_id})
            assert deleted["structuredContent"]["deleted"] is True
            row = db.fetch_one("SELECT deleted_at FROM knowledges WHERE id=?", (knowledge_id,))
            assert row and row["deleted_at"]

    _run(case)


def test_agent_wiki_tools_cover_write_search_issue_and_link_mutations() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            user_id = login["user"]["id"]

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "Agent wiki tools",
                    "indexing_strategy": {
                        "vector_enabled": True,
                        "keyword_enabled": True,
                        "wiki_enabled": True,
                        "graph_enabled": False,
                    },
                },
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            source = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Widget source", "content": "The widget is blue."},
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]

            def run(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                return wiki_agent_tools.execute(
                    name,
                    arguments,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    knowledge_base_ids=[kb_id],
                )

            written = run(
                "wiki_write_page",
                {
                    "slug": "entity/blue-widget",
                    "title": "Blue widget",
                    "summary": "A blue widget.",
                    "content": (
                        "# Blue widget\n\nThe widget is blue.\n\n"
                        "| Holder | Certificate |\n| --- | --- |\n"
                        "| Alice | A-1 |\n| Bob | B-2 |"
                    ),
                    "page_type": "entity",
                    "aliases": ["widget"],
                    "source_refs": [source_id],
                },
            )
            assert written["success"] is True, written
            page = db.fetch_one(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug='entity/blue-widget'",
                (kb_id,),
            )
            assert page and page["last_edit_source"] == "agent"
            assert json.loads(page["source_refs"]) == [
                {"knowledge_id": source_id, "title": "Widget source"}
            ]

            truncated = run(
                "wiki_write_page",
                {
                    "slug": "entity/blue-widget",
                    "title": "Blue widget",
                    "summary": "A blue widget.",
                    "content": (
                        "# Blue widget\n\nThe widget is blue.\n\n"
                        "| Holder | Certificate |\n| --- | --- |\n| Alice | A-1 |"
                    ),
                    "page_type": "entity",
                },
            )
            assert truncated["success"] is False
            assert "lost 1 of 2 table row identities" in truncated["output"]
            unchanged = db.fetch_one(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug='entity/blue-widget'",
                (kb_id,),
            )
            assert unchanged and unchanged["version"] == page["version"]
            assert "Bob | B-2" in unchanged["content"]

            linked = run(
                "wiki_write_page",
                {
                    "slug": "concept/widget-links",
                    "title": "Widget links",
                    "summary": "Links to the widget.",
                    "content": "See [[entity/blue-widget|the widget]].",
                    "page_type": "concept",
                },
            )
            assert linked["success"] is True, linked

            found = run("wiki_search", {"query": "blue widget"})
            assert found["success"] is True, found
            assert found["raw"]["results"][0]["slug"] == "entity/blue-widget"
            read = run("wiki_read_page", {"slug": "entity/blue-widget"})
            assert read["success"] is True, read
            assert "The widget is blue" in read["raw"]["content"]

            replaced = run(
                "wiki_replace_text",
                {
                    "slug": "entity/blue-widget",
                    "old_text": "blue",
                    "new_text": "cobalt",
                },
            )
            assert replaced["success"] is True, replaced
            assert replaced["raw"]["replacement_count"] == 1

            issue = run(
                "wiki_flag_issue",
                {
                    "slug": "entity/blue-widget",
                    "issue_type": "out_of_date",
                    "description": "Colour needs verification.",
                    "suspected_knowledge_ids": [source_id],
                },
            )
            assert issue["success"] is True, issue
            issue_row = db.fetch_one(
                "SELECT * FROM wiki_page_issues WHERE knowledge_base_id=? AND slug='entity/blue-widget'",
                (kb_id,),
            )
            assert issue_row and issue_row["status"] == "pending"
            issue_read = run("wiki_read_issue", {"issue_id": issue_row["id"]})
            assert issue_read["success"] is True, issue_read
            issue_updated = run(
                "wiki_update_issue", {"issue_id": issue_row["id"], "status": "resolved"}
            )
            assert issue_updated["success"] is True, issue_updated
            assert db.fetch_one(
                "SELECT status FROM wiki_page_issues WHERE id=?", (issue_row["id"],)
            )["status"] == "resolved"

            renamed = run(
                "wiki_rename_page",
                {"slug": "entity/blue-widget", "new_slug": "entity/cobalt-widget"},
            )
            assert renamed["success"] is True, renamed
            linked_page = db.fetch_one(
                "SELECT content FROM wiki_pages WHERE knowledge_base_id=? AND slug='concept/widget-links'",
                (kb_id,),
            )
            assert "[[entity/cobalt-widget|the widget]]" in linked_page["content"]

            deleted = run("wiki_delete_page", {"slug": "entity/cobalt-widget"})
            assert deleted["success"] is True, deleted
            assert db.fetch_one(
                "SELECT deleted_at FROM wiki_pages WHERE id=?", (page["id"],)
            )["deleted_at"]
            linked_page = db.fetch_one(
                "SELECT content FROM wiki_pages WHERE knowledge_base_id=? AND slug='concept/widget-links'",
                (kb_id,),
            )
            assert "[[entity/cobalt-widget" not in linked_page["content"]

            viewer_headers = await _login_viewer(client, tenant_id)
            viewer_token = viewer_headers["Authorization"].split(" ", 1)[1]
            viewer_id = decode_token(viewer_token, "access")["sub"]
            denied = wiki_agent_tools.execute(
                "wiki_write_page",
                {
                    "slug": "entity/viewer-widget",
                    "title": "Viewer widget",
                    "summary": "Must not be created.",
                    "content": "Denied.",
                    "page_type": "entity",
                },
                tenant_id=tenant_id,
                user_id=viewer_id,
                knowledge_base_ids=[kb_id],
            )
            assert denied["success"] is False
            assert "No editable Wiki knowledge bases" in denied["output"]

    _run(case)


def test_agent_chat_registers_and_executes_wiki_write_tool(monkeypatch) -> None:
    rounds: list[list[dict[str, Any]]] = []

    def fake_chat(messages, tools=None):
        rounds.append([dict(message) for message in messages])
        if len(rounds) == 1:
            names = {tool.get("function", {}).get("name") for tool in tools or []}
            assert "wiki_write_page" in names
            assert "wiki_read_page" in names
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "wiki-call",
                    "type": "function",
                    "function": {
                        "name": "wiki_write_page",
                        "arguments": json.dumps({
                            "slug": "entity/agent-widget",
                            "title": "Agent widget",
                            "summary": "Written by the agent.",
                            "content": "# Agent widget\n\nCreated through the tool loop.",
                            "page_type": "entity",
                        }),
                    },
                }],
            }
        tool_message = next(message for message in messages if message.get("role") == "tool")
        assert "Successfully created page [[entity/agent-widget]]" in tool_message["content"]
        return {"role": "assistant", "content": "The Wiki page was created."}

    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)
    monkeypatch.setattr(app_config, "LLM_BASE_URL", "https://llm.test/v1")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "Agent wiki chat",
                    "indexing_strategy": {
                        "vector_enabled": True,
                        "keyword_enabled": True,
                        "wiki_enabled": True,
                        "graph_enabled": False,
                    },
                },
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "title": "Wiki tool loop",
                    "agent_config": {
                        "memory_enabled": False,
                        "allowed_tools": ["wiki_search", "wiki_read_page", "wiki_write_page"],
                        "kb_selection_mode": "selected",
                        "knowledge_bases": [kb_id],
                    },
                },
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]
            response = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "Create the agent widget page."},
            )
            assert response.status_code == 200, response.text
            events = _events(response)
            tool_call = next(event for event in events if event.get("response_type") == "tool_call")
            assert tool_call["data"]["tool_name"] == "wiki_write_page"
            complete = next(event for event in events if event.get("response_type") == "complete")
            assert complete["data"]["final_content"] == "The Wiki page was created."
            page = db.fetch_one(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug='entity/agent-widget'",
                (kb_id,),
            )
            assert page and page["last_edit_source"] == "agent"

    _run(case)


def test_mcp_endpoint_ask_uses_offline_answer_and_session(monkeypatch) -> None:
    monkeypatch.setattr(llm_service, "_remote_answer", lambda *args, **kwargs: None)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "MCP ask"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={
                    "title": "MCP ask source",
                    "content": "The launch window marker is kestrel-seven.",
                },
            )
            assert manual.status_code == 201, manual.text

            created = await client.post(
                "/api/v1/mcp-endpoints",
                headers=headers,
                json={
                    "name": "MCP ask endpoint",
                    "knowledge_base_ids": [kb_id],
                    "tools": ["ask"],
                },
            )
            assert created.status_code == 201, created.text
            endpoint = created.json()["data"]
            path = "/mcp/" + endpoint["id"]
            auth = {"Authorization": "Bearer " + endpoint["token"]}

            async def ask(question: str, session_id: str | None = None) -> dict[str, Any]:
                arguments = {"question": question}
                if session_id:
                    arguments["session_id"] = session_id
                response = await client.post(
                    path,
                    headers=auth,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": "ask", "arguments": arguments,
                    }},
                )
                assert response.status_code == 200, response.text
                result = response.json()["result"]
                assert result.get("isError") is not True, result
                return result["structuredContent"]

            first = await ask("What is the launch window marker?")
            assert first["agent_id"] == "builtin-quick-answer"
            assert first["session_id"]
            assert first["references"]
            assert "kestrel-seven" in first["answer"]

            second = await ask("Repeat the marker.", first["session_id"])
            assert second["session_id"] == first["session_id"]
            session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (first["session_id"],))
            assert session and session["user_id"] == "mcp-" + endpoint["id"]
            messages = db.fetch_all(
                "SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid",
                (first["session_id"],),
            )
            assert [message["role"] for message in messages] == [
                "user", "assistant", "user", "assistant",
            ]
            assert all(message["channel"] == "mcp" for message in messages)
            assert all(message["is_completed"] for message in messages)

    _run(case)


def test_mcp_endpoint_ask_runs_configured_agent_tool_loop(monkeypatch) -> None:
    rounds: list[list[dict[str, Any]]] = []

    def fake_chat(messages, tools=None):
        rounds.append([dict(message) for message in messages])
        if len(rounds) == 1:
            assert any(
                tool.get("function", {}).get("name") == "knowledge_search"
                for tool in tools or []
            )
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "mcp-ask-search",
                        "type": "function",
                        "function": {
                            "name": "knowledge_search",
                            "arguments": json.dumps({"query": "kestrel-seven"}),
                        },
                    }
                ],
            }
        assert any(
            message.get("role") == "tool"
            and "kestrel-seven" in str(message.get("content") or "")
            for message in messages
        )
        return {"role": "assistant", "content": "Agent verified kestrel-seven."}

    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)
    monkeypatch.setattr(app_config, "LLM_BASE_URL", "https://llm.test/v1")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "MCP agent ask"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={
                    "title": "Agent ask source",
                    "content": "The launch window marker is kestrel-seven.",
                },
            )
            assert manual.status_code == 201, manual.text

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "MCP ask agent",
                    "config": {
                        "agent_mode": "smart-reasoning",
                        "allowed_tools": ["search_knowledge"],
                        "kb_selection_mode": "all",
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["data"]["id"]

            created = await client.post(
                "/api/v1/mcp-endpoints",
                headers=headers,
                json={
                    "name": "MCP agent ask endpoint",
                    "knowledge_base_ids": [kb_id],
                    "default_agent_id": agent_id,
                    "tools": ["ask"],
                },
            )
            assert created.status_code == 201, created.text
            endpoint = created.json()["data"]
            response = await client.post(
                "/mcp/" + endpoint["id"],
                headers={"Authorization": "Bearer " + endpoint["token"]},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "ask",
                        "arguments": {"question": "What is the launch window marker?"},
                    },
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert result.get("isError") is not True, result
            structured = result["structuredContent"]
            assert structured["agent_id"] == agent_id
            assert structured["answer"] == "Agent verified kestrel-seven."
            assert structured["references"]
            assert len(rounds) == 2

            messages = db.fetch_all(
                "SELECT * FROM messages WHERE session_id=? ORDER BY created_at, rowid",
                (structured["session_id"],),
            )
            assert [message["role"] for message in messages] == ["user", "assistant"]
            assert messages[1]["content"] == "Agent verified kestrel-seven."
            assert all(bool(message["is_completed"]) for message in messages)

    _run(case)


def test_custom_agent_config_controls_memory_and_mcp_scope(monkeypatch) -> None:
    def fake_request(url, payload, headers, timeout):
        method = str((payload or {}).get("method") or "")
        if method == "notifications/initialized":
            return 202, {}, b"", "application/json"
        if method == "tools/list":
            result = {"tools": [{"name": "lookup", "description": "lookup", "inputSchema": {"type": "object"}}]}
        else:
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Scoped MCP", "version": "1.0"},
            }
        body = json.dumps({"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}).encode()
        return 200, {}, body, "application/json"

    monkeypatch.setattr(mcp_client, "_request", fake_request)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = login["active_tenant"]["id"]
            user_id = login["user"]["id"]

            service = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={"name": "Scoped MCP", "transport_type": "http-streamable", "url": "https://scoped.test/rpc"},
            )
            assert service.status_code == 201, service.text
            service_id = service.json()["data"]["id"]
            refreshed = await client.post(f"/api/v1/mcp-services/{service_id}/metadata/refresh", headers=headers)
            assert refreshed.status_code == 200, refreshed.text

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Scoped agent",
                    "config": {
                        "memory_enabled": False,
                        "mcp_selection_mode": "selected",
                        "mcp_services": [service_id],
                        "allowed_tools": [],
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["data"]["id"]

            resolved = agents.agent_config(tenant_id, agent_id)
            assert resolved["memory_enabled"] is False
            assert agents.selected_mcp_service_ids(tenant_id, resolved) == [service_id]

            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "scoped agent", "agent_id": agent_id, "agent_config": {}},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            chat = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "记住：夜航星是内部代号"},
            )
            assert chat.status_code == 200, chat.text
            assert any(event.get("response_type") == "complete" for event in _events(chat))

            items = await client.get("/api/v1/memory/items", headers=headers)
            assert items.status_code == 200
            assert all("夜航星" not in item["content"] for item in items.json()["data"])

            loaded = agents.resolve_config({"agent_id": agent_id, "agent_config": "{}"}, tenant_id)
            assert loaded["allowed_tools"] == []

    _run(case)


def test_agent_chat_streams_mcp_tool_calls(monkeypatch) -> None:
    def fake_request(url, payload, headers, timeout):
        method = str((payload or {}).get("method") or "")
        if method == "notifications/initialized":
            return 202, {}, b"", "application/json"
        if method == "tools/list":
            result = {"tools": [{"name": "lookup", "description": "lookup", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            assert (payload or {}).get("params", {}).get("arguments") == {"name": "v1"}
            result = {
                "content": [{"type": "text", "text": "Release v1 is healthy"}],
                "structuredContent": {"release": "v1", "healthy": True},
            }
        else:
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Chat MCP", "version": "1.0"},
            }
        body = json.dumps({"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}).encode()
        return 200, {}, body, "application/json"

    rounds: list[list[dict]] = []

    def fake_chat(messages, tools=None):
        rounds.append([dict(message) for message in messages])
        if len(rounds) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "mcp_chat_mcp_lookup", "arguments": json.dumps({"name": "v1"})},
                    }
                ],
            }
        assert any(
            message.get("role") == "tool"
            and "Release v1 is healthy" in message.get("content", "")
            and '"healthy":true' in message.get("content", "")
            for message in messages
        )
        return {"role": "assistant", "content": "The release is healthy."}

    monkeypatch.setattr(mcp_client, "_request", fake_request)
    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)
    monkeypatch.setattr(app_config, "LLM_BASE_URL", "https://llm.test/v1")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            service = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={"name": "Chat MCP", "transport_type": "http-streamable", "url": "https://chat-mcp.test/rpc"},
            )
            assert service.status_code == 201, service.text
            service_id = service.json()["data"]["id"]
            refreshed = await client.post(f"/api/v1/mcp-services/{service_id}/metadata/refresh", headers=headers)
            assert refreshed.status_code == 200, refreshed.text

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Tool agent",
                    "config": {
                        "mcp_selection_mode": "selected",
                        "mcp_services": [service_id],
                        "allowed_tools": [],
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["data"]["id"]

            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "tool chat", "agent_id": agent_id, "agent_config": {}},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            response = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "Check release v1"},
            )
            assert response.status_code == 200, response.text
            events = _events(response)
            tool_calls = [event for event in events if event.get("response_type") == "tool_call"]
            tool_results = [event for event in events if event.get("response_type") == "tool_result"]
            assert tool_calls[0]["data"]["tool_name"] == "mcp_chat_mcp_lookup"
            assert tool_calls[0]["content"] == "Calling tool: mcp_chat_mcp_lookup"
            assert tool_calls[0]["data"]["iteration"] == 0
            assert tool_results[0]["data"]["output"] == (
                'Release v1 is healthy\n\nStructured content:\n{"healthy":true,"release":"v1"}'
            )
            assert tool_results[0]["content"] == ""
            assert tool_results[0]["data"]["error"] == ""
            assert tool_results[0]["data"]["iteration"] == 0
            thinking = [event for event in events if event.get("response_type") == "thinking"]
            assert thinking[0]["content"].startswith("检索到 ")
            assert thinking[0]["content"].endswith(" 条相关资料")
            assert thinking[0]["data"] == {"event_id": "thinking-1"}
            assert thinking[1]["done"] is True
            assert thinking[1]["data"]["event_id"] == "thinking-1"
            complete = next(event for event in events if event.get("response_type") == "complete")
            assert complete["data"]["final_content"] == "The release is healthy."
            assert complete["data"]["total_steps"] == 1
            assert complete["data"]["total_duration_ms"] >= 0
            assert "usage" not in complete["data"]

    _run(case)


def test_agent_stream_emits_internal_error_event(monkeypatch) -> None:
    async def failing_answer(*args, **kwargs):
        raise RuntimeError("llm crashed")

    monkeypatch.setattr(chat_router, "answer_with_tools_async", failing_answer)
    monkeypatch.setattr(chat_router.model_runtime, "available", lambda runtime: True)
    monkeypatch.setattr(chat_router, "retrieve", lambda *args, **kwargs: [])

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "title": "error stream",
                    "agent_config": {"allowed_tools": ["knowledge_search"]},
                },
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            response = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "trigger the internal failure"},
            )
            assert response.status_code == 200, response.text
            events = _events(response)
            error = next(event for event in events if event.get("response_type") == "error")
            assert error["content"] == "llm crashed"
            assert error["done"] is True
            assert error["data"] == {"stage": "agent_execution", "error": "llm crashed"}
            assert not any(event.get("response_type") == "complete" for event in events)

    _run(case)


def test_agent_chat_waits_for_tool_approval_and_uses_modified_args(monkeypatch) -> None:
    called_arguments: list[dict] = []

    def fake_request(url, payload, headers, timeout):
        method = str((payload or {}).get("method") or "")
        if method == "notifications/initialized":
            return 202, {}, b"", "application/json"
        if method == "tools/list":
            result = {"tools": [{"name": "lookup", "description": "lookup", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            arguments = (payload or {}).get("params", {}).get("arguments") or {}
            called_arguments.append(dict(arguments))
            result = {"content": [{"type": "text", "text": f"Release {arguments.get('name')} is healthy"}]}
        else:
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Approval MCP", "version": "1.0"},
            }
        body = json.dumps({"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}).encode()
        return 200, {}, body, "application/json"

    def fake_chat(messages, tools=None):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "approval-call",
                        "type": "function",
                        "function": {"name": "mcp_approval_mcp_lookup", "arguments": json.dumps({"name": "v1"})},
                    }
                ],
            }
        content = str(tool_messages[-1].get("content") or "")
        if content == "not allowed":
            return {"role": "assistant", "content": "The tool call was rejected."}
        return {"role": "assistant", "content": "The modified call succeeded."}

    monkeypatch.setattr(mcp_client, "_request", fake_request)
    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)
    monkeypatch.setattr(app_config, "LLM_BASE_URL", "https://llm.test/v1")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            service = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={"name": "Approval MCP", "transport_type": "http-streamable", "url": "https://approval-mcp.test/rpc"},
            )
            assert service.status_code == 201, service.text
            service_id = service.json()["data"]["id"]
            refreshed = await client.post(f"/api/v1/mcp-services/{service_id}/metadata/refresh", headers=headers)
            assert refreshed.status_code == 200, refreshed.text
            policy = await client.put(
                f"/api/v1/mcp-services/{service_id}/tools/lookup",
                headers=headers,
                json={"enabled": True, "require_approval": True},
            )
            assert policy.status_code == 200, policy.text

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Approval agent",
                    "config": {
                        "mcp_selection_mode": "selected",
                        "mcp_services": [service_id],
                        "allowed_tools": [],
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "approval chat", "agent_id": agent.json()["data"]["id"], "agent_config": {}},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            first_task = asyncio.create_task(
                client.post(
                    f"/api/v1/agent-chat/{session_id}",
                    headers=headers,
                    json={"query": "Check the approved release"},
                )
            )
            first_pending = await _wait_for_pending("tool")
            assert first_pending.service_id == service_id
            assert first_pending.mcp_tool_name == "lookup"
            approved = await client.post(
                f"/api/v1/agent/tool-approvals/{first_pending.id}",
                headers=headers,
                json={"decision": "approve", "modified_args": {"name": "v2"}, "reason": "safe"},
            )
            assert approved.status_code == 200, approved.text
            first_response = await first_task
            assert first_response.status_code == 200, first_response.text
            first_events = _events(first_response)
            required = next(event for event in first_events if event.get("response_type") == "tool_approval_required")
            resolved = next(event for event in first_events if event.get("response_type") == "tool_approval_resolved")
            result = next(event for event in first_events if event.get("response_type") == "tool_result")
            complete = next(event for event in first_events if event.get("response_type") == "complete")
            assert json.loads(required["data"]["args_json"]) == {"name": "v1"}
            assert required["content"] == "MCP tool requires human approval"
            assert required["done"] is True
            assert resolved["data"]["approved"] is True
            assert resolved["content"] == "MCP tool approval resolved"
            assert resolved["done"] is True
            assert result["data"]["output"] == "Release v2 is healthy"
            assert complete["data"]["final_content"] == "The modified call succeeded."
            assert called_arguments == [{"name": "v2"}]

            second_task = asyncio.create_task(
                client.post(
                    f"/api/v1/agent-chat/{session_id}",
                    headers=headers,
                    json={"query": "Run the rejected release check"},
                )
            )
            second_pending = await _wait_for_pending("tool")
            rejected = await client.post(
                f"/api/v1/agent/tool-approvals/{second_pending.id}",
                headers=headers,
                json={"decision": "reject", "reason": "not allowed"},
            )
            assert rejected.status_code == 200, rejected.text
            second_response = await second_task
            assert second_response.status_code == 200, second_response.text
            second_events = _events(second_response)
            rejected_resolution = next(
                event for event in second_events if event.get("response_type") == "tool_approval_resolved"
            )
            rejected_result = next(event for event in second_events if event.get("response_type") == "tool_result")
            assert rejected_resolution["data"]["approved"] is False
            assert rejected_result["data"]["output"] == "not allowed"
            assert called_arguments == [{"name": "v2"}]

    _run(case)


def test_agent_chat_waits_for_mcp_oauth_and_retries_tool(monkeypatch) -> None:
    authorized = {"value": False}
    execution_calls: list[bool] = []

    def fake_request(url, payload, headers, timeout):
        method = str((payload or {}).get("method") or "")
        if method == "notifications/initialized":
            return 202, {}, b"", "application/json"
        if method == "tools/list":
            result = {"tools": [{"name": "lookup", "description": "lookup", "inputSchema": {"type": "object"}}]}
        else:
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "OAuth Chat MCP", "version": "1.0"},
            }
        body = json.dumps({"jsonrpc": "2.0", "id": (payload or {}).get("id"), "result": result}).encode()
        return 200, {}, body, "application/json"

    def fake_execute(
        tool,
        arguments,
        *,
        tenant_id,
        user_id,
        knowledge_base_ids=None,
        allowed_skills=None,
        skip_approval=False,
    ):
        execution_calls.append(skip_approval)
        if not authorized["value"]:
            raise agent_tools.MCPOAuthRequired(
                tool["function"]["name"], tool["service_id"], tool["tool_name"], arguments
            )
        return {"success": True, "output": "OAuth tool ran", "duration_ms": 1}

    def fake_chat(messages, tools=None):
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "oauth-call",
                        "type": "function",
                        "function": {"name": "mcp_oauth_chat_mcp_lookup", "arguments": json.dumps({"name": "v1"})},
                    }
                ],
            }
        if tool_messages[-1].get("content") == "user canceled":
            return {"role": "assistant", "content": "Authorization was canceled."}
        return {"role": "assistant", "content": "OAuth tool completed."}

    monkeypatch.setattr(mcp_client, "_request", fake_request)
    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)
    monkeypatch.setattr(app_config, "LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setattr(chat_router, "execute_tool", fake_execute)
    monkeypatch.setattr(
        chat_router.mcp_oauth,
        "is_authorized",
        lambda tenant_id, user_id, service_id: authorized["value"],
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            service = await client.post(
                "/api/v1/mcp-services",
                headers=headers,
                json={"name": "OAuth Chat MCP", "transport_type": "http-streamable", "url": "https://oauth-chat.test/rpc"},
            )
            assert service.status_code == 201, service.text
            service_id = service.json()["data"]["id"]
            refreshed = await client.post(f"/api/v1/mcp-services/{service_id}/metadata/refresh", headers=headers)
            assert refreshed.status_code == 200, refreshed.text
            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "OAuth chat agent",
                    "config": {
                        "mcp_selection_mode": "selected",
                        "mcp_services": [service_id],
                        "allowed_tools": [],
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "oauth chat", "agent_id": agent.json()["data"]["id"], "agent_config": {}},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            first_task = asyncio.create_task(
                client.post(
                    f"/api/v1/agent-chat/{session_id}",
                    headers=headers,
                    json={"query": "Run the OAuth tool"},
                )
            )
            first_pending = await _wait_for_pending("oauth")
            not_ready = await client.post(
                f"/api/v1/agent/mcp-oauth-resolutions/{first_pending.id}",
                headers=headers,
                json={"service_id": service_id, "decision": "authorize"},
            )
            assert not_ready.status_code == 409, not_ready.text
            authorized["value"] = True
            approved = await client.post(
                f"/api/v1/agent/mcp-oauth-resolutions/{first_pending.id}",
                headers=headers,
                json={"service_id": service_id, "decision": "authorize"},
            )
            assert approved.status_code == 200, approved.text
            first_response = await first_task
            assert first_response.status_code == 200, first_response.text
            first_events = _events(first_response)
            oauth_required = next(event for event in first_events if event.get("response_type") == "mcp_oauth_required")
            oauth_resolved = next(event for event in first_events if event.get("response_type") == "mcp_oauth_resolved")
            result = next(event for event in first_events if event.get("response_type") == "tool_result")
            complete = next(event for event in first_events if event.get("response_type") == "complete")
            assert oauth_required["data"]["service_id"] == service_id
            assert oauth_required["content"] == "MCP service requires OAuth authorization"
            assert oauth_required["done"] is True
            assert oauth_resolved["data"]["authorized"] is True
            assert oauth_resolved["content"] == "MCP OAuth authorization resolved"
            assert oauth_resolved["done"] is True
            assert result["data"]["output"] == "OAuth tool ran"
            assert complete["data"]["final_content"] == "OAuth tool completed."
            assert execution_calls == [False, True]

            authorized["value"] = False
            second_task = asyncio.create_task(
                client.post(
                    f"/api/v1/agent-chat/{session_id}",
                    headers=headers,
                    json={"query": "Run the OAuth tool again"},
                )
            )
            second_pending = await _wait_for_pending("oauth")
            cancelled = await client.post(
                f"/api/v1/agent/mcp-oauth-resolutions/{second_pending.id}/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200, cancelled.text
            second_response = await second_task
            assert second_response.status_code == 200, second_response.text
            second_events = _events(second_response)
            cancelled_resolution = next(
                event for event in second_events if event.get("response_type") == "mcp_oauth_resolved"
            )
            cancelled_result = next(event for event in second_events if event.get("response_type") == "tool_result")
            assert cancelled_resolution["data"]["authorized"] is False
            assert cancelled_resolution["data"]["canceled"] is True
            assert cancelled_result["data"]["output"] == "user canceled"

    _run(case)


def test_steer_queue_persists_and_signs_sandbox_tickets() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = login["active_tenant"]["id"]
            user_id = login["user"]["id"]
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "steer smoke"},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            idle = await client.post(
                f"/api/v1/sessions/{session_id}/steer",
                headers=headers,
                json={"query": "not running", "delivery": "after"},
            )
            assert idle.status_code == 200, idle.text
            assert idle.json()["data"]["status"] == "new_run"

            assistant_id = chat_router._create_message(
                session_id=session_id,
                role="assistant",
                content="",
                request_id=db.new_id(),
                tenant_id=tenant_id,
                is_completed=False,
            )
            steer_id = str(uuid.uuid4())
            queued = await client.post(
                f"/api/v1/sessions/{session_id}/steer",
                headers=headers,
                json={
                    "query": "prefer release v2",
                    "delivery": "after",
                    "steer_id": steer_id,
                    "expected_assistant_message_id": assistant_id,
                    "mentioned_items": [{"type": "knowledge_base", "id": "kb-1"}],
                },
            )
            assert queued.status_code == 200, queued.text
            queued_data = queued.json()["data"]
            assert queued_data["status"] == "queued"
            assert queued_data["assistant_message_id"] == assistant_id

            duplicate = await client.post(
                f"/api/v1/sessions/{session_id}/steer",
                headers=headers,
                json={"query": "prefer release v2", "steer_id": steer_id, "delivery": "after"},
            )
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["data"]["status"] == "queued"
            conflict_response = await client.post(
                f"/api/v1/sessions/{session_id}/steer",
                headers=headers,
                json={"query": "different text", "steer_id": steer_id, "delivery": "after"},
            )
            assert conflict_response.status_code == 409

            listing = await client.get(f"/api/v1/sessions/{session_id}/steer", headers=headers)
            assert listing.status_code == 200, listing.text
            listed = listing.json()["data"]
            assert listed["assistant_message_id"] == assistant_id
            assert listed["items"][0]["steer_id"] == steer_id
            assert listed["items"][0]["mentioned_items"][0]["id"] == "kb-1"

            promoted = await client.post(
                f"/api/v1/sessions/{session_id}/steer/{steer_id}/inject",
                headers=headers,
            )
            assert promoted.status_code == 200, promoted.text
            assert promoted.json()["data"]["delivery"] == "inject"
            listing = await client.get(f"/api/v1/sessions/{session_id}/steer", headers=headers)
            assert listing.json()["data"]["items"][0]["delivery"] == "inject"

            removed = await client.delete(
                f"/api/v1/sessions/{session_id}/steer/{steer_id}",
                headers=headers,
            )
            assert removed.status_code == 200, removed.text
            assert removed.json()["data"]["removed"] is True
            listing = await client.get(f"/api/v1/sessions/{session_id}/steer", headers=headers)
            assert listing.json()["data"]["items"] == []

            ticket = await client.post(
                f"/api/v1/sessions/{session_id}/sandbox/terminal-ticket",
                headers=headers,
            )
            assert ticket.status_code == 200, ticket.text
            ticket_data = ticket.json()["data"]
            assert ticket_data["expires_in"] == 120
            claims = decode_token(ticket_data["ticket"], "sandbox_terminal")
            assert claims["sub"] == session_id
            assert claims["session_id"] == session_id
            assert claims["user_id"] == user_id
            access_row = db.fetch_one(
                "SELECT id FROM auth_tokens WHERE token=? AND token_type='access_token'",
                (login["token"],),
            )
            assert access_row is not None
            assert claims["token_id"] == access_row["id"]
            with db.transaction() as conn:
                conn.execute("UPDATE auth_tokens SET is_revoked=1 WHERE token=?", (login["token"],))
            revoked = await client.post(
                f"/api/v1/sessions/{session_id}/sandbox/terminal-ticket",
                headers=headers,
            )
            assert revoked.status_code == 401

    _run(case)


def test_session_deletion_destroys_bound_sandboxes(monkeypatch) -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            created: list[str] = []
            for title in ("Delete one", "Delete batch", "Delete all"):
                response = await client.post(
                    "/api/v1/sessions", headers=headers, json={"title": title}
                )
                assert response.status_code == 201, response.text
                created.append(response.json()["data"]["id"])

            now = db.utcnow()
            with db.transaction() as conn:
                for session_id in created:
                    conn.execute(
                        "INSERT INTO py_session_sandboxes(tenant_id, session_id, config_id, provider, "
                        "sandbox_id, created_at, updated_at) VALUES (?, ?, 'cfg', 'docker', ?, ?, ?)",
                        (tenant_id, session_id, f"container-{session_id}", now, now),
                    )

            destroyed: list[tuple[int, str]] = []
            monkeypatch.setattr(
                chat_router.session_sandbox,
                "delete",
                lambda tenant, session_id: destroyed.append((tenant, session_id)),
            )

            single = await client.delete(f"/api/v1/sessions/{created[0]}", headers=headers)
            assert single.status_code == 200, single.text
            assert destroyed == [(tenant_id, created[0])]

            batch = await client.request(
                "DELETE",
                "/api/v1/sessions/batch",
                headers=headers,
                json={"ids": [created[1], str(uuid.uuid4()), created[1]]},
            )
            assert batch.status_code == 200, batch.text
            assert batch.json()["data"]["deleted"] == 1
            assert destroyed[-1] == (tenant_id, created[1])

            empty = await client.request(
                "DELETE", "/api/v1/sessions/batch", headers=headers, json={"ids": []}
            )
            assert empty.status_code == 400

            delete_all = await client.request(
                "DELETE", "/api/v1/sessions/batch", headers=headers, json={"delete_all": True}
            )
            assert delete_all.status_code == 200, delete_all.text
            assert delete_all.json()["data"]["deleted"] == "all"
            assert (tenant_id, created[2]) in destroyed
            assert db.fetch_one(
                "SELECT deleted_at FROM sessions WHERE id=?", (created[2],)
            )["deleted_at"]

    _run(case)


def test_tenant_admin_manages_channel_sessions_without_exposing_private_web_sessions() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            tenant_id = int(login["active_tenant"]["id"])
            admin_headers = await _login_tenant_member(client, tenant_id, "admin")
            viewer_headers = await _login_viewer(client, tenant_id)
            now = db.utcnow()
            channel_id = str(uuid.uuid4())
            private_id = str(uuid.uuid4())
            embed_id = str(uuid.uuid4())
            foreign_id = str(uuid.uuid4())
            with db.transaction() as conn:
                for row_id, row_tenant, owner, description, title in (
                    (channel_id, tenant_id, "im:wecom:visitor", "", "channel"),
                    (embed_id, tenant_id, "embed_session:1:ch-1:sess-1", "", "embed"),
                    (private_id, tenant_id, "another-web-user", "", "private"),
                    (foreign_id, tenant_id + 1, "im:wecom:visitor", "", "foreign"),
                ):
                    conn.execute(
                        "INSERT INTO sessions(id, tenant_id, title, description, user_id, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (row_id, row_tenant, title, description, owner, now, now),
                    )

            denied = await client.put(
                f"/api/v1/sessions/{channel_id}",
                headers=viewer_headers,
                json={"title": "viewer edit"},
            )
            assert denied.status_code == 404, denied.text

            updated = await client.put(
                f"/api/v1/sessions/{channel_id}",
                headers=admin_headers,
                json={"title": "admin edit"},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["title"] == "admin edit"

            private = await client.delete(
                f"/api/v1/sessions/{private_id}", headers=admin_headers
            )
            assert private.status_code == 404, private.text
            foreign = await client.delete(
                f"/api/v1/sessions/{foreign_id}", headers=admin_headers
            )
            assert foreign.status_code == 404, foreign.text

            batch = await client.request(
                "DELETE",
                "/api/v1/sessions/batch",
                headers=admin_headers,
                json={"ids": [channel_id, private_id, embed_id, str(uuid.uuid4())]},
            )
            assert batch.status_code == 200, batch.text
            assert batch.json()["data"]["deleted"] == 2
            assert db.fetch_one(
                "SELECT deleted_at FROM sessions WHERE id=?", (channel_id,)
            )["deleted_at"]
            assert db.fetch_one(
                "SELECT deleted_at FROM sessions WHERE id=?", (embed_id,)
            )["deleted_at"]
            assert not db.fetch_one(
                "SELECT deleted_at FROM sessions WHERE id=?", (private_id,)
            )["deleted_at"]

    _run(case)


def test_tool_loop_injects_queued_steer_before_round(monkeypatch) -> None:
    calls: list[list[dict]] = []
    pending = [{"content": "prefer release v2"}]

    def fake_chat(messages, tools=None):
        calls.append([dict(message) for message in messages])
        return {"role": "assistant", "content": "steered answer"}

    async def before_round():
        return [pending.pop(0)] if pending else []

    async def execute(tool, arguments, tool_call_id):
        raise AssertionError("no tool should run")

    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)

    async def case() -> None:
        answer, steps = await llm_service.answer_with_tools_async(
            "question",
            "context",
            [],
            [],
            "",
            [agent_tools.builtin_knowledge_tool()],
            execute,
            before_round=before_round,
            runtime={"base_url": "https://llm.test/v1", "name": "test-model", "headers": {}},
        )
        assert answer == "steered answer"
        assert steps == []
        assert any(
            message.get("role") == "user" and message.get("content") == "prefer release v2"
            for message in calls[0]
        )

    anyio.run(case)


def test_llm_turn_stats_aggregate_tool_round_usage(monkeypatch) -> None:
    calls: list[list[dict]] = []

    def fake_completion(runtime, messages, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "probe", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            }
        return {
            "choices": [{"message": {"role": "assistant", "content": "finished"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }

    async def execute(tool, arguments, tool_call_id):
        return {"success": False, "output": "probe failed", "duration_ms": 7}

    monkeypatch.setattr(llm_service.model_runtime, "chat_completion", fake_completion)

    async def case() -> None:
        answer, steps = await llm_service.answer_with_tools_async(
            "question",
            "context",
            [],
            [],
            "",
            [{"type": "function", "function": {"name": "probe"}}],
            execute,
            runtime={"base_url": "https://llm.test/v1", "name": "test-model", "headers": {}},
        )
        assert answer == "finished"
        assert steps[0]["iteration"] == 0
        assert steps[1]["iteration"] == 0
        assert steps[1]["success"] is False
        assert steps[1]["error"] == "probe failed"
        assert llm_service.current_turn_stats() == {
            "prompt_tokens": 15,
            "completion_tokens": 5,
            "total_tokens": 20,
            "rounds": 2,
        }

    anyio.run(case)


def test_after_steer_starts_server_side_follow_up_run(monkeypatch) -> None:
    monkeypatch.setattr(chat_router, "generate_answer", lambda *args, **kwargs: "background answer")

    async def case() -> None:
        db.init_db()
        login_response = None
        async with await _client() as client:
            login_response = await _login(client)
            headers = _headers(login_response)
            session_response = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "after steer follow-up"},
            )
            assert session_response.status_code == 201, session_response.text
            session_id = session_response.json()["data"]["id"]
            session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
            assert session is not None
            tenant_id = int(login_response["active_tenant"]["id"])
            user_id = str(login_response["user"]["id"])

            stream = chat_router.stream_chat_events(
                session=session,
                session_id=session_id,
                tenant_id=tenant_id,
                payload={"query": "initial question"},
                agent_mode=False,
                channel="web",
                user_id=user_id,
                preferences={},
            )
            await anext(stream)
            assistant_id = chat_router._live_assistant_id(session_id)
            assert assistant_id
            steer_id = str(uuid.uuid4())
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO session_steer_messages(id, tenant_id, session_id, assistant_message_id, "
                    "content, delivery, mentioned_items, channel, status, user_message_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'follow up question', 'after', '[]', 'web', 'queued', '', ?, ?)",
                    (steer_id, tenant_id, session_id, assistant_id, db.utcnow(), db.utcnow()),
                )
            async for _ in stream:
                pass

            for _ in range(300):
                follow_up = db.fetch_one(
                    "SELECT * FROM messages WHERE session_id=? AND role='assistant' AND is_completed=1 "
                    "AND content='background answer' ORDER BY created_at, rowid DESC LIMIT 1",
                    (session_id,),
                )
                if follow_up and not chat_router._background_tasks:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("timed out waiting for the steer follow-up run")

            rows = db.fetch_all(
                "SELECT role, content, is_completed FROM messages WHERE session_id=? AND deleted_at IS NULL ORDER BY created_at, rowid",
                (session_id,),
            )
            assert [row["content"] for row in rows] == [
                "initial question",
                "background answer",
                "follow up question",
                "background answer",
            ]
            assert all(bool(row["is_completed"]) for row in rows)
            steer = db.fetch_one("SELECT * FROM session_steer_messages WHERE id=?", (steer_id,))
            assert steer is not None
            assert steer["status"] == "injected"
            assert steer["user_message_id"]

    anyio.run(case)


def test_steer_inject_retries_in_order_and_drops_after_three_failures(monkeypatch) -> None:
    original_insert = chat_router._insert_message
    snapshots: list[list[dict]] = []

    def flaky_insert(conn, **kwargs):
        if kwargs.get("content") == "older instruction":
            raise RuntimeError("simulated message insert failure")
        return original_insert(conn, **kwargs)

    async def fake_answer(*args, before_round=None, **kwargs):
        assert before_round is not None
        for _ in range(4):
            snapshots.append(await before_round())
        return "steered answer", []

    monkeypatch.setattr(chat_router, "_insert_message", flaky_insert)
    monkeypatch.setattr(chat_router, "answer_with_tools_async", fake_answer)
    monkeypatch.setattr(chat_router.model_runtime, "available", lambda runtime: True)
    monkeypatch.setattr(chat_router, "retrieve", lambda *args, **kwargs: [])

    async def case() -> None:
        db.init_db()
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            session_response = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "steer persistence order"},
            )
            assert session_response.status_code == 201, session_response.text
            session_id = session_response.json()["data"]["id"]
            session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
            assert session is not None
            tenant_id = int(login["active_tenant"]["id"])
            user_id = str(login["user"]["id"])
            assistant_id = chat_router._create_message(
                session_id=session_id,
                role="assistant",
                content="",
                request_id=db.new_id(),
                tenant_id=tenant_id,
                is_completed=False,
            )
            steer_ids = {"older": str(uuid.uuid4()), "newer": str(uuid.uuid4())}
            mentioned = [{"type": "knowledge_base", "id": "kb-1"}]
            with db.transaction() as conn:
                for created_at, (name, content) in enumerate(
                    (("older", "older instruction"), ("newer", "newer instruction"))
                ):
                    conn.execute(
                        "INSERT INTO session_steer_messages(id, tenant_id, session_id, "
                        "assistant_message_id, content, delivery, mentioned_items, channel, "
                        "status, user_message_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'inject', ?, 'web', 'queued', '', ?, ?)",
                        (
                            steer_ids[name],
                            tenant_id,
                            session_id,
                            assistant_id,
                            content,
                            json.dumps(mentioned),
                            f"2026-01-01T00:00:0{created_at}Z",
                            db.utcnow(),
                        ),
                    )

            stream = chat_router.stream_chat_events(
                session=session,
                session_id=session_id,
                tenant_id=tenant_id,
                payload={"query": "initial question"},
                agent_mode=True,
                channel="web",
                user_id=user_id,
                preferences={},
                precreated_assistant_message_id=assistant_id,
            )
            events = []
            async for frame in stream:
                data = next(
                    line[6:]
                    for line in frame.decode().splitlines()
                    if line.startswith("data: ")
                )
                events.append(json.loads(data))

            assert snapshots == [
                [],
                [],
                [{"content": "newer instruction"}],
                [],
            ]
            injected_events = [
                event for event in events if event.get("response_type") == "user_message_injected"
            ]
            assert [event["data"]["content"] for event in injected_events] == ["newer instruction"]

            steers = {
                row["id"]: row
                for row in db.fetch_all(
                    "SELECT * FROM session_steer_messages WHERE session_id=?", (session_id,)
                )
            }
            assert steers[steer_ids["older"]]["status"] == "dropped"
            assert steers[steer_ids["older"]]["user_message_id"] == ""
            assert steers[steer_ids["newer"]]["status"] == "injected"
            newer_message_id = steers[steer_ids["newer"]]["user_message_id"]
            assert newer_message_id

            persisted = db.fetch_all(
                "SELECT * FROM messages WHERE session_id=? AND role='user' "
                "AND deleted_at IS NULL ORDER BY created_at, rowid",
                (session_id,),
            )
            assert [row["content"] for row in persisted] == [
                "initial question",
                "newer instruction",
            ]
            assert json.loads(persisted[-1]["mentioned_items"]) == mentioned
            assert persisted[-1]["id"] == newer_message_id

    anyio.run(case)


def test_agent_chat_loads_skill_instructions_progressively(monkeypatch) -> None:
    seen_system: list[str] = []
    seen_tool_content: list[str] = []

    def fake_chat(messages, tools=None):
        if not seen_system:
            system = str(messages[0].get("content") or "")
            seen_system.append(system)
            assert "Available skills" in system
            assert '<skill name="release-check"' in system
            assert "Always verify the release manifest." not in system
            assert any(tool.get("function", {}).get("name") == "read_file" for tool in tools or [])
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-skill",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "skill://release-check/SKILL.md"}),
                        },
                    }
                ],
            }
        for message in messages:
            if message.get("role") == "tool" and message.get("tool_call_id") == "read-skill":
                seen_tool_content.append(str(message.get("content") or ""))
        return {"role": "assistant", "content": "Release checklist applied."}

    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)
    monkeypatch.setattr(app_config, "LLM_BASE_URL", "https://llm.test/v1")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = login["active_tenant"]["id"]
            now = db.utcnow()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO tenant_skills(id, tenant_id, sandbox_config_id, name, version, description, instructions, enabled, status, created_at, updated_at) "
                    "VALUES (?, ?, '', 'release-check', '1.0.0', 'Verify releases before publishing', 'Always verify the release manifest.', 1, 'ready', ?, ?)",
                    (db.new_id(), tenant_id, now, now),
                )

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Skill agent",
                    "config": {
                        "allowed_tools": [],
                        "mcp_selection_mode": "none",
                        "skills_selection_mode": "selected",
                        "selected_skills": ["release-check"],
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["data"]["id"]

            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "skill chat", "agent_id": agent_id, "agent_config": {}},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            response = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "Prepare the release"},
            )
            assert response.status_code == 200, response.text
            events = _events(response)
            call = next(event for event in events if event.get("response_type") == "tool_call")
            result = next(event for event in events if event.get("response_type") == "tool_result")
            assert call["data"]["tool_name"] == "read_file"
            assert "Always verify the release manifest." in result["data"]["output"]
            complete = next(event for event in events if event.get("response_type") == "complete")
            assert complete["data"]["final_content"] == "Release checklist applied."

    _run(case)
    assert any(
        "Always verify the release manifest." in content for content in seen_tool_content
    )


def test_agent_chat_offers_workspace_read_file_without_skills(monkeypatch) -> None:
    seen_system: list[str] = []
    seen_tool_content: list[str] = []
    seen_paths: list[str] = []
    sandbox_config_id = str(uuid.uuid4())

    def fake_chat(messages, tools=None):
        if not seen_system:
            seen_system.append(str(messages[0].get("content") or ""))
            names = {
                tool.get("function", {}).get("name") for tool in tools or []
            }
            assert "list_sandbox_files" in names
            assert {"write_sandbox_file", "edit_sandbox_file"} <= names
            read_file = next(
                tool
                for tool in tools or []
                if tool.get("function", {}).get("name") == "read_file"
            )
            assert "Sandbox files" in read_file["function"]["description"]
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-workspace",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "notes.txt"}),
                        },
                    }
                ],
            }
        for message in messages:
            if message.get("role") == "tool" and message.get("tool_call_id") == "read-workspace":
                seen_tool_content.append(str(message.get("content") or ""))
        return {"role": "assistant", "content": "Workspace file applied."}

    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)
    monkeypatch.setattr(app_config, "LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda _tenant_id, _session, path: seen_paths.append(path)
        or {"kind": "file", "size": 12},
    )
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: b"workspace ok\n"
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = login["active_tenant"]["id"]
            now = db.utcnow()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO tenant_sandbox_configs(id, tenant_id, name, description, sandbox_type, config, created_at, updated_at) "
                    "VALUES (?, ?, 'Read sandbox', '', 'cube', ?, ?, ?)",
                    (
                        sandbox_config_id,
                        tenant_id,
                        json.dumps({"cube": {"api_url": "https://cube.test"}}),
                        now,
                        now,
                    ),
                )

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "name": "Workspace reader",
                    "config": {
                        "allowed_tools": [],
                        "mcp_selection_mode": "none",
                        "skills_selection_mode": "none",
                    },
                },
            )
            assert agent.status_code == 201, agent.text
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "workspace chat", "agent_id": agent.json()["data"]["id"]},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE sessions SET sandbox_config_id=? WHERE id=?",
                    (sandbox_config_id, session_id),
                )

            response = await client.post(
                f"/api/v1/agent-chat/{session_id}",
                headers=headers,
                json={"query": "Read the workspace note"},
            )
            assert response.status_code == 200, response.text
            events = _events(response)
            result = next(event for event in events if event.get("response_type") == "tool_result")
            assert result["data"]["success"] is True
            assert "workspace ok" in result["data"]["output"]
            complete = next(event for event in events if event.get("response_type") == "complete")
            assert complete["data"]["final_content"] == "Workspace file applied."

    _run(case)
    assert seen_paths == ["/workspace/notes.txt"]
    assert any("workspace ok" in content for content in seen_tool_content)


def test_browser_account_pair_authorize_and_revoke(monkeypatch) -> None:
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            status = await client.get("/api/v1/me/browser", headers=headers)
            assert status.status_code == 200, status.text
            assert status.json()["data"]["enabled"] is True
            assert status.json()["data"]["connected"] is False

            paired = await client.post(
                "/api/v1/me/browser",
                headers=headers,
                json={"action": "pair", "origin": "https://weknora.test"},
            )
            assert paired.status_code == 200, paired.text
            link = paired.json()["data"]["pairing_link"]
            assert link.startswith("wss://weknora.test/api/v1/local-browser/extension#")
            token = link.rsplit("#", 1)[1]

            insecure_pair = await client.post(
                "/api/v1/me/browser",
                headers=headers,
                json={"action": "pair", "origin": "http://remote.example"},
            )
            assert insecure_pair.status_code == 400, insecure_pair.text

            invalid_origin = await client.post(
                "/api/v1/local-browser/extension/authorize",
                headers={
                    "Origin": "chrome-extension://not-a-chrome-id",
                    "Authorization": "Bearer " + token,
                },
                json={"action": "pair", "next_token": "n" * 43, "label": "Chrome test"},
            )
            assert invalid_origin.status_code == 403, invalid_origin.text

            authorized = await client.post(
                "/api/v1/local-browser/extension/authorize",
                headers={
                    "Origin": "chrome-extension://" + "a" * 32,
                    "Authorization": "Bearer " + token,
                },
                json={"action": "pair", "next_token": "n" * 43, "label": "Chrome test"},
            )
            assert authorized.status_code == 200, authorized.text
            assert authorized.json()["device_id"]

            account = await client.get("/api/v1/me/browser", headers=headers)
            assert account.status_code == 200
            assert account.json()["data"]["device"]["label"] == "Chrome test"

            revoked = await client.post(
                "/api/v1/me/browser",
                headers=headers,
                json={"action": "revoke"},
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["data"].get("device") is None
            assert revoked.json()["data"]["connected"] is False

    _run(case)


def test_browser_shutdown_releases_only_owned_leases() -> None:
    db.init_db()
    owned_scope = browser_skill.scope_key(7, "shutdown-owned")
    other_scope = browser_skill.scope_key(7, "shutdown-other")
    now = db.utcnow()
    future = browser_skill._timestamp(3600)
    with db.transaction() as conn:
        for scope, owner in ((owned_scope, browser_skill.NODE_ID), (other_scope, "other-node")):
            conn.execute(
                "INSERT OR REPLACE INTO browser_devices(scope_key, id, tenant, \"user\", label, token_hash, "
                "previous_hash, previous_until, expires_at, renew_after, created_at, last_seen_at, revoked_at, "
                "owner, owner_url, lease_key, lease_until) "
                "VALUES (?, ?, 7, ?, 'Shutdown test', ?, '', ?, ?, ?, ?, ?, NULL, ?, 'http://owner', ?, ?)",
                (
                    scope,
                    uuid.uuid4().hex,
                    scope,
                    secrets.token_hex(32),
                    now,
                    future,
                    future,
                    now,
                    now,
                    owner,
                    browser_skill._new_lease_key(),
                    future,
                ),
            )
    try:
        browser_skill.shutdown()
        owned = db.fetch_one("SELECT * FROM browser_devices WHERE scope_key=?", (owned_scope,))
        other = db.fetch_one("SELECT * FROM browser_devices WHERE scope_key=?", (other_scope,))
        assert owned["owner"] == ""
        assert owned["owner_url"] == ""
        assert owned["lease_until"] == ""
        assert other["owner"] == "other-node"
        assert other["lease_until"] == future
    finally:
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM browser_devices WHERE scope_key IN (?, ?)",
                (owned_scope, other_scope),
            )


def test_local_browser_tool_uses_daemon_session(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    calls: list[tuple[str, dict]] = []

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        calls.append((method, params))
        if method == "session.start":
            return {
                "session_id": "browser-task-1",
                "browser_instance_id": params["browser_instance_id"],
            }
        return {"text": "Example page", "tab_id": 1}

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    scope = browser_skill.scope_key(7, "alice")
    browser_skill.clear_tasks(scope)
    try:
        result = browser_skill.call_tool(
            scope,
            "chat",
            "navigate",
            {"url": "https://example.com", "session_id": "foreign", "browser_instance_id": "foreign"},
        )
        assert result["text"] == "Example page"
        assert calls[0][0] == "session.start"
        assert calls[0][1]["focused"] is False
        assert calls[1] == (
            "tool.navigate",
            {
                "url": "https://example.com",
                "wait_until": "domcontentloaded",
                "session_id": "browser-task-1",
            },
        )

        calls.clear()
        browser_skill.call_tool(scope, "chat", "observe", {"session_id": "foreign"})
        assert [method for method, _ in calls] == ["tool.observe"]
        assert calls[0][1] == {"session_id": "browser-task-1"}
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_status_reports_action_progress(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    scope = browser_skill.scope_key(7, "alice")

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        if method == "session.start":
            return {
                "session_id": "browser-task-progress",
                "browser_instance_id": params["browser_instance_id"],
            }
        if method == "tool.navigate" and params["url"].endswith("/timeout"):
            return {
                "url": "https://user:pass@example.com/slow?token=secret#fragment",
                "tab_id": 1,
                "reached": "timeout",
                "error_text": "document not ready",
            }
        if method == "tool.request_help":
            active = browser_skill.status(scope, "chat")
            assert active["action"] == "request_help"
            assert active["needs_help"] is True
            assert active["help_prompt"] == "Sign in"
            return {"outcome": "continued"}
        return {
            "url": "https://user:pass@example.com/path?token=secret#fragment",
            "tab_id": 1,
        }

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    browser_skill.clear_tasks(scope)
    try:
        browser_skill.call_tool(scope, "chat", "navigate", {"url": "https://example.com"})
        status = browser_skill.status(scope, "chat")
        assert status["action"] == "navigate"
        assert status["page_url"] == "https://example.com/path"
        assert status["last_error"] == ""

        with browser_skill._tasks_lock:
            task = browser_skill._tasks[scope]["chat"]
            task["action_finished"] = task["action_started"] + 1.25
        assert browser_skill.status(scope, "chat")["action_elapsed_ms"] == 1250

        browser_skill.call_tool(
            scope, "chat", "navigate", {"url": "https://example.com/timeout"}
        )
        status = browser_skill.status(scope, "chat")
        assert status["last_error"] == "document not ready"
        assert status["page_url"] == "https://example.com/slow"

        browser_skill.call_tool(scope, "chat", "request_help", {"prompt": "Sign in"})
        status = browser_skill.status(scope, "chat")
        assert status["action"] == "request_help"
        assert status["needs_help"] is False
        assert status["help_prompt"] == ""

        for value in [
            "javascript:alert(1)",
            "file:///tmp/test",
            "invalid",
            "https://example.com/" + "a" * 8192,
        ]:
            assert browser_skill._status_page_url(value) == ""
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_rpc_timeout_notifies_daemon(monkeypatch) -> None:
    received: list[dict] = []

    class FakeSocket:
        def __init__(self, *_args) -> None:
            self.connected = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, path: str) -> None:
            self.connected = path

        def sendall(self, payload: bytes) -> None:
            received.append(json.loads(payload.split(b"\n", 1)[0]))

        def recv(self, _size: int) -> bytes:
            raise TimeoutError

    class FakeSocketModule:
        socket = FakeSocket
        AF_UNIX = browser_skill.socket.AF_UNIX
        SOCK_STREAM = browser_skill.socket.SOCK_STREAM

    monkeypatch.setattr(browser_skill, "ensure_daemon", lambda: ("/tmp/fake-home", 9000))
    monkeypatch.setattr(browser_skill, "socket", FakeSocketModule())
    with pytest.raises(browser_skill.BrowserSkillError) as timed_out:
        browser_skill._rpc("tool.observe", {"session_id": "browser-task-1"}, timeout=0.05)
    assert timed_out.value.code == "timeout"
    assert [frame["method"] for frame in received] == ["tool.observe", "cancel"]
    assert received[1]["params"] == {"rpc_id": received[0]["id"]}


def test_browser_pause_interrupt_and_stop_cancel_active_rpc(monkeypatch) -> None:
    scope = browser_skill.scope_key(7, "alice")
    task_key = (scope, "chat")
    sent: list[dict] = []
    started = threading.Event()
    cancelled = threading.Event()
    cancel_calls: list[tuple[str, str]] = []

    class FakeSocket:
        def __init__(self, *_args) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, payload: bytes) -> None:
            sent.append(json.loads(payload.split(b"\n", 1)[0]))
            started.set()

        def recv(self, _size: int) -> bytes:
            assert cancelled.wait(2)
            return (
                json.dumps(
                    {
                        "id": sent[0]["id"],
                        "error": {"code": "user_aborted", "message": "command cancelled"},
                    }
                ).encode()
                + b"\n"
            )

    class FakeSocketModule:
        socket = FakeSocket
        AF_UNIX = browser_skill.socket.AF_UNIX
        SOCK_STREAM = browser_skill.socket.SOCK_STREAM

    def fake_cancel(path: Path, request_id: str) -> None:
        cancel_calls.append((str(path), request_id))
        cancelled.set()

    browser_skill.clear_tasks(scope)
    with browser_skill._tasks_lock:
        browser_skill._tasks[scope] = {
            "chat": {"task_id": "browser-task-active", "turn": browser_skill._fresh_turn()}
        }
    monkeypatch.setattr(browser_skill, "ensure_daemon", lambda: ("/tmp/fake-home", 9000))
    monkeypatch.setattr(browser_skill, "socket", FakeSocketModule())
    monkeypatch.setattr(browser_skill, "_device", lambda _scope: {"lease_key": "browser-lease"})
    monkeypatch.setattr(browser_skill, "_cancel_rpc", fake_cancel)
    errors: list[browser_skill.BrowserSkillError] = []

    def invoke() -> None:
        try:
            browser_skill._rpc(
                "tool.observe",
                {"session_id": "browser-task-active"},
                task_key=task_key,
            )
        except browser_skill.BrowserSkillError as exc:
            errors.append(exc)

    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        assert started.wait(2)
        assert browser_skill._task_rpc_calls(scope, "chat")
        browser_skill.pause_task(scope, "chat")
        worker.join(2)
        assert not worker.is_alive()
        assert errors and errors[0].code == "user_aborted"
        assert cancel_calls[-1][1] == sent[0]["id"]
        assert browser_skill.status(scope, "chat")["paused"] is True
        assert browser_skill._task_rpc_calls(scope, "chat") == []

        with browser_skill._tasks_lock:
            browser_skill._tasks[scope]["chat"]["rpc_calls"] = {("/tmp/fake-home/run/daemon.sock", "pending")}
        assert browser_skill.observe_event(
            scope,
            json.dumps(
                {
                    "event": "session.user_interrupt",
                    "payload": {"session_id": "browser-task-active"},
                }
            ),
        ) is True
        assert cancel_calls[-1] == ("/tmp/fake-home/run/daemon.sock", "pending")

        stop_calls: list[tuple[str, dict]] = []

        def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
            stop_calls.append((method, params))
            return {"stopped": ["browser-task-active"]}

        monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
        with browser_skill._tasks_lock:
            browser_skill._tasks[scope]["chat"]["rpc_calls"] = {("daemon.sock", "active-stop")}
        browser_skill.stop_task(scope, "chat")
        assert cancel_calls[-1] == ("daemon.sock", "active-stop")
        assert stop_calls == [
            ("session.stop", {"session_id": "browser-task-active", "all": False})
        ]
        assert browser_skill.task_id(scope, "chat") == ""
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_tab_close_preserves_agent_window(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    calls: list[tuple[str, dict, float]] = []
    state = {"tabs": [{"tab_id": 3}], "fail_main": False}

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        calls.append((method, params, timeout))
        if method == "session.start":
            return {
                "session_id": "browser-task-tab",
                "browser_instance_id": params["browser_instance_id"],
            }
        if method == "tool.tab_list":
            return {"tabs": state["tabs"]}
        if method == "tool.tab_create":
            return {"tab_id": 9}
        if method == "tool.tab_close" and params["tab_id"] == 3 and state["fail_main"]:
            raise browser_skill.BrowserSkillError("tab is not closable", code="permission_denied")
        return {"closed": params.get("tab_id")}

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    scope = browser_skill.scope_key(7, "alice")
    browser_skill.clear_tasks(scope)
    try:
        browser_skill.call_tool(scope, "chat", "tab_close", {"tab_id": 3})
        assert [call[0] for call in calls] == [
            "session.start",
            "tool.tab_list",
            "tool.tab_create",
            "tool.tab_close",
        ]
        assert calls[1][1] == {"session_id": "browser-task-tab", "scope": "agent"}
        assert calls[2][1] == {
            "session_id": "browser-task-tab",
            "url": "about:blank",
            "active": False,
        }
        assert calls[3][1] == {"tab_id": 3, "session_id": "browser-task-tab"}

        calls.clear()
        state["fail_main"] = True
        with pytest.raises(browser_skill.BrowserSkillError):
            browser_skill.call_tool(scope, "chat", "tab_close", {"tab_id": 3})
        assert [call[0] for call in calls] == [
            "tool.tab_list",
            "tool.tab_create",
            "tool.tab_close",
            "tool.tab_list",
            "tool.tab_close",
        ]
        assert calls[2][1]["tab_id"] == 3
        assert calls[4][1]["tab_id"] == 9
        assert calls[4][2] == 5
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_interrupted_and_unfinished_human_steps_pause_task(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    failure = {"error": browser_skill.BrowserSkillError("command cancelled", code="cancelled")}

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        if method == "session.start":
            return {
                "session_id": "browser-task-pause",
                "browser_instance_id": params["browser_instance_id"],
            }
        if method == "tool.request_help":
            return {"outcome": "expired"}
        raise failure["error"]

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    scope = browser_skill.scope_key(7, "alice")
    browser_skill.clear_tasks(scope)
    try:
        result = browser_skill.call_tool(scope, "chat", "request_help", {"prompt": "Sign in"})
        assert result["outcome"] == "expired"
        status = browser_skill.status(scope, "chat")
        assert status["paused"] is True
        assert status["needs_help"] is False
        with pytest.raises(browser_skill.BrowserSkillError) as paused:
            browser_skill.call_tool(scope, "chat", "observe", {})
        assert paused.value.code == "task_paused"

        browser_skill.clear_tasks(scope)
        with pytest.raises(browser_skill.BrowserSkillError) as cancelled:
            browser_skill.call_tool(scope, "chat", "observe", {})
        assert cancelled.value.code == "cancelled"
        assert browser_skill.status(scope, "chat")["paused"] is True

        browser_skill.clear_tasks(scope)
        failure["error"] = browser_skill.BrowserSkillError(
            "not_found: session not registered or already stopped", code="not_found"
        )
        with pytest.raises(browser_skill.BrowserSkillError):
            browser_skill.call_tool(scope, "chat", "observe", {})
        status = browser_skill.status(scope, "chat")
        assert status["paused"] is True
        assert browser_skill.task_id(scope, "chat") == ""
        browser_skill.control(scope, "chat", "resume")
        assert browser_skill.status(scope, "chat")["paused"] is False
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_human_steps_use_human_timeout_and_sanitize(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    calls: list[tuple[str, dict, float]] = []

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        calls.append((method, params, timeout))
        if method == "session.start":
            return {"session_id": "browser-task-human", "browser_instance_id": params["browser_instance_id"]}
        return {"outcome": "continued"}

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    scope = browser_skill.scope_key(7, "alice")
    browser_skill.clear_tasks(scope)
    try:
        browser_skill.call_tool(
            scope,
            "chat",
            "tab_borrow",
            {"tab_id": 4, "confirmation_timeout_ms": 999_999, "timeout_ms": 5},
        )
        method, params, timeout = calls[-1]
        assert method == "tool.tab_borrow"
        assert timeout == browser_skill.HUMAN_STEP_TIMEOUT
        assert params == {
            "tab_id": 4,
            "confirmation_timeout_ms": browser_skill.HUMAN_STEP_WAIT_MS,
            "session_id": "browser-task-human",
        }

        browser_skill.call_tool(
            scope,
            "chat",
            "request_help",
            {"prompt": "Sign in", "timeout_ms": "forever", "completion_criteria": {"text_exists": "Done"}},
        )
        method, params, timeout = calls[-1]
        assert method == "tool.request_help"
        assert timeout == browser_skill.HUMAN_STEP_TIMEOUT
        assert params == {
            "prompt": "Sign in",
            "timeout_ms": browser_skill.HUMAN_STEP_WAIT_MS,
            "session_id": "browser-task-human",
        }

        browser_skill.call_tool(scope, "chat", "observe", {})
        assert calls[-1][2] == browser_skill.LOCAL_RPC_TIMEOUT
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_paused_task_requires_explicit_resume(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    calls: list[tuple[str, dict]] = []

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        calls.append((method, params))
        if method == "session.start":
            return {
                "session_id": "browser-task-paused",
                "browser_instance_id": params["browser_instance_id"],
            }
        return {"ok": True}

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    scope = browser_skill.scope_key(7, "alice")
    browser_skill.clear_tasks(scope)
    try:
        browser_skill.call_tool(scope, "chat", "navigate", {"url": "https://example.com"})
        browser_skill.pause_task(scope, "chat")
        calls.clear()
        with pytest.raises(browser_skill.BrowserSkillError) as paused:
            browser_skill.call_tool(scope, "chat", "observe", {})
        assert paused.value.code == "task_paused"
        assert calls == []

        browser_skill.control(scope, "chat", "resume")
        assert calls[0][0] == "tool.snapshot"
        browser_skill.call_tool(scope, "chat", "observe", {})
        assert calls[-1][0] == "tool.observe"
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_window_close_discards_task_and_starts_fresh(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    calls: list[tuple[str, dict]] = []
    counter = 0

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        nonlocal counter
        calls.append((method, params))
        if method == "session.start":
            counter += 1
            return {
                "session_id": f"browser-task-{counter}",
                "browser_instance_id": params["browser_instance_id"],
            }
        return {"ok": True}

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    scope = browser_skill.scope_key(7, "alice")
    browser_skill.clear_tasks(scope)
    try:
        browser_skill.call_tool(scope, "chat", "observe", {})
        assert browser_skill.task_id(scope, "chat") == "browser-task-1"

        with browser_skill._tasks_lock:
            browser_skill._tasks[scope]["chat"]["stopping"] = True
        closed = json.dumps(
            {"event": "session.window_closed", "payload": {"session_id": "browser-task-1"}}
        )
        assert browser_skill.observe_event(scope, closed) is False
        assert browser_skill.task_id(scope, "chat") == "browser-task-1"
        with browser_skill._tasks_lock:
            browser_skill._tasks[scope]["chat"]["stopping"] = False

        interrupt = json.dumps(
            {"event": "session.user_interrupt", "payload": {"session_id": "browser-task-1"}}
        )
        assert browser_skill.observe_event(scope, interrupt) is True
        assert browser_skill.task_id(scope, "chat") == "browser-task-1"

        assert browser_skill.observe_event(scope, closed) is False
        assert browser_skill.task_id(scope, "chat") == ""
        status = browser_skill.status(scope, "chat")
        assert status["selected"] is True
        assert status["paused"] is True

        calls.clear()
        browser_skill.control(scope, "chat", "resume")
        assert calls[0][0] == "session.start"
        assert browser_skill.task_id(scope, "chat") == "browser-task-2"
        assert browser_skill.status(scope, "chat")["paused"] is False
    finally:
        browser_skill.clear_tasks(scope)


def test_browser_skill_finish_turn_retention_and_forget(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setattr(browser_skill, "connected", lambda scope: True)
    monkeypatch.setattr(browser_skill, "_device", lambda scope: {"lease_key": "browser-lease"})
    calls: list[tuple[str, dict]] = []
    counter = 0

    def fake_rpc(method: str, params: dict, timeout: float = 35, **_kwargs) -> dict:
        nonlocal counter
        calls.append((method, params))
        if method == "session.start":
            counter += 1
            return {
                "session_id": f"browser-task-{counter}",
                "browser_instance_id": params["browser_instance_id"],
            }
        return {"ok": True}

    monkeypatch.setattr(browser_skill, "_rpc", fake_rpc)
    scope = browser_skill.scope_key(7, "alice")
    browser_skill.clear_tasks(scope)
    try:
        browser_skill.begin_turn(scope, "chat")
        browser_skill.call_tool(scope, "chat", "navigate", {"url": "https://example.com"}, keep_open=False)
        browser_skill.finish_turn(scope, "chat")
        assert calls[-1] == ("session.stop", {"session_id": "browser-task-1", "all": False})
        assert not browser_skill.task_id(scope, "chat")

        browser_skill.begin_turn(scope, "chat")
        browser_skill.call_tool(scope, "chat", "observe", {}, keep_open=True)
        browser_skill.finish_turn(scope, "chat")
        assert browser_skill.task_id(scope, "chat") == "browser-task-2"
        assert calls[-1][0] == "tool.observe"

        browser_skill.begin_turn(scope, "chat")
        browser_skill.call_tool(scope, "chat", "observe", {})
        browser_skill.finish_turn(scope, "chat")
        assert calls[-1] == ("session.stop", {"session_id": "browser-task-2", "all": False})

        browser_skill.begin_turn(scope, "chat")
        browser_skill.call_tool(scope, "chat", "observe", {})
        browser_skill.forget(scope, ["chat"])
        assert calls[-1] == ("session.stop", {"session_id": "browser-task-3", "all": False})
        assert not browser_skill.task_id(scope, "chat")
    finally:
        browser_skill.clear_tasks(scope)


def test_local_browser_tool_sanitizes_screenshots_and_bounds_results(monkeypatch) -> None:
    tool = {
        "function": {"name": "local_browser"},
        "tool_name": "local_browser",
        "_session_id": "chat",
    }
    args = {"tenant_id": 7, "user_id": "alice"}
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8A"
        "AusB9Y9Z0ZkAAAAASUVORK5CYII="
    )

    monkeypatch.setattr(
        browser_skill,
        "call_tool",
        lambda *call_args, **call_kwargs: {
            "image_base64": png,
            "format": "png",
            "tab_id": 7,
            "dialogs": [{"type": "confirm"}],
        },
    )
    result = agent_tools.execute_tool(tool, {"method": "screenshot"}, **args)
    metadata = json.loads(result["output"])
    assert result["success"] is True
    assert metadata == {
        "tab_id": 7,
        "format": "png",
        "width": 1,
        "height": 1,
        "dialogs": [{"type": "confirm"}],
    }
    assert "image_base64" not in result["output"]
    assert result["raw"]["image_base64"] == png
    assert result["images"] == [f"data:image/png;base64,{png}"]

    monkeypatch.setattr(
        browser_skill,
        "call_tool",
        lambda *call_args, **call_kwargs: {"image_base64": "PHN2Zz4=", "format": "svg"},
    )
    result = agent_tools.execute_tool(tool, {"method": "screenshot"}, **args)
    assert result["success"] is False
    assert "invalid or too large" in result["output"]

    monkeypatch.setattr(
        browser_skill,
        "call_tool",
        lambda *call_args, **call_kwargs: {"text": "x" * 30000},
    )
    result = agent_tools.execute_tool(tool, {"method": "observe"}, **args)
    assert result["success"] is True
    assert len(result["output"]) <= agent_tools.MAX_BROWSER_OUTPUT_RUNES
    assert "output truncated" in result["output"]

    def fail_call(*call_args, **call_kwargs):
        raise browser_skill.BrowserSkillError(
            "tab borrowing was not approved",
            code="permission_denied",
            data={"reason": "confirmation_timeout", "payload": "x" * 9000},
        )

    monkeypatch.setattr(browser_skill, "call_tool", fail_call)
    result = agent_tools.execute_tool(tool, {"method": "tab_borrow", "tab_id": 3}, **args)
    error = json.loads(result["output"])
    assert result["success"] is False
    assert error["method"] == "tab_borrow"
    assert error["error"]["code"] == "permission_denied"
    assert error["error"]["data"] == {"truncated": True, "reason": "confirmation_timeout"}
    assert "confirmation is no longer pending" in error["recovery_hint"]
    assert "does not grant tab access" in error["recovery_hint"]


def test_local_browser_validates_flat_arguments_and_maps_text_budget(monkeypatch) -> None:
    tool = {
        "function": {"name": "local_browser"},
        "tool_name": "local_browser",
        "_session_id": "chat",
    }
    args = {"tenant_id": 7, "user_id": "alice"}
    calls: list[tuple] = []

    def fake_call(*call_args, **call_kwargs) -> dict:
        calls.append((call_args, call_kwargs))
        return {"text": "Example page", "ok": True}

    monkeypatch.setattr(browser_skill, "call_tool", fake_call)
    schema = browser_skill.local_browser_tool()["function"]["parameters"]["properties"]
    assert set(schema) == {"method", *browser_skill.BROWSER_TOOL_PROPERTIES}
    for rule in browser_skill.BROWSER_ARGUMENT_RULES.values():
        assert rule["fields"] <= set(schema)

    invalid = [
        {"method": "observe", "keep_open": "true"},
        {},
        {"method": "unknown"},
        {"method": "navigate", "params": {"url": "https://example.com"}},
        {"method": "navigate"},
        {"method": "navigate", "url": ""},
        {"method": "navigate", "url": 123},
        {"method": "navigate", "url": "https://example.com", "session_id": "foreign"},
        {"method": "observe", "browser_instance_id": "foreign"},
        {"method": "click", "url": "https://example.com"},
        {"method": "click"},
        {"method": "click", "ref": "e1", "selector": "button"},
        {"method": "click", "ref": ""},
        {"method": "fill", "ref": "e1"},
        {"method": "press"},
        {"method": "select", "ref": "e1", "values": [1]},
        {"method": "click", "ref": "e1", "modifiers": ["unknown"]},
        {"method": "tab_select"},
        {"method": "tab_select", "tab_id": 0.5},
        {"method": "wait_ms", "duration_ms": 10001},
        {"method": "wait_ms", "duration_ms": "100"},
        {"method": "window_resize", "width": 800},
        {"method": "window_resize", "width": 1, "height": 600},
        {"method": "emulate"},
        {"method": "emulate", "off": True, "overrides": {"width": 800}},
        {"method": "emulate", "overrides": "{}"},
        {"method": "emulate", "overrides": {"width": "800"}},
        {"method": "emulate", "overrides": {"height": -1}},
        {
            "method": "emulate",
            "overrides": {"user_agent_metadata": {"brands": [{"brand": "Chrome"}]}},
        },
        {"method": "request_help", "prompt": None},
        {
            "method": "request_help",
            "prompt": "Sign in",
            "completion_criteria": {"any": [{"text_exists": "Done"}]},
        },
        {"method": "request_help", "prompt": "Sign in", "targets": [{"ref": 1}]},
        {"method": "observe", "max_tokens": 750},
    ]
    for payload in invalid:
        result = agent_tools.execute_tool(tool, payload, **args)
        assert result["success"] is False, payload
        assert "Invalid browser arguments" in result["output"], payload
    assert calls == []

    result = agent_tools.execute_tool(
        tool, {"method": "observe", "max_text_chars": 3000}, **args
    )
    assert result["success"] is True
    assert calls[-1][0][2] == "observe"
    assert calls[-1][0][3] == {"max_tokens": 750}

    result = agent_tools.execute_tool(
        tool, {"method": "snapshot", "max_text_chars": 5}, **args
    )
    assert result["success"] is True
    assert calls[-1][0][3] == {"max_tokens": 2}

    result = agent_tools.execute_tool(tool, {"method": "click", "ref": "e1"}, **args)
    assert result["success"] is True
    assert calls[-1][0][3] == {"ref": "e1"}


def test_browser_recovery_hints_match_go() -> None:
    def hint(method: str, message: str, *, code: str, data: dict | None = None) -> str:
        return agent_tools._browser_recovery_hint(
            method,
            browser_skill.BrowserSkillError(message, code=code, data=data or {}),
        )

    borrowed = hint(
        "tab_select",
        "tab_select: borrow this tab before controlling it remotely",
        code="permission_denied",
    )
    assert "call tab_borrow once" in borrowed
    assert "before borrowing succeeds" in borrowed
    assert "navigate in the task's own tab" in borrowed

    invalid = hint("observe", "max_tokens is not supported", code="invalid_params")
    assert "documented fields at the top level" in invalid
    assert "guessing alternative parameter names" in invalid

    selector = hint("click", "selector missing", code="not_found", data={"reason": "selector_not_found"})
    assert "Do not guess selectors" in selector
    assert "iframe/shadow-root" in selector

    select = hint("select", "bad option", code="invalid_params", data={"reason": "single_select_value_count"})
    assert "requires a native select" in select
    assert "custom dropdown" in select

    borrowed_timeout = hint(
        "tab_borrow",
        "Timed out waiting for tab borrow confirmation",
        code="timeout",
        data={"reason": "confirmation_timeout"},
    )
    assert "confirmation is no longer pending" in borrowed_timeout
    assert "does not grant tab access" in borrowed_timeout
    assert "Do not call request_help" in borrowed_timeout

    unavailable = hint(
        "tab_borrow",
        "No user tab can display the borrow confirmation",
        code="unsupported",
        data={"reason": "confirmation_ui_unavailable"},
    )
    assert "HTTP(S) page in a regular browser window" in unavailable
    assert "Do not retry until a page is available" in unavailable

    fill = hint("fill", "value mismatch", code="cdp_failed", data={"reason": "fill_value_mismatch"})
    assert "remaining difference" in fill
    assert "blindly repeat fill" in fill

    committed = hint("click", "transport lost", code="cdp_failed", data={"effect_state": "committed"})
    assert "may already have taken effect" in committed
    assert "do not repeat the action blindly" in committed


def test_browser_tool_interprets_navigation_evaluate_and_human_results(monkeypatch) -> None:
    tool = {
        "function": {"name": "local_browser"},
        "tool_name": "local_browser",
        "_session_id": "chat",
    }
    args = {"tenant_id": 7, "user_id": "alice"}

    monkeypatch.setattr(
        browser_skill,
        "call_tool",
        lambda *call_args, **call_kwargs: {"reached": "timeout", "url": "https://example.com"},
    )
    result = agent_tools.execute_tool(tool, {"method": "navigate", "url": "https://example.com"}, **args)
    assert result["success"] is False
    assert "did not reach the requested loading phase" in result["output"]

    monkeypatch.setattr(
        browser_skill,
        "call_tool",
        lambda *call_args, **call_kwargs: {
            "ok": False,
            "error": {"text": "SyntaxError: Illegal return statement"},
        },
    )
    result = agent_tools.execute_tool(tool, {"method": "evaluate", "expression": "return 1"}, **args)
    assert result["success"] is False
    assert "top-level return" in result["output"]

    monkeypatch.setattr(
        browser_skill,
        "call_tool",
        lambda *call_args, **call_kwargs: {"outcome": "expired"},
    )
    result = agent_tools.execute_tool(tool, {"method": "request_help", "prompt": "Sign in"}, **args)
    assert result["success"] is False
    assert "human step did not complete" in result["output"]

    def fail_call(*call_args, **call_kwargs):
        raise browser_skill.BrowserSkillError(
            "renderer read timed out",
            code="browser_error",
            data={"reason": "renderer_read_timeout"},
        )

    monkeypatch.setattr(browser_skill, "call_tool", fail_call)
    result = agent_tools.execute_tool(tool, {"method": "observe"}, **args)
    error = json.loads(result["output"])
    assert result["success"] is False
    assert "Do not repeat snapshot/observe" in error["recovery_hint"]


def test_session_sandbox_binds_and_executes_remote_command(monkeypatch) -> None:
    db.init_db()
    user = db.fetch_one("SELECT * FROM users WHERE email=?", (ADMIN["email"],))
    assert user is not None
    tenant_id = int(user["tenant_id"])
    config_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tenant_sandbox_configs(id, tenant_id, name, description, sandbox_type, config, created_at, updated_at) "
            "VALUES (?, ?, 'Exec sandbox', '', 'cube', ?, ?, ?)",
            (
                config_id,
                tenant_id,
                json.dumps({"cube": {"api_url": "https://cube.test"}, "env_vars": {"BASE": "1"}}),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO sessions(id, tenant_id, title, user_id, sandbox_config_id, created_at, updated_at) "
            "VALUES (?, ?, 'Sandbox test', ?, ?, ?, ?)",
            (session_id, tenant_id, str(user["id"]), config_id, now, now),
        )

    class FakeClient:
        created = 0
        executed: list[tuple[str, int, dict[str, str]]] = []
        deleted: list[str] = []

        def __init__(self, provider: str, backend: dict, config_data: dict) -> None:
            assert provider == "cube"
            assert backend["api_url"] == "https://cube.test"

        def create(self, *, timeout_seconds: int):
            FakeClient.created += 1
            return remote_sandbox.RemoteSandbox("cube", "sandbox-1", "traffic", "envd", "cube.test")

        def exec(self, sandbox, command: str, *, timeout: int, env: dict[str, str]):
            FakeClient.executed.append((command, timeout, env))
            return remote_sandbox.RemoteExecResult("hello\n", "", 0, 7)

        def delete(self, sandbox) -> None:
            FakeClient.deleted.append(sandbox.sandbox_id)

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session is not None
    binding = session_sandbox.ensure(tenant_id, session)
    assert binding["sandbox_id"] == "sandbox-1"
    assert session_sandbox.ensure(tenant_id, session)["sandbox_id"] == "sandbox-1"
    assert FakeClient.created == 1

    result = session_sandbox.exec_command(
        tenant_id,
        session,
        "printf hello",
        work_dir="/workspace",
        timeout=30,
        env={"EXTRA": "2"},
    )
    assert result.stdout == "hello\n"
    assert FakeClient.executed == [
        ("cd -- /workspace && printf hello", 30, {"BASE": "1", "EXTRA": "2"})
    ]

    tool = {
        "function": {"name": "shell_exec"},
        "tool_name": "shell_exec",
        "_session_id": session_id,
    }
    tool_result = agent_tools.execute_tool(
        tool,
        {"command": "printf hello", "timeout_sec": 30, "env": {"EXTRA": "2"}},
        tenant_id=tenant_id,
        user_id=str(user["id"]),
    )
    assert tool_result["success"] is True
    payload = json.loads(tool_result["output"])
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "hello\n"

    session_sandbox.delete(tenant_id, session_id)
    assert FakeClient.deleted == ["sandbox-1"]
    assert session_sandbox._binding(tenant_id, session_id) is None


def test_session_sandbox_binds_and_executes_docker_command(monkeypatch) -> None:
    db.init_db()
    user = db.fetch_one("SELECT * FROM users WHERE email=?", (ADMIN["email"],))
    assert user is not None
    tenant_id = int(user["tenant_id"])
    config_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tenant_sandbox_configs(id, tenant_id, name, description, sandbox_type, config, created_at, updated_at) "
            "VALUES (?, ?, 'Docker exec sandbox', '', 'docker', ?, ?, ?)",
            (
                config_id,
                tenant_id,
                json.dumps({"docker": {"image": "weknora/sandbox:test"}, "env_vars": {"BASE": "1"}}),
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO sessions(id, tenant_id, title, user_id, sandbox_config_id, created_at, updated_at) "
            "VALUES (?, ?, 'Docker sandbox test', ?, ?, ?, ?)",
            (session_id, tenant_id, str(user["id"]), config_id, now, now),
        )

    class FakeDockerClient:
        executed: list[tuple[str, int, dict[str, str]]] = []
        deleted: list[str] = []

        def __init__(self, backend: dict, config_data: dict) -> None:
            assert backend["image"] == "weknora/sandbox:test"
            assert config_data["env_vars"] == {"BASE": "1"}

        def create(self, *, timeout_seconds: int):
            assert timeout_seconds == 3600
            return remote_sandbox.RemoteSandbox("docker", "container-1")

        def exec(self, sandbox, command: str, *, timeout: int, env: dict[str, str]):
            FakeDockerClient.executed.append((command, timeout, env))
            return remote_sandbox.RemoteExecResult("container-1\n", "", 0, 3)

        def delete(self, sandbox) -> None:
            FakeDockerClient.deleted.append(sandbox.sandbox_id)

    monkeypatch.setattr(docker_sandbox, "DockerSandboxClient", FakeDockerClient)
    session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session is not None
    binding = session_sandbox.ensure(tenant_id, session)
    assert binding["provider"] == "docker"
    assert binding["sandbox_id"] == "container-1"

    result = session_sandbox.exec_command(
        tenant_id, session, "hostname", work_dir="/tmp", timeout=30, env={"EXTRA": "2"}
    )
    assert result.stdout == "container-1\n"
    assert FakeDockerClient.executed == [
        ("cd -- /tmp && hostname", 30, {"BASE": "1", "EXTRA": "2"})
    ]

    with pytest.raises(session_sandbox.SessionSandboxError) as unsupported:
        session_sandbox.ensure_desktop(tenant_id, session)
    assert unsupported.value.code == "desktop_unsupported"

    session_sandbox.delete(tenant_id, session_id)
    assert FakeDockerClient.deleted == ["container-1"]
    assert session_sandbox._binding(tenant_id, session_id) is None


def test_session_sandbox_replaces_reaped_docker_container(monkeypatch) -> None:
    db.init_db()
    user = db.fetch_one("SELECT * FROM users WHERE email=?", (ADMIN["email"],))
    assert user is not None
    tenant_id = int(user["tenant_id"])
    config_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO tenant_sandbox_configs(id, tenant_id, name, description, sandbox_type, config, created_at, updated_at) "
            "VALUES (?, ?, 'Docker replacement sandbox', '', 'docker', ?, ?, ?)",
            (config_id, tenant_id, json.dumps({"docker": {}}), now, now),
        )
        conn.execute(
            "INSERT INTO sessions(id, tenant_id, title, user_id, sandbox_config_id, created_at, updated_at) "
            "VALUES (?, ?, 'Docker replacement test', ?, ?, ?, ?)",
            (session_id, tenant_id, str(user["id"]), config_id, now, now),
        )
        conn.execute(
            "INSERT INTO py_session_sandboxes(tenant_id, session_id, config_id, provider, sandbox_id, "
            "created_at, updated_at) VALUES (?, ?, ?, 'docker', 'reaped', ?, ?)",
            (tenant_id, session_id, config_id, now, now),
        )

    class FakeDockerClient:
        created = 0

        def __init__(self, backend: dict, config_data: dict) -> None:
            return None

        def exists(self, sandbox_id: str) -> bool:
            assert sandbox_id == "reaped"
            return False

        def create(self, *, timeout_seconds: int):
            FakeDockerClient.created += 1
            return remote_sandbox.RemoteSandbox("docker", "replacement")

        def delete(self, sandbox) -> None:
            return None

    monkeypatch.setattr(docker_sandbox, "DockerSandboxClient", FakeDockerClient)
    session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session is not None
    binding = session_sandbox.ensure(tenant_id, session)
    assert binding["sandbox_id"] == "replacement"
    assert FakeDockerClient.created == 1


def test_session_sandbox_deletes_new_remote_on_binding_write_failure(monkeypatch) -> None:
    db.init_db()
    _, tenant_id, session_id = _terminal_session(with_sandbox=True)
    session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session is not None

    class FakeClient:
        created = 0
        deleted: list[str] = []

        def __init__(self, provider: str, backend: dict, config_data: dict) -> None:
            return None

        def create(self, *, timeout_seconds: int):
            FakeClient.created += 1
            return remote_sandbox.RemoteSandbox("cube", "orphan-sandbox", "traffic", "envd", "cube.test")

        def delete(self, sandbox) -> None:
            FakeClient.deleted.append(sandbox.sandbox_id)

    from contextlib import contextmanager

    @contextmanager
    def fail_transaction():
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    monkeypatch.setattr(session_sandbox.db, "transaction", fail_transaction)
    with pytest.raises(RuntimeError, match="database unavailable"):
        session_sandbox.ensure(tenant_id, session)
    assert FakeClient.created == 1
    assert FakeClient.deleted == ["orphan-sandbox"]


def test_shell_exec_uses_sixty_four_kib_head_tail_limit(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session is not None

    class FakeClient:
        def __init__(self, provider: str, backend: dict, config_data: dict) -> None:
            return None

        def create(self, *, timeout_seconds: int):
            return remote_sandbox.RemoteSandbox("cube", "large-output", "traffic", "envd", "cube.test")

        def exec(self, sandbox, command: str, *, timeout: int, env: dict[str, str]):
            return remote_sandbox.RemoteExecResult("HEAD-MARKER" + "x" * 70000 + "TAIL-MARKER", "", 0, 7)

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    tool = {
        "function": {"name": "shell_exec"},
        "tool_name": "shell_exec",
        "_session_id": session_id,
    }
    result = agent_tools.execute_tool(
        tool,
        {"command": "large-output", "max_output_bytes": 65536, "max_stderr_bytes": 16384},
        tenant_id=tenant_id,
        user_id=str(user["id"]),
    )
    output = result["output"]
    assert result["success"] is True
    assert len(output.encode("utf-8")) <= agent_tools.MAX_SHELL_OUTPUT_BYTES
    assert "output truncated" not in output
    assert "HEAD-MARKER" in output
    assert "TAIL-MARKER" in output
    assert "...[truncated" in output


def _terminal_session(*, with_sandbox: bool) -> tuple[dict, str, str]:
    user = db.fetch_one("SELECT * FROM users WHERE email=?", (ADMIN["email"],))
    assert user is not None
    tenant_id = int(user["tenant_id"])
    config_id = str(uuid.uuid4()) if with_sandbox else ""
    session_id = str(uuid.uuid4())
    now = db.utcnow()
    with db.transaction() as conn:
        if with_sandbox:
            conn.execute(
                "INSERT INTO tenant_sandbox_configs(id, tenant_id, name, description, sandbox_type, config, created_at, updated_at) "
                "VALUES (?, ?, ?, '', 'cube', ?, ?, ?)",
                (
                    config_id,
                    tenant_id,
                    f"Terminal sandbox {config_id}",
                    json.dumps({"cube": {"api_url": "https://cube.test"}, "env_vars": {"BASE": "1"}}),
                    now,
                    now,
                ),
            )
        conn.execute(
            "INSERT INTO sessions(id, tenant_id, title, user_id, sandbox_config_id, created_at, updated_at) "
            "VALUES (?, ?, 'Terminal test', ?, ?, ?, ?)",
            (session_id, tenant_id, str(user["id"]), config_id, now, now),
        )
    return user, tenant_id, session_id


def _terminal_ticket(
    session_id: str,
    tenant_id: int,
    user_id: str,
    *,
    token_type: str = "sandbox_terminal",
    **claims: str,
) -> str:
    token_id = str(uuid.uuid4())
    extra = {"user_id": user_id, "session_id": session_id, "token_id": token_id}
    extra.update(claims)
    ticket, expires = issue_token(session_id, tenant_id, token_type, 120, extra=extra)
    payload = decode_token(ticket, token_type)
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO auth_tokens(id, user_id, token, token_type, expires_at, is_revoked, created_at, updated_at) "
            "VALUES (?, ?, ?, 'access_token', ?, 0, ?, ?)",
            (token_id, user_id, f"test-access-token-{token_id}", now, now, now),
        )
        conn.execute(
            "INSERT INTO py_sandbox_tickets(jti, token_type, tenant_id, user_id, session_id, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (payload["jti"], token_type, tenant_id, user_id, session_id, expires),
        )
    return ticket


class FakeTerminalSocket:
    def __init__(self, session_id: str, query: dict[str, str], protocols: list[str] | None = None) -> None:
        self.scope = {
            "path_params": {"session_id": session_id},
            "query_string": urllib.parse.urlencode(query).encode(),
            "subprotocols": protocols or [],
        }
        self.incoming: asyncio.Queue[dict | None] = asyncio.Queue()
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed: int | None = None
        self.close_reason = ""
        self.accepted_subprotocol = ""

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted_subprotocol = subprotocol or ""
        return None

    async def receive(self) -> dict:
        event = await self.incoming.get()
        assert event is not None
        return event

    async def send_text(self, value: str) -> None:
        self.sent_text.append(value)

    async def send_bytes(self, value: bytes) -> None:
        self.sent_bytes.append(value)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = code
        self.close_reason = reason


def test_sandbox_terminal_websocket_provisions_and_executes(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)

    class FakeTerminal:
        backend = "cube"
        pid = 0
        reattachable = False

        def __init__(self) -> None:
            self.reads = [b"hello\n"]
            self.written = bytearray()
            self.resizes: list[tuple[int, int]] = []
            self.closed = False

        def read(self, size: int = 32768) -> bytes:
            if self.reads:
                return self.reads.pop(0)
            raise SandboxTerminalClosed(0)

        def write(self, data: bytes) -> None:
            self.written.extend(data)

        def resize(self, cols: int, rows: int) -> None:
            self.resizes.append((cols, rows))

        def close(self) -> None:
            self.closed = True

    class FakeClient:
        created = 0
        terminal: FakeTerminal | None = None
        terminal_args: tuple | None = None

        def __init__(self, provider: str, backend: dict, config_data: dict) -> None:
            assert provider == "cube"
            assert backend["api_url"] == "https://cube.test"

        def create(self, *, timeout_seconds: int):
            FakeClient.created += 1
            return remote_sandbox.RemoteSandbox("cube", "terminal-sandbox", "traffic", "envd", "cube.test")

        def open_terminal(self, sandbox, **kwargs):
            FakeClient.terminal = FakeTerminal()
            FakeClient.terminal_args = (sandbox.sandbox_id, kwargs)
            return FakeClient.terminal

        def delete(self, sandbox) -> None:
            return None

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    ticket = _terminal_ticket(session_id, tenant_id, str(user["id"]))

    async def case() -> None:
        ws = FakeTerminalSocket(
            session_id,
            {"ticket": ticket, "provision": "1", "cols": "120", "rows": "40"},
        )
        ws.incoming.put_nowait({"type": "websocket.receive", "bytes": b"l"})
        ws.incoming.put_nowait({"type": "websocket.receive", "text": '{"type":"resize","cols":100,"rows":30}'})
        ws.incoming.put_nowait({"type": "websocket.disconnect"})
        await asyncio.wait_for(chat_router.sandbox_terminal_socket(ws), timeout=5)

        ready = json.loads(ws.sent_text[0])
        assert ready["type"] == "ready"
        assert ready["backend"] == "cube"
        assert ready["reattachable"] is False
        assert "pty_id" not in ready
        assert ws.sent_bytes == [b"hello\n"]
        assert ws.closed is None

    _run(case)
    assert FakeClient.created == 1
    terminal = FakeClient.terminal
    assert terminal is not None
    assert bytes(terminal.written) == b"l"
    assert terminal.resizes == [(100, 30)]
    assert terminal.closed is True
    sandbox_id, kwargs = FakeClient.terminal_args or ("", {})
    assert sandbox_id == "terminal-sandbox"
    assert kwargs == {"cols": 120, "rows": 40, "cwd": "/workspace", "env": {"BASE": "1"}}


def test_sandbox_terminal_websocket_rejects_invalid_bindings() -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    _, _, unbound_session_id = _terminal_session(with_sandbox=False)
    user_id = str(user["id"])
    ticket = _terminal_ticket(session_id, tenant_id, user_id)
    unbound_ticket = _terminal_ticket(unbound_session_id, tenant_id, user_id)
    bad_tenant_ticket, _ = issue_token(
        session_id,
        tenant_id,
        "sandbox_terminal",
        120,
        extra={"user_id": user_id, "session_id": session_id, "tenant_id": "not-a-number"},
    )

    async def case() -> None:
        missing = FakeTerminalSocket(session_id, {})
        await chat_router.sandbox_terminal_socket(missing)
        assert (missing.closed, missing.close_reason) == (1008, "AUTH_REVOKED")

        mismatch = FakeTerminalSocket(unbound_session_id, {"ticket": ticket})
        await chat_router.sandbox_terminal_socket(mismatch)
        assert (mismatch.closed, mismatch.close_reason) == (1008, "AUTH_REVOKED")

        malformed = FakeTerminalSocket(session_id, {"ticket": bad_tenant_ticket})
        await chat_router.sandbox_terminal_socket(malformed)
        assert (malformed.closed, malformed.close_reason) == (1008, "AUTH_REVOKED")

        no_sandbox = FakeTerminalSocket(unbound_session_id, {"ticket": unbound_ticket})
        await chat_router.sandbox_terminal_socket(no_sandbox)
        assert (no_sandbox.closed, no_sandbox.close_reason) == (1008, "SANDBOX_NOT_BOUND")

        replay = FakeTerminalSocket(unbound_session_id, {"ticket": unbound_ticket})
        await chat_router.sandbox_terminal_socket(replay)
        assert (replay.closed, replay.close_reason) == (1008, "AUTH_REVOKED")

    _run(case)


def test_sandbox_terminal_websocket_reports_reattachable_exit(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)

    class FakeTerminal:
        backend = "cube"
        pid = 4321
        reattachable = True

        def __init__(self) -> None:
            self.closed = False

        def read(self, size: int = 32768) -> bytes:
            raise SandboxTerminalClosed(7)

        def write(self, data: bytes) -> None:
            return None

        def resize(self, cols: int, rows: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    terminal = FakeTerminal()

    class FakeClient:
        def __init__(self, provider: str, backend: dict, config_data: dict) -> None:
            return None

        def create(self, *, timeout_seconds: int):
            return remote_sandbox.RemoteSandbox("cube", "terminal-sandbox", "traffic", "envd", "cube.test")

        def open_terminal(self, sandbox, **kwargs):
            return terminal

        def delete(self, sandbox) -> None:
            return None

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    ticket = _terminal_ticket(session_id, tenant_id, str(user["id"]))

    async def case() -> None:
        ws = FakeTerminalSocket(session_id, {"ticket": ticket, "pty_id": "4321", "provision": "1"})
        await asyncio.wait_for(chat_router.sandbox_terminal_socket(ws), timeout=5)
        assert ws.sent_text, (ws.closed, ws.close_reason)
        ready = json.loads(ws.sent_text[0])
        assert ready == {"type": "ready", "backend": "cube", "reattachable": True, "pty_id": 4321}
        exited = json.loads(ws.sent_text[1])
        assert exited == {"type": "exited", "exit_code": 7}
        assert (ws.closed, ws.close_reason) == (1000, "")
        assert terminal.closed is True

    _run(case)


def test_sandbox_terminal_websocket_caps_concurrent_connections(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    release = asyncio.Event()

    async def hold_terminal(*args, **kwargs) -> None:
        await release.wait()

    monkeypatch.setattr(chat_router, "_serve_sandbox_terminal", hold_terminal)

    async def case() -> None:
        tasks = [
            asyncio.create_task(
                chat_router.sandbox_terminal_socket(
                    FakeTerminalSocket(
                        session_id,
                        {"ticket": _terminal_ticket(session_id, tenant_id, str(user["id"]))},
                    )
                )
            )
            for _ in range(chat_router.TERMINAL_MAX_PER_SESSION)
        ]
        for _ in range(200):
            if chat_router._terminal_counts.get(session_id) == chat_router.TERMINAL_MAX_PER_SESSION:
                break
            await asyncio.sleep(0.01)
        assert chat_router._terminal_counts[session_id] == chat_router.TERMINAL_MAX_PER_SESSION

        extra = FakeTerminalSocket(
            session_id,
            {"ticket": _terminal_ticket(session_id, tenant_id, str(user["id"]))},
        )
        await asyncio.wait_for(chat_router.sandbox_terminal_socket(extra), timeout=5)
        assert (extra.closed, extra.close_reason) == (1008, "TOO_MANY_TERMINALS")

        release.set()
        await asyncio.gather(*tasks)
        assert session_id not in chat_router._terminal_counts

    _run(case)


def test_sandbox_terminal_websocket_rechecks_session_ownership(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    ticket = _terminal_ticket(session_id, tenant_id, str(user["id"]))

    async def yield_run_sync(func, *args, **kwargs):
        await asyncio.sleep(0.001)
        return func(*args, **kwargs)

    monkeypatch.setattr(chat_router.anyio.to_thread, "run_sync", yield_run_sync)

    class FakeTerminal:
        backend = "cube"
        pid = 0
        reattachable = False

        def __init__(self) -> None:
            self.closed = False

        def read(self, size: int = 32768) -> bytes:
            return b""

        def write(self, data: bytes) -> None:
            return None

        def resize(self, cols: int, rows: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    terminal = FakeTerminal()
    monkeypatch.setattr(
        session_sandbox,
        "open_terminal",
        lambda *args, **kwargs: ({"provider": "cube"}, terminal),
    )
    monkeypatch.setattr(chat_router, "TERMINAL_AUTH_RECHECK_SECONDS", 0.01)

    async def case() -> None:
        ws = FakeTerminalSocket(session_id, {"ticket": ticket})
        task = asyncio.create_task(chat_router.sandbox_terminal_socket(ws))
        for _ in range(200):
            if ws.sent_text:
                break
            await asyncio.sleep(0.01)
        assert json.loads(ws.sent_text[0])["type"] == "ready"

        with db.transaction() as conn:
            conn.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (db.utcnow(), session_id))
        for _ in range(200):
            if ws.closed is not None:
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(task, timeout=5)
        assert (ws.closed, ws.close_reason) == (1008, "AUTH_REVOKED")
        assert terminal.closed is True
        assert session_id not in chat_router._terminal_counts

    _run(case)


def test_sandbox_terminal_websocket_rechecks_access_token(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    ticket = _terminal_ticket(session_id, tenant_id, str(user["id"]))
    token_id = decode_token(ticket, "sandbox_terminal")["token_id"]

    async def yield_run_sync(func, *args, **kwargs):
        await asyncio.sleep(0.001)
        return func(*args, **kwargs)

    monkeypatch.setattr(chat_router.anyio.to_thread, "run_sync", yield_run_sync)

    class FakeTerminal:
        backend = "cube"
        pid = 0
        reattachable = False

        def __init__(self) -> None:
            self.closed = False

        def read(self, size: int = 32768) -> bytes:
            return b""

        def write(self, data: bytes) -> None:
            return None

        def resize(self, cols: int, rows: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    terminal = FakeTerminal()
    monkeypatch.setattr(
        session_sandbox,
        "open_terminal",
        lambda *args, **kwargs: ({"provider": "cube"}, terminal),
    )
    monkeypatch.setattr(chat_router, "TERMINAL_AUTH_RECHECK_SECONDS", 0.01)

    async def case() -> None:
        ws = FakeTerminalSocket(session_id, {"ticket": ticket})
        task = asyncio.create_task(chat_router.sandbox_terminal_socket(ws))
        for _ in range(200):
            if ws.sent_text:
                break
            await asyncio.sleep(0.01)
        assert json.loads(ws.sent_text[0])["type"] == "ready"

        with db.transaction() as conn:
            conn.execute("UPDATE auth_tokens SET is_revoked=1 WHERE id=?", (token_id,))
        for _ in range(200):
            if ws.closed is not None:
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(task, timeout=5)
        assert (ws.closed, ws.close_reason) == (1008, "AUTH_REVOKED")
        assert terminal.closed is True
        assert session_id not in chat_router._terminal_counts

    _run(case)


def test_docker_terminal_stream_writes_resizes_and_terminates() -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.incoming = bytearray(b"prompt$ ")
            self.sent = bytearray()
            self.closed = False

        def recv(self, size: int = 32768) -> bytes:
            data = bytes(self.incoming[:size])
            del self.incoming[:size]
            return data

        def sendall(self, data: bytes) -> None:
            self.sent.extend(data)

        def close(self) -> None:
            self.closed = True

    class FakeEngine:
        def __init__(self) -> None:
            self.resizes: list[tuple[str, int, int]] = []
            self.created: list[dict] = []
            self.started: list[tuple[str, bool]] = []

        def resize_exec(self, exec_id: str, cols: int, rows: int) -> None:
            self.resizes.append((exec_id, cols, rows))

        def inspect_exec(self, exec_id: str) -> dict:
            assert exec_id == "exec-1"
            return {"Running": False, "ExitCode": 7}

        def create_exec(self, container_id: str, payload: dict) -> str:
            self.created.append({"container_id": container_id, "payload": payload})
            return "kill-exec"

        def start_exec(self, exec_id: str, *, detached: bool = False):
            self.started.append((exec_id, detached))
            return None

    engine = FakeEngine()
    stream = FakeStream()
    session = docker_sandbox.DockerTerminalSession(
        engine,
        stream,
        "container-1",
        "exec-1",
        "terminal-token",
    )
    assert session.read() == b"prompt$ "
    session.write(b"echo hi\r")
    assert bytes(stream.sent) == b"echo hi\r"
    session.resize(120, 30)
    assert engine.resizes == [("exec-1", 120, 30)]
    with pytest.raises(SandboxTerminalClosed) as exited:
        session.read()
    assert exited.value.exit_code == 7
    session.close()
    session.close()
    assert engine.created[0]["container_id"] == "container-1"
    assert engine.created[0]["payload"]["Tty"] is False
    assert engine.created[0]["payload"]["Cmd"][-1] == "terminal-token"
    assert engine.started == [("kill-exec", True)]


def test_docker_open_terminal_uses_engine_pty(monkeypatch) -> None:
    class FakeStream:
        def __init__(self) -> None:
            self.sent = bytearray()
            self.closed = False

        def recv(self, size: int = 32768) -> bytes:
            return b""

        def sendall(self, data: bytes) -> None:
            self.sent.extend(data)

        def close(self) -> None:
            self.closed = True

    class FakeEngine:
        def __init__(self) -> None:
            self.create_payload: dict | None = None
            self.resizes: list[tuple[int, int]] = []
            self.stream = FakeStream()

        def create_exec(self, container_id: str, payload: dict) -> str:
            self.create_payload = payload
            return "exec-1"

        def start_exec(self, exec_id: str, *, detached: bool = False):
            return None if detached else self.stream

        def resize_exec(self, exec_id: str, cols: int, rows: int) -> None:
            self.resizes.append((cols, rows))

        def inspect_exec(self, exec_id: str) -> dict:
            return {"Running": False, "ExitCode": 4}

    engine = FakeEngine()

    def runner(argv: list[str], env: dict[str, str], timeout: float) -> docker_sandbox.CommandResult:
        assert argv[-1] == "container-1"
        if "{{.State.Status}}" in argv:
            return docker_sandbox.CommandResult(0, "running\n", "")
        return docker_sandbox.CommandResult(0, "true\n", "")

    client = docker_sandbox.DockerSandboxClient(
        {}, runner=runner, sweep_enabled=False, engine=engine
    )
    session = client.open_terminal(
        remote_sandbox.RemoteSandbox("docker", "container-1"),
        cols=120,
        rows=40,
        attach_pid=99,
        env={"EXTRA": "2"},
    )
    payload = engine.create_payload
    assert payload is not None
    assert payload["Tty"] is True
    assert payload["AttachStdin"] is True
    assert payload["WorkingDir"] == "/workspace"
    assert "TERM=xterm-256color" in payload["Env"]
    assert "EXTRA=2" in payload["Env"]
    assert engine.resizes == [(120, 40)]
    with pytest.raises(SandboxTerminalClosed):
        session.read()
    session.close()


def test_docker_engine_uses_exec_api_and_hijacked_stream() -> None:
    class FakeSocket:
        def __init__(self, responses: list[bytes]) -> None:
            self.responses = list(responses)
            self.sent = bytearray()
            self.closed = False
            self.timeout: float | None = None

        def recv(self, size: int) -> bytes:
            if not self.responses:
                return b""
            data = self.responses[0]
            chunk = data[:size]
            if len(data) > size:
                self.responses[0] = data[size:]
            else:
                self.responses.pop(0)
            return chunk

        def sendall(self, data: bytes) -> None:
            self.sent.extend(data)

        def settimeout(self, value: float | None) -> None:
            self.timeout = value

        def shutdown(self, how: int) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    create_socket = FakeSocket([
        b'HTTP/1.1 200 OK\r\nContent-Length: 18\r\n\r\n{"Id":"exec-1234"}'
    ])
    engine = docker_sandbox.DockerEngine()
    engine._connect = lambda timeout=None: create_socket  # type: ignore[method-assign]
    exec_id = engine.create_exec(
        "container-1",
        {"AttachStdin": True, "AttachStdout": True, "AttachStderr": True, "Tty": True, "Cmd": ["sh"]},
    )
    assert exec_id == "exec-1234"
    request = bytes(create_socket.sent)
    assert request.startswith(b"POST /v1.41/containers/container-1/exec HTTP/1.1\r\n")
    assert b'"Tty":true' in request
    assert create_socket.closed is True

    stream_socket = FakeSocket([
        b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n\r\nprompt$ "
    ])
    engine._connect = lambda timeout=None: stream_socket  # type: ignore[method-assign]
    stream = engine.start_exec("exec-1234")
    assert stream is not None
    assert stream.recv() == b"prompt$ "
    start_request = bytes(stream_socket.sent)
    assert start_request.startswith(b"POST /v1.41/exec/exec-1234/start HTTP/1.1\r\n")
    assert b"Connection: Upgrade\r\n" in start_request
    assert b"Upgrade: tcp\r\n" in start_request
    assert stream_socket.timeout is None


class _FrameResponse:
    def __init__(self, payload: bytes) -> None:
        self.stream = io.BytesIO(payload)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def close(self) -> None:
        self.closed = True


class _FrameConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _cube_terminal_frame(event: dict) -> bytes:
    return remote_sandbox._connect_envelope(json.dumps({"event": event}).encode())


def test_envd_cube_terminal_stream_parses_output_and_unary_control() -> None:
    frames = b"".join(
        [
            _cube_terminal_frame({"start": {"pid": 4321}}),
            _cube_terminal_frame({"data": {"pty": "aGVsbG8="}}),
            _cube_terminal_frame({"end": {"exitCode": 7, "exited": True}}),
            b"\x02" + b"\x00\x00\x00\x00",
        ]
    )
    response = _FrameResponse(frames)
    connection = _FrameConnection()

    class FakeClient:
        provider = "cube"

        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes]] = []

        def _terminal_rpc(self, sandbox, operation: str, payload: bytes) -> None:
            self.calls.append((operation, payload))

    client = FakeClient()
    session = remote_sandbox.EnvdTerminalSession(
        client,
        remote_sandbox.RemoteSandbox("cube", "sandbox-1"),
        scheme="https",
        dial_host="49983-sandbox-1.cube.test",
        authority="49983-sandbox-1.cube.test",
        connection=connection,
        response=response,
    )
    session.begin()
    assert session.pid == 4321
    assert session.reattachable is True
    assert session.read() == b"hello"
    session.write(b"ls\r")
    session.resize(100, 30)
    assert client.calls[0][0] == "SendInput"
    assert json.loads(client.calls[0][1])["input"]["pty"] == "bHMN"
    assert client.calls[1][0] == "Update"
    assert json.loads(client.calls[1][1])["pty"]["size"] == {"cols": 100, "rows": 30}
    with pytest.raises(SandboxTerminalClosed) as exited:
        session.read()
    assert exited.value.exit_code == 7
    session.close()
    assert response.closed is True
    assert connection.closed is True


def test_envd_e2b_terminal_stream_decodes_protobuf_events() -> None:
    start = remote_sandbox._field_bytes(1, remote_sandbox._field_varint(1, 99))
    data = remote_sandbox._field_bytes(2, remote_sandbox._field_bytes(3, b"world"))
    end = remote_sandbox._field_bytes(
        3,
        remote_sandbox._field_varint(1, remote_sandbox._zigzag(5))
        + remote_sandbox._field_varint(2, 1),
    )
    frames = b"".join(
        [
            remote_sandbox._connect_envelope(remote_sandbox._field_bytes(1, start)),
            remote_sandbox._connect_envelope(remote_sandbox._field_bytes(1, data)),
            remote_sandbox._connect_envelope(remote_sandbox._field_bytes(1, end)),
            b"\x02" + b"\x00\x00\x00\x00",
        ]
    )
    response = _FrameResponse(frames)
    connection = _FrameConnection()

    class FakeClient:
        provider = "e2b"

        def __init__(self) -> None:
            self.calls: list[tuple[str, bytes]] = []

        def _terminal_rpc(self, sandbox, operation: str, payload: bytes) -> None:
            self.calls.append((operation, payload))

    client = FakeClient()
    session = remote_sandbox.EnvdTerminalSession(
        client,
        remote_sandbox.RemoteSandbox("e2b", "sandbox-2"),
        scheme="https",
        dial_host="49983-sandbox-2.e2b.test",
        authority="49983-sandbox-2.e2b.test",
        connection=connection,
        response=response,
    )
    session.begin()
    assert session.pid == 99
    assert session.read() == b"world"
    session.write(b"x")
    session.resize(80, 24)
    assert [call[0] for call in client.calls] == ["SendInput", "Update"]
    assert client.calls[0][1] == remote_sandbox._e2b_terminal_input_request(99, b"x")
    assert client.calls[1][1] == remote_sandbox._e2b_terminal_update_request(99, 80, 24)
    with pytest.raises(SandboxTerminalClosed) as exited:
        session.read()
    assert exited.value.exit_code == 5
    session.close()


def test_session_sandbox_prepares_desktop(monkeypatch) -> None:
    db.init_db()
    _, tenant_id, session_id = _terminal_session(with_sandbox=True)
    session = db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    assert session is not None
    secret = "a" * 32

    class FakeClient:
        def __init__(self, provider: str, backend: dict, config_data: dict) -> None:
            assert provider == "cube"

        def create(self, *, timeout_seconds: int):
            return remote_sandbox.RemoteSandbox("cube", "desktop-sandbox", "traffic", "envd", "cube.test")

        def exec(self, sandbox, command: str, *, timeout: int, env: dict[str, str]):
            assert "start-desktop.sh" in command
            assert timeout == 90
            return remote_sandbox.RemoteExecResult(f"READY {secret}\n", "", 0, 7)

        def desktop_target(self, sandbox, value: str):
            assert sandbox.sandbox_id == "desktop-sandbox"
            assert value == secret
            return "wss://gateway.test/websockify", {"Host": "6080-desktop-sandbox.cube.app"}

        def delete(self, sandbox) -> None:
            return None

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)
    binding, value = session_sandbox.ensure_desktop(tenant_id, session)
    assert binding["sandbox_id"] == "desktop-sandbox"
    assert value == secret
    _, url, headers = session_sandbox.desktop_endpoint(tenant_id, session, value)
    assert url == "wss://gateway.test/websockify"
    assert headers["Host"] == "6080-desktop-sandbox.cube.app"

    class UnsupportedClient(FakeClient):
        def exec(self, sandbox, command: str, *, timeout: int, env: dict[str, str]):
            return remote_sandbox.RemoteExecResult("", "WEKNORA_DESKTOP_UNSUPPORTED", 2, 7)

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", UnsupportedClient)
    with pytest.raises(session_sandbox.SessionSandboxError) as unsupported:
        session_sandbox.ensure_desktop(tenant_id, session)
    assert unsupported.value.code == "desktop_unsupported"


def test_sandbox_desktop_websocket_relays_rfb(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    ticket = _terminal_ticket(
        session_id,
        tenant_id,
        str(user["id"]),
        token_type="sandbox_desktop",
    )
    unavailable_ticket = _terminal_ticket(
        session_id,
        tenant_id,
        str(user["id"]),
        token_type="sandbox_desktop",
    )

    class FakeUpstream:
        def __init__(self) -> None:
            self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
            self.sent: list[bytes | str] = []
            self.closed = False

        async def send(self, value: bytes | str) -> None:
            self.sent.append(value)

        async def recv(self) -> bytes:
            value = await self.incoming.get()
            if value is None:
                raise AssertionError("upstream closed")
            return value

        def __aiter__(self):
            return self

        async def __anext__(self):
            value = await self.incoming.get()
            if value is None:
                raise StopAsyncIteration
            return value

        async def close(self) -> None:
            self.closed = True

    upstream = FakeUpstream()

    async def fake_open(tenant: int, session: dict, *, provision: bool):
        assert tenant == tenant_id
        assert session["id"] == session_id
        assert provision is True
        return {"sandbox_id": "desktop-sandbox"}, upstream

    monkeypatch.setattr(session_sandbox, "open_desktop", fake_open)

    async def case() -> None:
        ws = FakeTerminalSocket(
            session_id,
            {"ticket": ticket, "provision": "1"},
            protocols=["binary"],
        )
        await upstream.incoming.put(b"RFB 003.008\n")
        task = asyncio.create_task(chat_router.sandbox_desktop_socket(ws))
        for _ in range(200):
            if ws.sent_bytes:
                break
            await asyncio.sleep(0.01)
        assert ws.sent_bytes == [b"RFB 003.008\n"]
        assert ws.accepted_subprotocol == "binary"

        await ws.incoming.put({"type": "websocket.receive", "bytes": b"client-frame"})
        for _ in range(200):
            if upstream.sent:
                break
            await asyncio.sleep(0.01)
        assert upstream.sent == [b"client-frame"]
        await ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(task, timeout=5)
        assert upstream.closed is True
        assert ws.closed is None
        assert session_id not in chat_router._desktop_sessions

        async def fail_open(*args, **kwargs):
            raise session_sandbox.SessionSandboxError("sandbox_not_bound", "session has no live sandbox")

        monkeypatch.setattr(session_sandbox, "open_desktop", fail_open)
        unavailable = FakeTerminalSocket(session_id, {"ticket": unavailable_ticket}, protocols=["binary"])
        await chat_router.sandbox_desktop_socket(unavailable)
        assert (unavailable.closed, unavailable.close_reason) == (1008, "SANDBOX_NOT_BOUND")

    _run(case)


def test_sandbox_desktop_reports_rebuilt_sandbox(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)

    class HandshakeUpstream:
        def __init__(self) -> None:
            self.closed = False

        async def recv(self) -> bytes:
            return chat_router.DESKTOP_RFB_VERSION

        async def close(self) -> None:
            self.closed = True

    upstreams = [HandshakeUpstream(), HandshakeUpstream(), HandshakeUpstream()]
    sandbox_ids = iter(["desktop-a", "desktop-b", "desktop-b"])

    async def fake_open(*args, **kwargs):
        return {"sandbox_id": next(sandbox_ids)}, upstreams.pop(0)

    async def fake_relay(*args, **kwargs):
        return None

    monkeypatch.setattr(session_sandbox, "open_desktop", fake_open)
    monkeypatch.setattr(chat_router, "_relay_desktop", fake_relay)

    async def connect() -> FakeTerminalSocket:
        ticket = _terminal_ticket(
            session_id,
            tenant_id,
            str(user["id"]),
            token_type="sandbox_desktop",
        )
        ws = FakeTerminalSocket(session_id, {"ticket": ticket}, protocols=["binary"])
        await chat_router.sandbox_desktop_socket(ws)
        return ws

    async def case() -> None:
        first = await connect()
        assert first.sent_bytes == [chat_router.DESKTOP_RFB_VERSION]
        assert first.closed is None

        rebuilt = await connect()
        assert rebuilt.sent_bytes == [chat_router.DESKTOP_RFB_VERSION]
        assert (rebuilt.closed, rebuilt.close_reason) == (1008, "SANDBOX_REBUILT")

        stable = await connect()
        assert stable.sent_bytes == [chat_router.DESKTOP_RFB_VERSION]
        assert stable.closed is None

    _run(case)


def test_desktop_lease_is_exclusive_across_replicas() -> None:
    db.init_db()
    _, tenant_id, session_id = _terminal_session(with_sandbox=True)
    assert chat_router._desktop_lease_acquire(session_id, tenant_id, "lease-a", "node-a") is True
    assert chat_router._desktop_lease_acquire(session_id, tenant_id, "lease-b", "node-b") is False
    assert chat_router._desktop_lease_renew(session_id, tenant_id, "lease-a") is True
    chat_router._desktop_lease_release(session_id, tenant_id, "lease-a")
    assert chat_router._desktop_lease_acquire(session_id, tenant_id, "lease-b", "node-b") is True
    chat_router._desktop_lease_release(session_id, tenant_id, "lease-b")
    assert db.fetch_one("SELECT 1 FROM py_desktop_leases WHERE session_id=?", (session_id,)) is None


def test_sandbox_desktop_rejects_busy_session(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    ticket = _terminal_ticket(
        session_id,
        tenant_id,
        str(user["id"]),
        token_type="sandbox_desktop",
    )
    assert chat_router._desktop_lease_acquire(session_id, tenant_id, "lease-other", "node-other") is True
    opened = False

    async def fake_open(*args, **kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("busy session must not open a desktop")

    monkeypatch.setattr(session_sandbox, "open_desktop", fake_open)

    async def case() -> None:
        ws = FakeTerminalSocket(session_id, {"ticket": ticket}, protocols=["binary"])
        await chat_router.sandbox_desktop_socket(ws)
        assert (ws.closed, ws.close_reason) == (1008, "DESKTOP_BUSY")

    try:
        _run(case)
    finally:
        chat_router._desktop_lease_release(session_id, tenant_id, "lease-other")
    assert opened is False


def test_sandbox_desktop_disconnects_when_idle(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    ticket = _terminal_ticket(
        session_id,
        tenant_id,
        str(user["id"]),
        token_type="sandbox_desktop",
    )

    class IdleUpstream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def recv(self) -> bytes:
            return chat_router.DESKTOP_RFB_VERSION

        async def __anext__(self):
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.closed = True

    upstream = IdleUpstream()

    async def fake_open(*args, **kwargs):
        return {"sandbox_id": "desktop-sandbox"}, upstream

    monkeypatch.setattr(session_sandbox, "open_desktop", fake_open)
    monkeypatch.setattr(chat_router, "DESKTOP_IDLE_SECONDS", 0.05)
    monkeypatch.setattr(chat_router, "DESKTOP_WATCH_INTERVAL", 0.01)

    async def case() -> None:
        ws = FakeTerminalSocket(session_id, {"ticket": ticket}, protocols=["binary"])
        task = asyncio.create_task(chat_router.sandbox_desktop_socket(ws))
        for _ in range(200):
            if ws.closed is not None:
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(task, timeout=5)
        assert (ws.closed, ws.close_reason) == (1008, "IDLE_DISCONNECTED")
        assert upstream.closed is True
        assert session_id not in chat_router._desktop_sessions
        assert session_id not in chat_router._desktop_last_activity

    _run(case)


def test_sandbox_desktop_rechecks_session_ownership(monkeypatch) -> None:
    db.init_db()
    user, tenant_id, session_id = _terminal_session(with_sandbox=True)
    ticket = _terminal_ticket(
        session_id,
        tenant_id,
        str(user["id"]),
        token_type="sandbox_desktop",
    )

    class IdleUpstream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self):
            return self

        async def recv(self) -> bytes:
            return chat_router.DESKTOP_RFB_VERSION

        async def __anext__(self):
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.closed = True

    upstream = IdleUpstream()

    async def fake_open(*args, **kwargs):
        return {"sandbox_id": "desktop-sandbox"}, upstream

    monkeypatch.setattr(session_sandbox, "open_desktop", fake_open)
    monkeypatch.setattr(chat_router, "DESKTOP_AUTH_RECHECK_SECONDS", 0.01)

    async def case() -> None:
        ws = FakeTerminalSocket(session_id, {"ticket": ticket}, protocols=["binary"])
        task = asyncio.create_task(chat_router.sandbox_desktop_socket(ws))
        for _ in range(200):
            if session_id in chat_router._desktop_sessions:
                break
            await asyncio.sleep(0.01)
        with db.transaction() as conn:
            conn.execute("UPDATE sessions SET deleted_at=? WHERE id=?", (db.utcnow(), session_id))
        for _ in range(200):
            if ws.closed is not None:
                break
            await asyncio.sleep(0.01)
        await asyncio.wait_for(task, timeout=5)
        assert (ws.closed, ws.close_reason) == (1008, "AUTH_REVOKED")
        assert upstream.closed is True

    _run(case)


def test_agent_tool_images_reach_vision_context(monkeypatch) -> None:
    image = "data:image/png;base64,aGVsbG8="
    calls: list[list[dict]] = []

    def fake_chat(messages, tools=None):
        calls.append(json.loads(json.dumps(messages)))
        if len(calls) == 1:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "local_browser", "arguments": '{"method":"screenshot"}'},
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {"name": "local_browser", "arguments": '{"method":"screenshot"}'},
                    },
                ],
            }
        return {"role": "assistant", "content": "screenshot inspected"}

    async def execute(tool, arguments, tool_call_id):
        return {"success": True, "output": '{"width":1}', "images": [image], "duration_ms": 1}

    monkeypatch.setattr(llm_service, "_remote_chat", fake_chat)

    async def case() -> None:
        tools = [{"type": "function", "function": {"name": "local_browser"}}]
        answer, steps = await llm_service.answer_with_tools_async(
            "look",
            "",
            [],
            [],
            "",
            tools,
            execute,
            runtime={"base_url": "https://llm.test", "supports_vision": True},
        )
        assert answer == "screenshot inspected"
        assert steps[-1]["tool_name"] == "local_browser"
        assistant_index = next(
            index for index, message in enumerate(calls[1]) if message.get("tool_calls")
        )
        assert [message["role"] for message in calls[1][assistant_index + 1 :]] == [
            "tool",
            "tool",
            "user",
            "user",
        ]
        assert calls[1][-1]["role"] == "user"
        assert calls[1][-1]["content"][1] == {"type": "image_url", "image_url": {"url": image}}

        calls.clear()
        await llm_service.answer_with_tools_async(
            "look",
            "",
            [],
            [],
            "",
            tools,
            execute,
            runtime={"base_url": "https://llm.test"},
        )
        assert calls[1][-1]["role"] == "tool"
        assert "cannot view them" in calls[1][-1]["content"]

    _run(case)


def test_browser_extension_gateway_relays_authenticated_handshake(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setenv("BROWSERSKILL_MAX_CONNECTIONS", "1")
    user = db.fetch_one("SELECT * FROM users WHERE email=?", (ADMIN["email"],))
    assert user is not None
    scope = browser_skill.scope_key(int(user["tenant_id"]), str(user["id"]))
    token = secrets.token_urlsafe(32)
    now = db.utcnow()
    future = chat_router._browser_timestamp(24 * 3600)
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO browser_devices(scope_key, id, tenant, \"user\", label, token_hash, "
            "previous_hash, previous_until, expires_at, renew_after, created_at, last_seen_at, revoked_at, "
            "owner, owner_url, lease_key, lease_until) "
            "VALUES (?, ?, ?, ?, 'Gateway test', ?, '', ?, ?, ?, ?, ?, NULL, '', '', '', ?)",
            (
                scope,
                uuid.uuid4().hex,
                int(user["tenant_id"]),
                str(user["id"]),
                browser_skill.token_hash(token),
                now,
                future,
                future,
                now,
                now,
                now,
            ),
        )

    class FakeUpstream:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.closed = False
            self.replies = [
                json.dumps({"id": "handshake-1", "result": {"protocol_version": "1.3"}}),
            ]

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        async def recv(self) -> str:
            if self.replies:
                return self.replies.pop(0)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def close(self) -> None:
            self.closed = True

    upstream = FakeUpstream()
    monkeypatch.setattr(browser_skill, "ensure_daemon", lambda: ("/tmp/fake-browserskill", 9000))

    async def fake_connect(port: int, origin: str) -> FakeUpstream:
        assert port == 9000
        assert origin.startswith("chrome-extension://")
        return upstream

    monkeypatch.setattr(browser_skill, "connect_upstream", fake_connect)
    handshake = {
        "id": "handshake-1",
        "method": "system.handshake",
        "params": {
            "client": "browser-skill-extension",
            "version": "0.3.0",
            "protocol_version": "1.3",
            "instance_id": "untrusted",
        },
    }
    class FakeWebSocket:
        def __init__(self) -> None:
            self.headers = {"origin": "chrome-extension://" + "a" * 32}
            self.scope = {"subprotocols": [browser_skill.AUTH_PROTOCOL + token]}
            self.incoming = asyncio.Queue()
            self.sent: list[str] = []
            self.closed: int | None = None
            self.accepted_subprotocol = ""

        async def accept(self, subprotocol: str | None = None) -> None:
            self.accepted_subprotocol = subprotocol or ""

        async def receive_text(self) -> str:
            value = await self.incoming.get()
            if value is None:
                raise chat_router.WebSocketDisconnect()
            return value

        async def send_text(self, value: str) -> None:
            self.sent.append(value)

        async def close(self, code: int = 1000) -> None:
            self.closed = code

    async def case() -> None:
        occupied = browser_skill.LiveConnection("occupied", "device", "lease", object())
        browser_skill._live[occupied.scope] = occupied
        try:
            blocked = FakeWebSocket()
            await chat_router.browser_extension_gateway(blocked)
            assert blocked.closed == 1013
        finally:
            browser_skill._live.pop(occupied.scope, None)

        ws = FakeWebSocket()
        await ws.incoming.put(json.dumps(handshake))
        task = asyncio.create_task(chat_router.browser_extension_gateway(ws))
        for _ in range(200):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        assert ws.accepted_subprotocol == browser_skill.AUTH_PROTOCOL + token
        reply = json.loads(ws.sent[0])
        assert reply["result"]["protocol_version"] == "1.3"
        device = db.fetch_one("SELECT * FROM browser_devices WHERE scope_key=?", (scope,))
        assert device["lease_key"]
        assert device["owner"] == browser_skill.NODE_ID
        assert browser_skill.extension_version(scope) == "0.3.0"
        assert upstream.sent[0]["params"]["instance_id"] == device["lease_key"]
        assert upstream.sent[0]["params"]["instance_id"] != "untrusted"
        await ws.incoming.put(None)
        await asyncio.wait_for(task, timeout=5)
        device = db.fetch_one("SELECT * FROM browser_devices WHERE scope_key=?", (scope,))
        assert device.get("lease_until") == ""
        assert browser_skill.extension_version(scope) == ""
        assert upstream.closed is True

    _run(case)


def test_browser_extension_gateway_relays_frames_events_and_ui(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    user = db.fetch_one("SELECT * FROM users WHERE email=?", (ADMIN["email"],))
    assert user is not None
    scope = browser_skill.scope_key(int(user["tenant_id"]), str(user["id"]))
    token = secrets.token_urlsafe(32)
    now = db.utcnow()
    future = chat_router._browser_timestamp(24 * 3600)
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO browser_devices(scope_key, id, tenant, \"user\", label, token_hash, "
            "previous_hash, previous_until, expires_at, renew_after, created_at, last_seen_at, revoked_at, "
            "owner, owner_url, lease_key, lease_until) "
            "VALUES (?, ?, ?, ?, 'Gateway frame test', ?, '', ?, ?, ?, ?, ?, NULL, '', '', '', ?)",
            (
                scope,
                uuid.uuid4().hex,
                int(user["tenant_id"]),
                str(user["id"]),
                browser_skill.token_hash(token),
                now,
                future,
                future,
                now,
                now,
                now,
            ),
        )

    class FakeUpstream:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
            self.closed = False

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        async def recv(self) -> str:
            value = await self.incoming.get()
            if value is None:
                raise AssertionError("unreachable")
            return value

        async def close(self) -> None:
            self.closed = True

    upstream = FakeUpstream()
    awaitable_reply = json.dumps({"id": "handshake-1", "result": {"protocol_version": "1.3"}})
    upstream.incoming.put_nowait(awaitable_reply)
    monkeypatch.setattr(browser_skill, "ensure_daemon", lambda: ("/tmp/fake-browserskill", 9000))

    async def fake_connect(port: int, origin: str) -> FakeUpstream:
        assert port == 9000
        assert origin.startswith("chrome-extension://")
        return upstream

    monkeypatch.setattr(browser_skill, "connect_upstream", fake_connect)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.headers = {"origin": "chrome-extension://" + "a" * 32}
            self.scope = {"subprotocols": [browser_skill.AUTH_PROTOCOL + token]}
            self.incoming: asyncio.Queue[str | None] = asyncio.Queue()
            self.sent: list[str] = []
            self.closed: int | None = None
            self.accepted_subprotocol = ""

        async def accept(self, subprotocol: str | None = None) -> None:
            self.accepted_subprotocol = subprotocol or ""

        async def receive_text(self) -> str:
            value = await self.incoming.get()
            if value is None:
                raise chat_router.WebSocketDisconnect()
            return value

        async def send_text(self, value: str) -> None:
            self.sent.append(value)

        async def close(self, code: int = 1000) -> None:
            self.closed = code

    async def wait_until(predicate, message: str) -> None:
        for _ in range(500):
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(message)

    async def case() -> None:
        ws = FakeWebSocket()
        await ws.incoming.put(
            json.dumps(
                {
                    "id": "handshake-1",
                    "method": "system.handshake",
                    "params": {"version": "0.3.1", "protocol_version": "1.3"},
                }
            )
        )
        task = asyncio.create_task(chat_router.browser_extension_gateway(ws))
        await wait_until(lambda: bool(ws.sent), "gateway handshake did not finish")

        browser_skill._tasks.setdefault(scope, {})["chat"] = {
            "task_id": "browser-task-1",
            "paused": False,
            "turn": browser_skill._fresh_turn(),
        }
        direct = {"id": "rpc-1", "method": "tool.observe", "params": {}}
        await ws.incoming.put(json.dumps(direct))
        await wait_until(lambda: len(upstream.sent) >= 2, "extension frame was not relayed")
        assert upstream.sent[-1] == direct

        interrupt = {
            "event": "session.user_interrupt",
            "payload": {"session_id": "browser-task-1"},
        }
        await ws.incoming.put(json.dumps(interrupt))
        await wait_until(
            lambda: bool(browser_skill._tasks[scope]["chat"].get("paused")),
            "user interrupt did not pause the task",
        )
        assert len(upstream.sent) == 2

        after_interrupt = {"id": "rpc-2", "method": "tool.snapshot", "params": {}}
        await ws.incoming.put(json.dumps(after_interrupt))
        await wait_until(lambda: len(upstream.sent) >= 3, "relay stopped after interrupt")
        assert upstream.sent[-1] == after_interrupt

        preview = asyncio.create_task(browser_skill.ui_call(scope, "chat", "ui.task_preview"))
        await wait_until(lambda: len(upstream.sent) >= 4, "UI request was not relayed")
        request = upstream.sent[-1]
        assert request["method"] == "ui.task_preview"
        upstream.incoming.put_nowait(
            json.dumps({"id": request["id"], "result": {"image_base64": "dGVzdA=="}})
        )
        assert await asyncio.wait_for(preview, timeout=2) == {"image_base64": "dGVzdA=="}
        assert len(ws.sent) == 1

        upstream.incoming.put_nowait(json.dumps({"id": "daemon-1", "result": {"ok": True}}))
        await wait_until(lambda: len(ws.sent) >= 2, "daemon frame was not relayed")
        assert json.loads(ws.sent[-1]) == {"id": "daemon-1", "result": {"ok": True}}

        await ws.incoming.put(None)
        await asyncio.wait_for(task, timeout=5)
        device = db.fetch_one("SELECT * FROM browser_devices WHERE scope_key=?", (scope,))
        assert device.get("lease_until") == ""
        assert upstream.closed is True

    _run(case)


def test_browser_skill_routes_calls_to_owner_replica(monkeypatch) -> None:
    monkeypatch.setenv("BROWSERSKILL_CLUSTER_SECRET", "cluster-secret-" * 3)
    scope = browser_skill.scope_key(7, "alice")
    future = browser_skill._timestamp(60)
    monkeypatch.setattr(
        browser_skill,
        "_device",
        lambda _scope: {
            "id": "device-1",
            "tenant": 7,
            "user": "alice",
            "owner": "remote-node",
            "owner_url": "http://owner.test",
            "revoked_at": None,
            "expires_at": future,
            "lease_until": future,
        },
    )
    captured: dict[str, object] = {}

    def fake_post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> dict:
        captured.update({"url": url, "headers": headers, "body": body, "timeout": timeout})
        return {"data": {"text": "remote result"}}

    monkeypatch.setattr(browser_skill, "_post_internal", fake_post)
    result = browser_skill.call_tool(
        scope,
        "chat",
        "observe",
        {"session_id": "foreign", "max_text_chars": 500},
        keep_open=False,
    )
    assert result == {"text": "remote result"}
    assert captured["url"] == "http://owner.test/api/v1/local-browser/internal"
    body = captured["body"]
    assert isinstance(body, bytes)
    payload = json.loads(body)
    assert payload["node"] == "remote-node"
    assert payload["scope"] == {"tenant": 7, "user": "alice"}
    assert payload["operation"] == "call"
    assert payload["method"] == "observe"
    assert payload["params"] == {
        "tool_params": {"session_id": "foreign", "max_text_chars": 500},
        "keep_open": False,
    }
    assert captured["timeout"] == browser_skill.CLUSTER_RPC_TIMEOUT
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["X-Browser-Signature"] == browser_skill.sign_internal(
        headers["X-Browser-Timestamp"], headers["X-Browser-Nonce"], body
    )

    browser_skill.call_tool(scope, "chat", "tab_borrow", {"tab_id": 3})
    assert captured["timeout"] == browser_skill.HUMAN_STEP_TIMEOUT


def test_browser_internal_rpc_verifies_signature_and_replay(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setenv("BROWSERSKILL_BINARY", "/opt/browserskill")
    monkeypatch.setenv("BROWSERSKILL_CLUSTER_SECRET", "internal-secret-" * 3)
    tenant_id = 91
    user_id = "internal-rpc-user"
    scope = browser_skill.scope_key(tenant_id, user_id)
    now = db.utcnow()
    future = browser_skill._timestamp(3600)
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO browser_devices(scope_key, id, tenant, \"user\", label, token_hash, "
            "previous_hash, previous_until, expires_at, renew_after, created_at, last_seen_at, revoked_at, "
            "owner, owner_url, lease_key, lease_until) "
            "VALUES (?, ?, ?, ?, 'Internal RPC', ?, '', ?, ?, ?, ?, ?, NULL, ?, '', ?, ?)",
            (
                scope,
                uuid.uuid4().hex,
                tenant_id,
                user_id,
                browser_skill.token_hash(secrets.token_urlsafe(32)),
                now,
                future,
                future,
                now,
                now,
                browser_skill.NODE_ID,
                browser_skill._new_lease_key(),
                future,
            ),
        )
    body = json.dumps(
        {
            "node": browser_skill.NODE_ID,
            "scope": {"tenant": tenant_id, "user": user_id},
            "session": "",
            "operation": "status",
        },
        separators=(",", ":"),
    ).encode()
    error_body = json.dumps(
        {
            "node": browser_skill.NODE_ID,
            "scope": {"tenant": tenant_id, "user": user_id},
            "session": "chat",
            "operation": "call",
            "method": "fill",
            "params": {"tool_params": {"value": "x"}, "keep_open": False},
        },
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signature = browser_skill.sign_internal(timestamp, nonce, body)
    headers = {
        "Content-Type": "application/json",
        "X-Browser-Timestamp": timestamp,
        "X-Browser-Nonce": nonce,
        "X-Browser-Signature": signature,
    }
    error_nonce = secrets.token_hex(16)
    error_headers = {
        **headers,
        "X-Browser-Nonce": error_nonce,
        "X-Browser-Signature": browser_skill.sign_internal(timestamp, error_nonce, error_body),
    }

    def fail_call(*args, **kwargs):
        raise browser_skill.BrowserSkillError(
            "fill failed",
            code="cdp_failed",
            data={
                "reason": "fill_failed",
                "effect_state": "unknown",
                "page": "x" * 9000,
            },
        )

    monkeypatch.setattr(browser_skill, "call_tool", fail_call)

    async def case() -> None:
        async with await _client() as client:
            valid = await client.post(
                "/api/v1/local-browser/internal", headers=headers, content=body
            )
            assert valid.status_code == 200, valid.text
            assert valid.json()["data"]["enabled"] is True

            replay = await client.post(
                "/api/v1/local-browser/internal", headers=headers, content=body
            )
            assert replay.status_code == 409

            forged = await client.post(
                "/api/v1/local-browser/internal",
                headers={**headers, "X-Browser-Nonce": secrets.token_hex(16), "X-Browser-Signature": "00" * 32},
                content=body,
            )
            assert forged.status_code == 401

            bounded = await client.post(
                "/api/v1/local-browser/internal", headers=error_headers, content=error_body
            )
            assert bounded.status_code == 200
            rpc_error = bounded.json()["rpc_error"]
            assert rpc_error["code"] == "cdp_failed"
            assert rpc_error["data"] == {
                "truncated": True,
                "reason": "fill_failed",
                "effect_state": "unknown",
            }

    _run(case)


def test_browser_ui_call_routes_reply_to_preview(monkeypatch) -> None:
    db.init_db()
    monkeypatch.setattr(browser_skill, "task_id", lambda scope, session: "browser-task-1")

    class FakeConnection:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.sent_event = asyncio.Event()

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))
            self.sent_event.set()

    async def case() -> None:
        scope = browser_skill.scope_key(7, "alice")
        upstream = FakeConnection()
        live = browser_skill.LiveConnection(scope, "device-1", "lease-1", upstream)
        browser_skill.register_live(live)
        try:
            pending = asyncio.create_task(
                browser_skill.ui_call(scope, "chat", "ui.task_preview")
            )
            await asyncio.wait_for(upstream.sent_event.wait(), timeout=2)
            request = upstream.sent[0]
            assert request["method"] == "ui.task_preview"
            assert request["params"] == {"session_id": "browser-task-1"}
            browser_skill.receive_ui(
                scope,
                json.dumps(
                    {
                        "id": request["id"],
                        "result": {"image_base64": "aGVsbG8=", "format": "jpeg"},
                    }
                ),
            )
            result = await asyncio.wait_for(pending, timeout=2)
            assert result == {"image_base64": "aGVsbG8=", "format": "jpeg"}
        finally:
            browser_skill.unregister_live(live)

    _run(case)


def test_im_callback_routes_messages_to_agent_session() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            channel = await client.post(
                "/api/v1/agents/builtin-quick-answer/im-channels",
                headers=headers,
                json={
                    "platform": "wecom",
                    "name": "IM smoke",
                    "credentials": {"token": "callback-token"},
                },
            )
            assert channel.status_code == 200, channel.text
            channel_id = channel.json()["data"]["id"]
            callback_query = {
                "timestamp": "1700000000",
                "nonce": "callback-nonce",
                "msg_signature": "",
            }
            callback_query["msg_signature"] = hashlib.sha1(
                "".join(
                    sorted(
                        [
                            "callback-token",
                            callback_query["timestamp"],
                            callback_query["nonce"],
                            "callback-encrypt",
                        ]
                    )
                ).encode()
            ).hexdigest()
            callback_url = f"/api/v1/im/callback/{channel_id}?" + urllib.parse.urlencode(
                callback_query
            )

            payload = {
                "text": "你好，介绍一下知识库",
                "user_id": "im-user-1",
                "chat_id": "im-chat-1",
                "message_id": "im-message-1",
                "Encrypt": "callback-encrypt",
            }
            first = await client.post(callback_url, json=payload)
            assert first.status_code == 200, first.text
            data = first.json()["data"]
            assert data["received"] is True
            assert data["duplicate"] is False
            assert data["reply"]["text"]
            session_id = data["session_id"]

            duplicate = await client.post(callback_url, json=payload)
            assert duplicate.status_code == 200, duplicate.text
            assert duplicate.json()["data"]["duplicate"] is True
            assert duplicate.json()["data"]["session_id"] == session_id

            second = await client.post(
                callback_url,
                json={**payload, "message_id": "im-message-2", "text": "再补充一点"},
            )
            assert second.status_code == 200, second.text
            assert second.json()["data"]["session_id"] == session_id

            messages = db.fetch_all(
                "SELECT role, content FROM messages WHERE session_id=? AND deleted_at IS NULL ORDER BY created_at, rowid",
                (session_id,),
            )
            assert [row["content"] for row in messages if row["role"] == "user"] == [
                "你好，介绍一下知识库",
                "再补充一点",
            ]
            assert len([row for row in messages if row["role"] == "assistant"]) == 2

            challenge = await client.get(f"/api/v1/im/callback/{channel_id}?challenge=abc123")
            assert challenge.status_code == 200
            assert challenge.json() == "abc123"

    _run(case)


def test_knowledge_chat_persists_attachments_and_passes_images(monkeypatch) -> None:
    captured: dict = {}

    def fake_answer(question, context, rows, history, memory_prompt, runtime=None, images=None):
        captured.update(
            {
                "question": question,
                "context": context,
                "runtime": runtime,
                "images": images,
            }
        )
        return "attachment answer"

    monkeypatch.setattr(chat_router, "generate_answer", fake_answer)
    image = "data:image/png;base64,aGVsbG8="
    attachment = {
        "id": "attachment-1",
        "file_name": "notes.txt",
        "file_type": ".txt",
        "file_size": 12,
        "content": "attachment body",
        "line_count": 1,
        "is_truncated": False,
    }

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            session = await client.post("/api/v1/sessions", headers=headers, json={})
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            response = await client.post(
                f"/api/v1/knowledge-chat/{session_id}",
                headers=headers,
                json={
                    "query": "what is attached?",
                    "agent_enabled": False,
                    "attachments": [attachment],
                    "images": [image],
                },
            )
            assert response.status_code == 200, response.text
            assert any(event.get("response_type") == "complete" for event in _events(response))
            assert captured["images"] == [image]
            assert "untrusted reference data" in captured["context"]
            assert "attachment body" in captured["context"]

            row = db.fetch_one(
                "SELECT attachments FROM messages WHERE session_id=? AND role='user' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (session_id,),
            )
            assert json.loads(row["attachments"]) == [attachment]

    _run(case)


def test_im_image_message_downloads_and_passes_vision_input(monkeypatch) -> None:
    captured: dict = {}
    jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"x" * 32

    def fake_answer(question, context, rows, history, memory_prompt, runtime=None, images=None):
        captured.update({"question": question, "context": context, "images": images})
        return "image answer"

    monkeypatch.setattr(im_attachments, "download_attachment", lambda *_args: jpeg)
    monkeypatch.setattr(chat_router, "generate_answer", fake_answer)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/agents/builtin-quick-answer/im-channels",
                headers=headers,
                json={
                    "platform": "wecom",
                    "name": "WeCom media",
                    "credentials": {"token": "media-token"},
                },
            )
            assert created.status_code == 200, created.text
            channel = db.fetch_one(
                "SELECT * FROM im_channels WHERE id=?",
                (created.json()["data"]["id"],),
            )
            result = await im_handler.answer_message(
                channel,
                {
                    "platform": "wecom",
                    "user_id": "media-user",
                    "user_name": "media-user",
                    "chat_id": "",
                    "chat_type": "single",
                    "message_id": "media-message-1",
                    "thread_id": "",
                    "reply_request_id": "media-req-1",
                    "message_type": "image",
                    "content": "",
                    "file_key": "https://example.test/media",
                    "file_name": "media-message-1.png",
                    "extra": {"req_id": "media-req-1", "aes_key": "media-key"},
                },
            )
            assert result["error"] == ""
            assert result["answer"] == "image answer"
            assert captured["question"] == (
                "我上传了文件「media-message-1.png」。请确认已收到，并告知我接下来可以如何协助。"
            )
            assert captured["images"][0].startswith("data:image/jpeg;base64,")
            assert "File content extraction failed or is unsupported." in captured["context"]

            row = db.fetch_one(
                "SELECT attachments FROM messages WHERE session_id=? AND role='user'",
                (result["session_id"],),
            )
            attachments = json.loads(row["attachments"])
            assert attachments[0]["file_name"] == "media-message-1.png"
            assert attachments[0]["file_size"] == len(jpeg)
            assert db.fetch_one(
                "SELECT id FROM knowledges WHERE tenant_id=? AND file_name=?",
                (int(channel["tenant_id"]), "media-message-1.png"),
            ) is None

    _run(case)


def test_im_file_message_persists_to_channel_knowledge_base(monkeypatch) -> None:
    content = "IM attachment knowledge body".encode("utf-8")

    def fake_answer(question, context, rows, history, memory_prompt, runtime=None, images=None):
        return "file answer"

    monkeypatch.setattr(im_attachments, "download_attachment", lambda *_args: content)
    monkeypatch.setattr(chat_router, "generate_answer", fake_answer)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "IM attachment knowledge"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            created = await client.post(
                "/api/v1/agents/builtin-quick-answer/im-channels",
                headers=headers,
                json={
                    "platform": "wecom",
                    "name": "WeCom file knowledge",
                    "credentials": {"token": "file-token"},
                    "knowledge_base_id": kb_id,
                },
            )
            assert created.status_code == 200, created.text
            channel = db.fetch_one(
                "SELECT * FROM im_channels WHERE id=?",
                (created.json()["data"]["id"],),
            )
            message = {
                "platform": "wecom",
                "user_id": "file-user",
                "user_name": "file-user",
                "chat_id": "",
                "chat_type": "single",
                "message_id": "file-message-1",
                "thread_id": "",
                "reply_request_id": "file-req-1",
                "message_type": "file",
                "content": "",
                "file_key": "https://example.test/file",
                "file_name": "notes.txt",
                "extra": {"req_id": "file-req-1", "aes_key": "file-key"},
            }

            result = await im_handler.answer_message(channel, message)
            await _drain_im_background_tasks()
            assert result["error"] == ""
            assert result["answer"] == "file answer"
            rows = db.fetch_all(
                "SELECT * FROM knowledges WHERE tenant_id=? AND knowledge_base_id=? AND file_name=? AND deleted_at IS NULL",
                (int(channel["tenant_id"]), kb_id, "notes.txt"),
            )
            assert len(rows) == 1
            assert rows[0]["channel"] == "wecom"
            assert rows[0]["parse_status"] == "completed"
            chunks = db.fetch_all(
                "SELECT content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL",
                (rows[0]["id"],),
            )
            assert any("IM attachment knowledge body" in row["content"] for row in chunks)

            duplicate = await im_handler.answer_message(
                channel,
                {**message, "message_id": "file-message-2", "reply_request_id": "file-req-2"},
            )
            await _drain_im_background_tasks()
            assert duplicate["answer"] == "file answer"
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM knowledges WHERE tenant_id=? AND knowledge_base_id=? AND file_name=? AND deleted_at IS NULL",
                (int(channel["tenant_id"]), kb_id, "notes.txt"),
            )["n"] == 1

    _run(case)


def test_im_attachment_persistence_failure_does_not_break_reply(monkeypatch, caplog) -> None:
    def fake_answer(question, context, rows, history, memory_prompt, runtime=None, images=None):
        return "answer despite persistence failure"

    def fail_ingest(*_args, **_kwargs):
        raise RuntimeError("knowledge storage unavailable")

    monkeypatch.setattr(im_attachments, "download_attachment", lambda *_args: b"failure body")
    monkeypatch.setattr(chat_router, "generate_answer", fake_answer)
    monkeypatch.setattr(knowledge_router, "_store_uploaded_file", fail_ingest)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "IM persistence failure"},
            )
            assert kb.status_code == 201, kb.text
            created = await client.post(
                "/api/v1/agents/builtin-quick-answer/im-channels",
                headers=headers,
                json={
                    "platform": "wecom",
                    "name": "WeCom failure",
                    "credentials": {"token": "failure-token"},
                    "knowledge_base_id": kb.json()["data"]["id"],
                },
            )
            assert created.status_code == 200, created.text
            channel = db.fetch_one(
                "SELECT * FROM im_channels WHERE id=?",
                (created.json()["data"]["id"],),
            )
            result = await im_handler.answer_message(
                channel,
                {
                    "platform": "wecom",
                    "user_id": "failure-user",
                    "message_id": "failure-message",
                    "message_type": "file",
                    "content": "",
                    "file_key": "https://example.test/failure",
                    "file_name": "failure.txt",
                },
            )
            await _drain_im_background_tasks()
            assert result["error"] == ""
            assert result["answer"] == "answer despite persistence failure"
            assert "IM attachment persistence failed for failure.txt" in caplog.text

    _run(case)


def test_im_unsupported_attachment_is_not_saved_and_keeps_reply(monkeypatch) -> None:
    def fake_answer(question, context, rows, history, memory_prompt, runtime=None, images=None):
        return "unsupported attachment answer"

    monkeypatch.setattr(im_attachments, "download_attachment", lambda *_args: b"MZ binary")
    monkeypatch.setattr(chat_router, "generate_answer", fake_answer)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "IM unsupported attachment"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            created = await client.post(
                "/api/v1/agents/builtin-quick-answer/im-channels",
                headers=headers,
                json={
                    "platform": "wecom",
                    "name": "WeCom unsupported",
                    "credentials": {"token": "unsupported-token"},
                    "knowledge_base_id": kb_id,
                },
            )
            assert created.status_code == 200, created.text
            channel = db.fetch_one(
                "SELECT * FROM im_channels WHERE id=?",
                (created.json()["data"]["id"],),
            )
            result = await im_handler.answer_message(
                channel,
                {
                    "platform": "wecom",
                    "user_id": "unsupported-user",
                    "message_id": "unsupported-message",
                    "message_type": "file",
                    "content": "",
                    "file_key": "https://example.test/unsupported",
                    "file_name": "payload.exe",
                },
            )
            await _drain_im_background_tasks()
            assert result["error"] == ""
            assert result["answer"] == "unsupported attachment answer"
            assert db.fetch_one(
                "SELECT id FROM knowledges WHERE tenant_id=? AND knowledge_base_id=? AND file_name=?",
                (int(channel["tenant_id"]), kb_id, "payload.exe"),
            ) is None

    _run(case)


def test_im_channel_credentials_signature_and_delivery(monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []

    def fake_http_json(method, url, payload=None, headers=None, timeout=15):
        sent.append((url, dict(payload or {})))
        return {"ok": True}

    monkeypatch.setattr(im_service, "_http_json", fake_http_json)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/agents/builtin-quick-answer/im-channels",
                headers=headers,
                json={
                    "platform": "telegram",
                    "name": "Telegram signed",
                    "mode": "webhook",
                    "output_mode": "full",
                    "locale": "en-US",
                    "session_mode": "thread",
                    "credentials": {"bot_token": "bot-token", "secret_token": "callback-secret"},
                },
            )
            assert created.status_code == 200, created.text
            channel = created.json()["data"]
            channel_id = channel["id"]
            assert channel["credentials"] == {"bot_token": "bot-token", "secret_token": "callback-secret"}
            assert channel["mode"] == "webhook"
            assert channel["output_mode"] == "full"
            assert channel["locale"] == "en-US"

            payload = {
                "message": {
                    "message_id": 701,
                    "text": "hello from telegram",
                    "from": {"id": 42, "username": "tester"},
                    "chat": {"id": 99, "type": "private"},
                }
            }
            rejected = await client.post(f"/api/v1/im/callback/{channel_id}", json=payload)
            assert rejected.status_code == 403

            accepted = await client.post(
                f"/api/v1/im/callback/{channel_id}",
                json=payload,
                headers={"X-Telegram-Bot-Api-Secret-Token": "callback-secret"},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["data"]["delivery"]["sent"] is True
            assert sent[-1][0] == "https://api.telegram.org/botbot-token/sendMessage"
            assert sent[-1][1]["chat_id"] == "99"
            assert sent[-1][1]["text"]

            overview = await client.get("/api/v1/im-channels", headers=headers)
            row = next(item for item in overview.json()["data"] if item["id"] == channel_id)
            assert "credentials" not in row
            assert row["agent_name"] == "快速问答"

    _run(case)


def test_im_channel_create_and_update_validation() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]

            agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={"name": f"IM validation {suffix}"},
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["data"]["id"]
            knowledge_base = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"IM validation KB {suffix}"},
            )
            assert knowledge_base.status_code == 201, knowledge_base.text
            knowledge_base_id = knowledge_base.json()["data"]["id"]
            channel_url = f"/api/v1/agents/{agent_id}/im-channels"

            missing_platform = await client.post(
                channel_url, headers=headers, json={"name": "missing platform"}
            )
            assert missing_platform.status_code == 400, missing_platform.text
            assert missing_platform.json()["error"]["message"] == "platform is required"

            invalid_platform = await client.post(
                channel_url, headers=headers, json={"platform": "generic"}
            )
            assert invalid_platform.status_code == 400, invalid_platform.text
            assert "platform must be one of:" in invalid_platform.json()["error"]["message"]

            invalid_locale = await client.post(
                channel_url,
                headers=headers,
                json={"platform": "wecom", "locale": "fr-FR"},
            )
            assert invalid_locale.status_code == 400, invalid_locale.text
            assert invalid_locale.json()["error"]["message"] == (
                "locale must be one of: 'en-US', 'ja-JP', 'ko-KR', 'ru-RU', 'zh-CN', 'zh-TW'"
            )

            missing_kb = await client.post(
                channel_url,
                headers=headers,
                json={"platform": "wecom", "knowledge_base_id": "missing-kb"},
            )
            assert missing_kb.status_code == 400, missing_kb.text
            assert missing_kb.json()["error"]["message"] == "knowledge base not found"

            missing_agent = await client.post(
                "/api/v1/agents/missing/im-channels",
                headers=headers,
                json={"platform": "wecom"},
            )
            assert missing_agent.status_code == 400, missing_agent.text
            assert missing_agent.json()["error"]["message"] == "agent not found"

            created = await client.post(
                channel_url,
                headers=headers,
                json={
                    "platform": "wecom",
                    "locale": "en-US",
                    "knowledge_base_id": knowledge_base_id,
                },
            )
            assert created.status_code == 200, created.text
            assert created.json()["data"]["name"] == ""
            channel_id = created.json()["data"]["id"]
            update_url = f"/api/v1/im-channels/{channel_id}"

            for invalid_update in (
                {"locale": "fr-FR"},
                {"knowledge_base_id": "missing-kb"},
                {"agent_id": "missing"},
            ):
                rejected = await client.put(
                    update_url, headers=headers, json=invalid_update
                )
                assert rejected.status_code == 400, rejected.text

            channels = await client.get(channel_url, headers=headers)
            assert channels.status_code == 200, channels.text
            row = next(item for item in channels.json()["data"] if item["id"] == channel_id)
            assert row["locale"] == "en-US"
            assert row["knowledge_base_id"] == knowledge_base_id
            assert row["agent_id"] == agent_id

            cleared = await client.put(
                update_url,
                headers=headers,
                json={"locale": "", "knowledge_base_id": ""},
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["data"]["locale"] == ""
            assert cleared.json()["data"]["knowledge_base_id"] == ""

            wechat = await client.post(
                channel_url,
                headers=headers,
                json={"platform": "wechat", "mode": "websocket", "output_mode": "stream"},
            )
            assert wechat.status_code == 200, wechat.text
            assert wechat.json()["data"]["mode"] == "longpoll"
            assert wechat.json()["data"]["output_mode"] == "full"

    _run(case)


def test_im_outbound_http_uses_url_guard(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return 200, {}, b'{"ok": true}', url

    monkeypatch.setattr(im_service.url_guard, "request_url", fake_request)
    result = im_service._http_json(
        "POST",
        "https://qyapi.weixin.qq.com/cgi-bin/message/send",
        {"msgtype": "text"},
        {"Authorization": "Bearer token"},
        timeout=9,
    )

    assert result == {"ok": True}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://qyapi.weixin.qq.com/cgi-bin/message/send"
    assert calls[0]["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer token",
    }
    assert json.loads(calls[0]["body"]) == {"msgtype": "text"}
    assert calls[0]["timeout"] == 9


def test_im_outbound_http_surfaces_guard_and_status_errors(monkeypatch) -> None:
    def blocked_request(_method, _url, **_kwargs):
        raise im_service.url_guard.URLGuardError("remote host resolves to a private address")

    monkeypatch.setattr(im_service.url_guard, "request_url", blocked_request)
    with pytest.raises(RuntimeError, match="private address"):
        im_service._http_json("GET", "https://internal.example.test/token")

    monkeypatch.setattr(
        im_service.url_guard,
        "request_url",
        lambda _method, url, **_kwargs: (403, {}, b'{"error":"denied"}', url),
    )
    with pytest.raises(RuntimeError, match=r'HTTP 403: \{"error":"denied"\}'):
        im_service._http_json("POST", "https://qyapi.weixin.qq.com/cgi-bin/message/send", {})


def test_im_channel_list_shapes_match_go() -> None:
    agent_summary_keys = {
        "id", "tenant_id", "agent_id", "platform", "name", "enabled", "mode",
        "output_mode", "locale", "knowledge_base_id", "bot_identity",
        "session_mode", "credentials_configured", "created_at", "updated_at",
    }
    overview_keys = {
        "id", "tenant_id", "agent_id", "agent_name", "platform", "name",
        "enabled", "mode", "output_mode", "locale", "session_mode",
        "bot_identity", "created_at", "updated_at",
    }

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]

            custom = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={"name": f"IM custom {suffix}"},
            )
            assert custom.status_code == 201, custom.text
            custom_id = custom.json()["data"]["id"]
            knowledge_base = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"IM KB {suffix}"},
            )
            assert knowledge_base.status_code == 201, knowledge_base.text
            knowledge_base_id = knowledge_base.json()["data"]["id"]

            with_credentials = await client.post(
                f"/api/v1/agents/{custom_id}/im-channels",
                headers=headers,
                json={
                    "platform": "wecom",
                    "name": "with credentials",
                    "knowledge_base_id": knowledge_base_id,
                    "credentials": {"token": "secret"},
                },
            )
            assert with_credentials.status_code == 200, with_credentials.text
            with_credentials_data = with_credentials.json()["data"]
            with_credentials_id = with_credentials_data["id"]
            assert with_credentials_data["credentials"] == {"token": "secret"}

            without_credentials = await client.post(
                f"/api/v1/agents/{custom_id}/im-channels",
                headers=headers,
                json={"platform": "wecom", "name": "without credentials"},
            )
            assert without_credentials.status_code == 200, without_credentials.text
            without_credentials_id = without_credentials.json()["data"]["id"]

            builtin_channel = await client.post(
                "/api/v1/agents/builtin-quick-answer/im-channels",
                headers=headers,
                json={"platform": "wecom", "name": "builtin channel"},
            )
            assert builtin_channel.status_code == 200, builtin_channel.text
            builtin_channel_id = builtin_channel.json()["data"]["id"]

            removed_agent = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={"name": f"Removed IM agent {suffix}"},
            )
            assert removed_agent.status_code == 201, removed_agent.text
            removed_agent_id = removed_agent.json()["data"]["id"]
            removed_channel = await client.post(
                f"/api/v1/agents/{removed_agent_id}/im-channels",
                headers=headers,
                json={"platform": "wecom", "name": "removed channel"},
            )
            assert removed_channel.status_code == 200, removed_channel.text
            removed_channel_id = removed_channel.json()["data"]["id"]
            deleted = await client.delete(
                f"/api/v1/agents/{removed_agent_id}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE im_channels SET created_at=? WHERE id=?",
                    ("2026-01-01T00:00:01Z", builtin_channel_id),
                )
                conn.execute(
                    "UPDATE im_channels SET created_at=? WHERE id=?",
                    ("2026-01-01T00:00:02Z", with_credentials_id),
                )

            agent_channels = await client.get(
                f"/api/v1/agents/{custom_id}/im-channels", headers=headers
            )
            assert agent_channels.status_code == 200, agent_channels.text
            agent_rows = {
                item["id"]: item for item in agent_channels.json()["data"]
            }
            assert set(agent_rows) == {with_credentials_id, without_credentials_id}
            assert all(set(item) == agent_summary_keys for item in agent_rows.values())
            assert "credentials" not in agent_rows[with_credentials_id]
            assert agent_rows[with_credentials_id]["credentials_configured"] is True
            assert agent_rows[without_credentials_id]["credentials_configured"] is False
            assert agent_rows[with_credentials_id]["knowledge_base_id"] == knowledge_base_id

            overview = await client.get(
                "/api/v1/im-channels",
                headers={**headers, "Accept-Language": "en-US,en;q=0.9"},
            )
            assert overview.status_code == 200, overview.text
            overview_rows = overview.json()["data"]
            by_id = {item["id"]: item for item in overview_rows}
            assert set(by_id[builtin_channel_id]) == overview_keys
            assert set(by_id[with_credentials_id]) == overview_keys
            assert by_id[builtin_channel_id]["agent_name"] == "Quick Answer"
            assert by_id[with_credentials_id]["agent_name"] == f"IM custom {suffix}"
            assert "credentials" not in by_id[with_credentials_id]
            assert "knowledge_base_id" not in by_id[with_credentials_id]
            assert removed_channel_id not in by_id
            assert overview_rows.index(by_id[with_credentials_id]) < overview_rows.index(
                by_id[builtin_channel_id]
            )

    _run(case)


def test_wechat_qrcode_binding_uses_ilink_api(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_json(url, *, headers=None, timeout=15):
        calls.append(url)
        if "/get_bot_qrcode" in url:
            return {"qrcode": "qr-token", "qrcode_img_content": "weixin://scan/qr-token"}
        if "/get_qrcode_status" in url:
            assert headers == {"iLink-App-ClientVersion": "1"}
            return {
                "status": "confirmed",
                "bot_token": "ilink-bot-token",
                "ilink_bot_id": "bot-1",
                "ilink_user_id": "user-1",
                "baseurl": "https://ilink.example",
            }
        raise AssertionError(url)

    monkeypatch.setattr(wechat_service, "_http_json", fake_http_json)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            qrcode = await client.post("/api/v1/wechat/qrcode", headers=headers)
            assert qrcode.status_code == 200, qrcode.text
            assert qrcode.json()["data"] == {
                "qrcode_url": "weixin://scan/qr-token",
                "qrcode": "qr-token",
            }

            missing = await client.post("/api/v1/wechat/qrcode/status", headers=headers, json={})
            assert missing.status_code == 400
            status = await client.post(
                "/api/v1/wechat/qrcode/status",
                headers=headers,
                json={"qrcode": "qr-token"},
            )
            assert status.status_code == 200, status.text
            assert status.json()["data"]["status"] == "confirmed"
            assert status.json()["data"]["credentials"]["ilink_bot_id"] == "bot-1"
            assert status.json()["data"]["baseurl"] == "https://ilink.example"
            assert "bot_type=3" in calls[0]
            assert "qrcode=qr-token" in calls[1]

    _run(case)


def test_sandbox_skill_bundle_is_stored_and_read_back() -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("demo/SKILL.md", "# Demo Skill\n\nRun the demo safely.\n")
        bundle.writestr("demo/scripts/run.py", "print('demo')\n")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            sandbox = await client.post(
                "/api/v1/sandbox-configs",
                headers=headers,
                json={"name": "Skill bundle sandbox", "sandbox_type": "docker", "config": {}},
            )
            assert sandbox.status_code == 201, sandbox.text
            config_id = sandbox.json()["data"]["id"]

            installed = await client.post(
                f"/api/v1/sandbox-configs/{config_id}/skills",
                headers=headers,
                files={"file": ("demo.zip", archive.getvalue(), "application/zip")},
            )
            assert installed.status_code == 202, installed.text
            skill_id = installed.json()["data"]["skill_id"]
            skill_response = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}",
                headers=headers,
            )
            assert skill_response.status_code == 200, skill_response.text
            skill = skill_response.json()["data"]
            assert skill["status"] == "ready"
            assert "Run the demo safely" in skill["instructions"]
            assert len(skill["bundle_sha256"]) == 64
            assert skill["install_session_id"] and skill["install_message_id"]

            files = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/files",
                headers=headers,
            )
            assert files.status_code == 200, files.text
            assert {item["path"] for item in files.json()["data"]} == {
                "demo/SKILL.md",
                "demo/scripts/run.py",
            }
            content = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/files/content",
                headers=headers,
                params={"path": "demo/scripts/run.py"},
            )
            assert content.status_code == 200, content.text
            assert content.json()["data"]["content"] == "print('demo')\n"
            assert content.json()["data"]["encoding"] == "utf-8"

            escaped = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/files/content",
                headers=headers,
                params={"path": "../secret"},
            )
            assert escaped.status_code == 404

            events = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/install-events",
                headers=headers,
            )
            assert events.status_code == 200
            assert "text/event-stream" in events.headers["content-type"]
            assert '"done": true' in events.text

            transcript = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/transcript",
                headers=headers,
            )
            assert transcript.status_code == 200, transcript.text
            assert "text/event-stream" in transcript.headers["content-type"]
            assert '"response_type": "install_prompt"' in transcript.text
            assert '"response_type": "complete"' in transcript.text

            reinstalled = await client.post(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/reinstall",
                headers=headers,
                json={"instructions": "Verify the bundled script before finishing."},
            )
            assert reinstalled.status_code == 202, reinstalled.text
            refreshed = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}",
                headers=headers,
            )
            assert refreshed.json()["data"]["status"] == "ready"
            assert refreshed.json()["data"]["install_message_id"] != skill["install_message_id"]

            guidance = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/guidance",
                headers=headers,
            )
            assert guidance.status_code == 200, guidance.text
            assert guidance.json()["data"] == {"accepting": False, "messages": []}

            steer_id = str(uuid.uuid4())
            rejected = await client.post(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/guidance",
                headers=headers,
                json={"expected_message_id": "run-1", "steer_id": steer_id, "content": "Install the CLI"},
            )
            assert rejected.status_code == 409, rejected.text

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE tenant_skills SET status='installing', install_message_id='run-1' WHERE id=?",
                    (skill_id,),
                )
            accepted = await client.post(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/guidance",
                headers=headers,
                json={"expected_message_id": "run-1", "steer_id": steer_id, "content": "Install the CLI"},
            )
            assert accepted.status_code == 202, accepted.text
            duplicate = await client.post(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/guidance",
                headers=headers,
                json={"expected_message_id": "run-1", "steer_id": steer_id, "content": "Install the CLI"},
            )
            assert duplicate.status_code == 202, duplicate.text
            run_guidance = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/guidance",
                headers=headers,
            )
            assert run_guidance.json()["data"]["accepting"] is True
            assert run_guidance.json()["data"]["messages"] == [
                {"id": steer_id, "content": "Install the CLI", "status": "pending"}
            ]

            with db.transaction() as conn:
                conn.execute("UPDATE tenant_skills SET status='ready' WHERE id=?", (skill_id,))
            finished = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/guidance",
                headers=headers,
            )
            assert finished.json()["data"]["messages"][0]["status"] == "unprocessed"

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE tenant_skills SET status='installing' WHERE id=?",
                    (skill_id,),
                )
            stopped = await client.post(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/stop",
                headers=headers,
            )
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["data"]["status"] == "failed"
            assert "stopped" in stopped.json()["data"]["error"]
            not_installing = await client.post(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}/stop",
                headers=headers,
            )
            assert not_installing.status_code == 200
            assert not_installing.json()["data"]["status"] == "failed"

    _run(case)


def test_skill_catalog_register_install_and_files(monkeypatch) -> None:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "SKILL.md",
            "---\nname: catalog-demo\nversion: 2.1.0\ndescription: Catalog demo skill\n---\n\nFollow the catalog instructions.\n",
        )
        bundle.writestr("scripts/run.py", "print('catalog')\n")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            registered = await client.post(
                "/api/v1/skills/catalog",
                headers=headers,
                files={"file": ("catalog-demo.zip", archive.getvalue(), "application/zip")},
            )
            assert registered.status_code == 201, registered.text
            catalog = registered.json()["data"]
            catalog_id = catalog["id"]
            assert catalog["name"] == "catalog-demo"
            assert catalog["version"] == "2.1.0"
            assert catalog["description"] == "Catalog demo skill"

            listed = await client.get("/api/v1/skills/catalog", headers=headers)
            assert listed.status_code == 200, listed.text
            row = next(item for item in listed.json()["data"] if item["id"] == catalog_id)
            assert row["installations"] == []

            files = await client.get(
                f"/api/v1/skills/catalog/{catalog_id}/files",
                headers=headers,
            )
            assert files.status_code == 200, files.text
            assert {item["path"] for item in files.json()["data"]} == {"SKILL.md", "scripts/run.py"}
            content = await client.get(
                f"/api/v1/skills/catalog/{catalog_id}/files/content",
                headers=headers,
                params={"path": "SKILL.md"},
            )
            assert content.status_code == 200, content.text
            assert "Follow the catalog instructions" in content.json()["data"]["content"]

            sandbox = await client.post(
                "/api/v1/sandbox-configs",
                headers=headers,
                json={"name": "Catalog sandbox", "sandbox_type": "docker", "config": {}},
            )
            assert sandbox.status_code == 201, sandbox.text
            config_id = sandbox.json()["data"]["id"]

            installed = await client.post(
                f"/api/v1/skills/catalog/{catalog_id}/install",
                headers=headers,
                json={"sandbox_config_ids": [config_id]},
            )
            assert installed.status_code == 202, installed.text
            skill_id = installed.json()["data"]["installs"][config_id]

            listed = await client.get("/api/v1/skills/catalog", headers=headers)
            row = next(item for item in listed.json()["data"] if item["id"] == catalog_id)
            assert row["installations"][0]["skill_id"] == skill_id
            assert row["installations"][0]["sandbox_config_name"] == "Catalog sandbox"
            assert row["installations"][0]["status"] == "ready"

            visible = await client.get(
                "/api/v1/skills",
                headers=headers,
                params={"sandbox_config_id": config_id},
            )
            assert visible.status_code == 200, visible.text
            assert [item["name"] for item in visible.json()["data"]] == ["catalog-demo"]

            blocked = await client.delete(f"/api/v1/skills/catalog/{catalog_id}", headers=headers)
            assert blocked.status_code == 409, blocked.text

            removed = await client.delete(
                f"/api/v1/sandbox-configs/{config_id}/skills/{skill_id}",
                headers=headers,
            )
            assert removed.status_code == 200, removed.text
            deleted = await client.delete(f"/api/v1/skills/catalog/{catalog_id}", headers=headers)
            assert deleted.status_code == 200, deleted.text

            source_archive = io.BytesIO()
            with zipfile.ZipFile(source_archive, "w") as bundle:
                bundle.writestr("SKILL.md", "---\nname: source-skill\ndescription: From source\n---\n")
            monkeypatch.setattr(
                infra_router,
                "_download_skill_source",
                lambda source: (source_archive.getvalue(), "source-skill.zip", ""),
            )
            from_source = await client.post(
                "/api/v1/skills/catalog",
                headers=headers,
                json={"source": "@catalog/source-skill"},
            )
            assert from_source.status_code == 201, from_source.text
            assert from_source.json()["data"]["name"] == "source-skill"
            source_id = from_source.json()["data"]["id"]
            removed_source = await client.delete(f"/api/v1/skills/catalog/{source_id}", headers=headers)
            assert removed_source.status_code == 200, removed_source.text

    _run(case)


def test_skill_source_url_resolution() -> None:
    clawhub = urllib.parse.urlparse(infra_router._resolve_skill_source_url("@acme/demo@1.2.0"))
    assert clawhub.netloc == "clawhub.ai"
    assert clawhub.path == "/api/v1/download"
    assert urllib.parse.parse_qs(clawhub.query) == {
        "slug": ["demo"],
        "ownerHandle": ["acme"],
        "version": ["1.2.0"],
    }

    github = infra_router._resolve_skill_source_url(
        "https://github.com/acme/skills/tree/main/tools/demo"
    )
    assert github == "https://codeload.github.com/acme/skills/zip/main"

    gitlab = infra_router._resolve_skill_source_url(
        "https://gitlab.com/acme/platform/skills/-/tree/main/demo"
    )
    assert gitlab == "https://gitlab.com/acme/platform/skills/-/archive/main/skills-main.zip"

    skills_sh = urllib.parse.urlparse(
        infra_router._resolve_skill_source_url("https://skills.sh/acme/repo/demo")
    )
    assert skills_sh.path == "/api/v1/skills/demo/install"
    assert urllib.parse.parse_qs(skills_sh.query)["reference"] == ["skills-sh:acme/repo/demo"]

    skillhub = urllib.parse.urlparse(
        infra_router._resolve_skill_source_url("https://skillhub.cn/skills/publisher/demo")
    )
    assert skillhub.netloc == "api.skillhub.cn"
    assert urllib.parse.parse_qs(skillhub.query)["slug"] == ["demo"]

    with pytest.raises(Exception):
        infra_router._resolve_skill_source_url("acme/demo")


def test_protected_file_proxies_resolve_resource_and_local_paths() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Protected files"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("diagram.txt", b"protected image bytes", "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            file_path = uploaded.json()["data"]["file_path"]
            query = urllib.parse.urlencode({"file_path": file_path})

            tenant_file = await client.get("/files?" + query, headers=headers)
            assert tenant_file.status_code == 200, tenant_file.text
            assert tenant_file.content == b"protected image bytes"
            relative_file = Path(file_path).resolve().relative_to(app_config.FILES_DIR.resolve())
            static_file = await client.get("/files/" + urllib.parse.quote(relative_file.as_posix()))
            assert static_file.status_code == 404, static_file.text

            kb_file = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/files?{query}",
                headers=headers,
            )
            assert kb_file.status_code == 200, kb_file.text
            assert kb_file.content == b"protected image bytes"
            kb_unreferenced = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/files?{urllib.parse.urlencode({'file_path': file_path + '.unreferenced'})}",
                headers=headers,
            )
            assert kb_unreferenced.status_code == 403, kb_unreferenced.text

            session = await client.post("/api/v1/sessions", headers=headers, json={})
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]
            message_id = str(uuid.uuid4())
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO messages(id, request_id, session_id, role, content, is_completed) VALUES (?, ?, ?, 'assistant', ?, 1)",
                    (message_id, message_id, session_id, f"attached {file_path}"),
                )
            message_file = await client.get(
                f"/api/v1/sessions/{session_id}/messages/{message_id}/files?{query}",
                headers=headers,
            )
            assert message_file.status_code == 200, message_file.text
            assert message_file.content == b"protected image bytes"
            unreferenced = await client.get(
                f"/api/v1/sessions/{session_id}/messages/{message_id}/files?{urllib.parse.urlencode({'file_path': file_path + '.unreferenced'})}",
                headers=headers,
            )
            assert unreferenced.status_code == 403, unreferenced.text

            handle = uuid.uuid4().hex[:22]
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO resources(id, handle, tenant_id, provider, physical_path, location_hash, state) "
                    "VALUES (?, ?, ?, 'local', ?, ?, 'active')",
                    (str(uuid.uuid4()), handle, tenant_id, file_path, handle),
                )
            resource = await client.get(
                "/files?" + urllib.parse.urlencode({"file_path": "resource://" + handle}),
                headers=headers,
            )
            assert resource.status_code == 200, resource.text
            assert resource.content == b"protected image bytes"

            channel = await client.post(
                "/api/v1/agents/builtin-quick-answer/embed-channels",
                headers=headers,
                json={"name": "Protected files embed"},
            )
            assert channel.status_code == 201, channel.text
            channel_data = channel.json()["data"]
            embed_file = await client.get(
                f"/api/v1/embed/{channel_data['id']}/files?"
                + urllib.parse.urlencode({"file_path": "resource://" + handle}),
                headers={"Authorization": "Embed " + channel_data["publish_token"]},
            )
            assert embed_file.status_code == 200, embed_file.text
            assert embed_file.content == b"protected image bytes"

    _run(case)


def test_presigned_file_get_and_head(monkeypatch) -> None:
    from app.services import presign

    monkeypatch.setenv("SYSTEM_SIGNING_KEY", "s" * 32)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"Presigned files {uuid.uuid4().hex[:8]}"},
            )
            assert kb.status_code == 201, kb.text
            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb.json()['data']['id']}/knowledge/file",
                headers=headers,
                files={"file": ("presigned.txt", b"presigned bytes", "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            file_path = uploaded.json()["data"]["file_path"]
            url = presign.sign_file_url("http://testserver", file_path, tenant_id, 60)
            parsed = urllib.parse.urlsplit(url)
            target = parsed.path + "?" + parsed.query

            fetched = await client.get(target)
            assert fetched.status_code == 200, fetched.text
            assert fetched.content == b"presigned bytes"

            head = await client.head(target)
            assert head.status_code == 200, head.text
            assert head.content == b""
            assert head.headers["content-length"] == str(len(b"presigned bytes"))

            query = urllib.parse.parse_qs(parsed.query)
            query["sig"] = ["0" * len(query["sig"][0])]
            tampered = parsed.path + "?" + urllib.parse.urlencode(query, doseq=True)
            denied = await client.get(tampered)
            assert denied.status_code == 403, denied.text

    _run(case)


def test_protected_file_proxy_streams_tenant_object_storage(monkeypatch) -> None:
    from app.services import object_storage

    calls: list[tuple[str, dict[str, str]]] = []

    class FakeObject:
        status = 200
        headers = {"content-length": "11"}

        def iter_bytes(self):
            yield b"cloud bytes"

        def close(self):
            return None

    def fake_open_url(method: str, url: str, **kwargs):
        calls.append((url, dict(kwargs.get("headers") or {})))
        return FakeObject()

    monkeypatch.setattr(object_storage.url_guard, "open_url", fake_open_url)

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            backend = await client.post(
                "/api/v1/storage-backends",
                headers=headers,
                json={
                    "name": f"object-{suffix}",
                    "provider": "s3",
                    "config": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key_id": "ACCESS",
                        "secret_access_key": "SECRET",
                    },
                },
            )
            assert backend.status_code == 201, backend.text
            backend_id = backend.json()["data"]["id"]
            reference = (
                f"storage://{backend_id}/s3://docs/exports/{tenant_id}/diagram.png"
            )
            response = await client.get(
                "/files?" + urllib.parse.urlencode({"file_path": reference}),
                headers=headers,
            )
            assert response.status_code == 200, response.text
            assert response.content == b"cloud bytes"
            assert calls[-1][0] == "https://s3.example.test/docs/exports/%d/diagram.png" % tenant_id
            assert calls[-1][1]["Authorization"].startswith("AWS4-HMAC-SHA256")

            credentials = {
                "username": f"object-attacker-{suffix}",
                "email": f"object-attacker-{suffix}@example.com",
                "password": "ObjectAttackerPass123",
            }
            registered = await client.post("/api/v1/auth/register", json=credentials)
            assert registered.status_code == 201, registered.text
            attacker = await _login(
                client,
                {"email": credentials["email"], "password": credentials["password"]},
            )
            attacker_tenant = int(attacker["active_tenant"]["id"])
            attacker_reference = (
                f"storage://{backend_id}/s3://docs/exports/{attacker_tenant}/diagram.png"
            )
            denied = await client.get(
                "/files?" + urllib.parse.urlencode({"file_path": attacker_reference}),
                headers=_headers(attacker),
            )
            assert denied.status_code == 404, denied.text

    _run(case)


def test_knowledge_upload_and_delete_use_bound_object_storage(monkeypatch) -> None:
    from app.services import object_storage, presign

    calls: list[tuple[str, str, bytes | None]] = []
    closed: list[str] = []
    monkeypatch.setenv("SYSTEM_SIGNING_KEY", "s" * 32)

    class Response:
        def __init__(self, status: int, body: bytes = b"") -> None:
            self.status = status
            self.body = body
            self.headers = {"content-length": str(len(body))} if body else {}

        def iter_bytes(self):
            yield self.body

        def close(self) -> None:
            closed.append("response")

    def fake_open_url(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs.get("body")))
        if method == "GET":
            return Response(200, b"cloud source text")
        return Response(200)

    monkeypatch.setattr(object_storage.url_guard, "open_url", fake_open_url)

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            backend = await client.post(
                "/api/v1/storage-backends",
                headers=headers,
                json={
                    "name": f"upload-object-{suffix}",
                    "provider": "s3",
                    "config": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key_id": "ACCESS",
                        "secret_access_key": "SECRET",
                    },
                },
            )
            assert backend.status_code == 201, backend.text
            backend_id = backend.json()["data"]["id"]
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"Cloud upload {suffix}", "storage_backend_id": backend_id},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("cloud.txt", b"cloud source text", "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            knowledge = uploaded.json()["data"]
            reference = knowledge["file_path"]
            assert reference.startswith(
                f"storage://{backend_id}/s3://docs/{tenant_id}/{knowledge['id']}/"
            )
            assert calls[0][0] == "PUT"
            assert calls[0][2] == b"cloud source text"
            assert calls[0][1].startswith(
                f"https://s3.example.test/docs/{tenant_id}/{knowledge['id']}/"
            )

            signed = presign.sign_file_url("http://testserver", reference, tenant_id, 60)
            parsed = urllib.parse.urlsplit(signed)
            before_head = len(closed)
            head = await client.head(parsed.path + "?" + parsed.query)
            assert head.status_code == 200, head.text
            assert head.content == b""
            assert len(closed) == before_head + 1

            preview = await client.get(
                f"/api/v1/knowledge/{knowledge['id']}/preview", headers=headers
            )
            assert preview.status_code == 200, preview.text
            assert preview.content == b"cloud source text"
            assert calls[-1][0] == "GET"

            started = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": kb_id},
            )
            assert started.status_code == 200, started.text
            task_id = started.json()["data"]["task_id"]
            duplicate_id = started.json()["data"]["target_id"]
            progress = {}
            for _ in range(500):
                response = await client.get(
                    f"/api/v1/knowledge-bases/copy/progress/{task_id}", headers=headers
                )
                assert response.status_code == 200, response.text
                progress = response.json()["data"]
                if progress["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert progress["status"] == "completed", progress
            copied = await client.get(
                f"/api/v1/knowledge-bases/{duplicate_id}/knowledge", headers=headers
            )
            assert copied.status_code == 200, copied.text
            copied_knowledge = copied.json()["data"][0]
            copied_reference = copied_knowledge["file_path"]
            assert copied_reference != reference
            assert copied_reference.startswith(f"storage://{backend_id}/s3://docs/")
            assert copied_reference.endswith(".txt")

            before_delete = len(calls)
            deleted = await client.delete(
                f"/api/v1/knowledge/{knowledge['id']}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
            assert calls[before_delete][0] == "DELETE"
            assert calls[before_delete][1] == calls[0][1]

            copied_preview = await client.get(
                f"/api/v1/knowledge/{copied_knowledge['id']}/preview", headers=headers
            )
            assert copied_preview.status_code == 200, copied_preview.text
            assert copied_preview.content == b"cloud source text"

            deleted = await client.delete(
                f"/api/v1/knowledge/{copied_knowledge['id']}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
            assert calls[-1][0] == "DELETE"
            assert calls[-1][1].startswith(
                f"https://s3.example.test/docs/{tenant_id}/{copied_knowledge['id']}/"
            )

    _run(case)


def test_background_kb_clone_creates_independent_object(monkeypatch) -> None:
    from app.services import object_storage

    calls: list[str] = []

    class Response:
        def __init__(self, status: int, body: bytes = b"") -> None:
            self.status = status
            self.body = body
            self.headers = {"content-length": str(len(body))} if body else {}

        def iter_bytes(self):
            yield self.body

        def close(self) -> None:
            return None

    def fake_open_url(method: str, url: str, **kwargs):
        calls.append(method)
        return Response(200, b"clone source bytes" if method == "GET" else b"")

    monkeypatch.setattr(object_storage.url_guard, "open_url", fake_open_url)

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            backend = await client.post(
                "/api/v1/storage-backends",
                headers=headers,
                json={
                    "name": f"clone-object-{suffix}",
                    "provider": "s3",
                    "config": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key_id": "ACCESS",
                        "secret_access_key": "SECRET",
                    },
                },
            )
            assert backend.status_code == 201, backend.text
            backend_id = backend.json()["data"]["id"]
            source = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"Clone source {suffix}", "storage_backend_id": backend_id},
            )
            target = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"Clone target {suffix}", "storage_backend_id": backend_id},
            )
            assert source.status_code == 201, source.text
            assert target.status_code == 201, target.text
            source_id = source.json()["data"]["id"]
            target_id = target.json()["data"]["id"]
            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/file",
                headers=headers,
                files={"file": ("clone.txt", b"clone source bytes", "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            original = uploaded.json()["data"]

            started = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": target_id},
            )
            assert started.status_code == 200, started.text
            task_id = started.json()["data"]["task_id"]
            progress = {}
            for _ in range(500):
                response = await client.get(
                    f"/api/v1/knowledge-bases/copy/progress/{task_id}", headers=headers
                )
                assert response.status_code == 200, response.text
                progress = response.json()["data"]
                if progress["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert progress["status"] == "completed", progress

            copied = await client.get(
                f"/api/v1/knowledge-bases/{target_id}/knowledge", headers=headers
            )
            assert copied.status_code == 200, copied.text
            clone = copied.json()["data"][0]
            assert clone["file_path"] != original["file_path"]
            assert clone["file_path"].startswith(
                f"storage://{backend_id}/s3://docs/{tenant_id}/{clone['id']}/"
            )
            preview = await client.get(
                f"/api/v1/knowledge/{clone['id']}/preview", headers=headers
            )
            assert preview.status_code == 200, preview.text
            assert preview.content == b"clone source bytes"
            assert calls.count("PUT") == 2
            assert calls.count("GET") == 1

    _run(case)


def test_kb_clone_copies_chunk_images_and_rewrites_urls(monkeypatch) -> None:
    from app.services import object_storage

    calls: list[tuple[str, str, bytes | None]] = []
    image_bytes = b"\x89PNG\r\n\x1a\nclone-image"

    class Response:
        def __init__(self, status: int, body: bytes = b"") -> None:
            self.status = status
            self.body = body
            self.headers = {"content-length": str(len(body))} if body else {}

        def iter_bytes(self):
            yield self.body

        def close(self) -> None:
            return None

    def fake_open_url(method: str, url: str, **kwargs):
        calls.append((method, url, kwargs.get("body")))
        return Response(200, image_bytes if method == "GET" else b"")

    monkeypatch.setattr(object_storage.url_guard, "open_url", fake_open_url)

    def assert_clone(target_id: str, tenant_id: int, old_url: str) -> None:
        knowledge = db.fetch_one(
            "SELECT * FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
            (target_id,),
        )
        assert knowledge is not None
        chunks = db.fetch_all(
            "SELECT * FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
            (knowledge["id"],),
        )
        parent = next(chunk for chunk in chunks if "See ![diagram]" in chunk["content"])
        image_chunk = next(chunk for chunk in chunks if chunk["chunk_type"] == "image")
        assert parent["next_chunk_id"] == image_chunk["id"]
        assert image_chunk["pre_chunk_id"] == parent["id"]
        assert image_chunk["parent_chunk_id"] == parent["id"]
        images = json.loads(image_chunk["image_info"])
        new_url = images[0]["url"]
        assert new_url != old_url
        assert new_url.startswith(f"storage://")
        assert f"/{tenant_id}/{knowledge['id']}/" in new_url
        assert new_url in parent["content"]
        assert old_url not in parent["content"]
        assert new_url in parent["source_content"]
        assert old_url not in parent["source_content"]
        assert images[0]["original_url"] == new_url
        assert images[0]["caption"] == "diagram"
        assert images[0]["attrs"] == {"attrs": {"kind": "chart"}}
        projected = db.fetch_one(
            "SELECT url FROM chunk_images WHERE chunk_id=? AND image_index=0",
            (image_chunk["id"],),
        )
        assert projected == {"url": new_url}

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            backend = await client.post(
                "/api/v1/storage-backends",
                headers=headers,
                json={
                    "name": f"clone-images-{suffix}",
                    "provider": "s3",
                    "config": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key_id": "ACCESS",
                        "secret_access_key": "SECRET",
                    },
                },
            )
            assert backend.status_code == 201, backend.text
            backend_id = backend.json()["data"]["id"]
            source = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"Image clone source {suffix}", "storage_backend_id": backend_id},
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "Image source", "content": "image source"},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]
            old_url = f"storage://{backend_id}/s3://docs/{tenant_id}/exports/source.png"
            with db.transaction() as conn:
                parent = dict(
                    conn.execute(
                        "SELECT * FROM chunks WHERE knowledge_id=? ORDER BY chunk_index LIMIT 1",
                        (knowledge_id,),
                    ).fetchone()
                )
                child_id = db.new_id()
                conn.execute(
                    "UPDATE chunks SET content=?, source_content=?, image_info='', next_chunk_id=? WHERE id=?",
                    (
                        f"See ![diagram]({old_url}) here.",
                        f"See ![diagram]({old_url}) here.",
                        child_id,
                        parent["id"],
                    ),
                )
                child = dict(parent)
                child.update(
                    {
                        "id": child_id,
                        "chunk_index": int(parent["chunk_index"]) + 1,
                        "chunk_type": "image",
                        "content": "",
                        "source_content": "",
                        "pre_chunk_id": parent["id"],
                        "parent_chunk_id": parent["id"],
                        "image_info": json.dumps(
                            [
                                {
                                    "url": old_url,
                                    "original_url": old_url,
                                    "caption": "diagram",
                                    "attrs": {"attrs": {"kind": "chart"}},
                                }
                            ]
                        ),
                    }
                )
                db.insert("chunks", child, conn)

            duplicate = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/duplicate", headers=headers
            )
            assert duplicate.status_code == 201, duplicate.text
            duplicate_data = duplicate.json()["data"]
            assert duplicate_data["source_id"] == source_id
            assert duplicate_data["target_id"] == duplicate_data["knowledge_base"]["id"]
            assert duplicate_data["knowledge_base"]["knowledge_count"] == 0
            assert duplicate_data["knowledge_base"]["chunk_count"] == 0
            duplicate_items = await client.get(
                f"/api/v1/knowledge-bases/{duplicate_data['target_id']}/knowledge",
                headers=headers,
            )
            assert duplicate_items.status_code == 200, duplicate_items.text
            assert duplicate_items.json()["data"] == []

            target = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"Image clone target {suffix}", "storage_backend_id": backend_id},
            )
            assert target.status_code == 201, target.text
            target_id = target.json()["data"]["id"]
            started = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": target_id},
            )
            assert started.status_code == 200, started.text
            task_id = started.json()["data"]["task_id"]
            progress = {}
            for _ in range(500):
                response = await client.get(
                    f"/api/v1/knowledge-bases/copy/progress/{task_id}", headers=headers
                )
                assert response.status_code == 200, response.text
                progress = response.json()["data"]
                if progress["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert progress["status"] == "completed", progress
            assert_clone(target_id, tenant_id, old_url)

            gallery = await client.get(
                f"/api/v1/knowledge-bases/{target_id}/images", headers=headers
            )
            assert gallery.status_code == 200, gallery.text
            assert any(
                item["url"].startswith(f"storage://{backend_id}/s3://docs/{tenant_id}/")
                for item in gallery.json()["data"]
            )
            assert all(
                "/exports/" not in item["url"] for item in gallery.json()["data"]
            )

            gets = [call for call in calls if call[0] == "GET"]
            puts = [call for call in calls if call[0] == "PUT"]
            assert gets == []
            assert len(puts) == 1
            assert puts[0][2] is None

    _run(case)


def test_kb_duplicate_copies_settings_only() -> None:
    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": f"Duplicate settings {suffix}",
                    "description": "settings survive, content does not",
                    "chunking_config": {"chunk_size": 256, "chunk_overlap": 24},
                    "image_processing_config": {"enable_multimodal": True},
                    "indexing_strategy": {
                        "vector_enabled": True,
                        "keyword_enabled": True,
                        "wiki_enabled": False,
                        "graph_enabled": False,
                    },
                },
            )
            assert created.status_code == 201, created.text
            source = created.json()["data"]
            source_id = source["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "source-only document", "content": "source content"},
            )
            assert manual.status_code == 201, manual.text
            generated = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/profile/generate", headers=headers
            )
            assert generated.status_code == 200, generated.text
            pinned = await client.put(
                f"/api/v1/knowledge-bases/{source_id}/pin", headers=headers
            )
            assert pinned.status_code == 200, pinned.text
            assert pinned.json()["data"]["is_pinned"] is True
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET is_temporary=1 WHERE id=?", (source_id,)
                )

            duplicate = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/duplicate", headers=headers
            )
            assert duplicate.status_code == 201, duplicate.text
            data = duplicate.json()["data"]
            target = data["knowledge_base"]
            target_id = data["target_id"]
            assert data["source_id"] == source_id
            assert target["id"] == target_id
            assert target["name"] == f"{source['name']} 副本"
            assert target["description"] == source["description"]
            assert target["chunking_config"] == source["chunking_config"]
            assert target["image_processing_config"] == source["image_processing_config"]
            assert target["indexing_strategy"] == source["indexing_strategy"]
            assert target["is_temporary"] is False
            assert target["is_pinned"] is False
            assert target["generated_profile"] == {}
            assert target["knowledge_count"] == 0
            assert target["chunk_count"] == 0

            target_items = await client.get(
                f"/api/v1/knowledge-bases/{target_id}/knowledge", headers=headers
            )
            assert target_items.status_code == 200, target_items.text
            assert target_items.json()["data"] == []
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (target_id,),
            ) == {"n": 0}

            source_items = await client.get(
                f"/api/v1/knowledge-bases/{source_id}/knowledge", headers=headers
            )
            assert source_items.status_code == 200, source_items.text
            assert [item["title"] for item in source_items.json()["data"]] == [
                "source-only document"
            ]

    _run(case)


def test_url_knowledge_import_blocks_loopback() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "URL safety"},
            )
            assert kb.status_code == 201, kb.text
            imported = await client.post(
                f"/api/v1/knowledge-bases/{kb.json()['data']['id']}/knowledge/url",
                headers=headers,
                json={"url": "http://127.0.0.1:8080/internal"},
            )
            assert imported.status_code == 201, imported.text
            data = imported.json()["data"]
            assert data["parse_status"] == "failed"
            assert "loopback address" in data["error_message"]

    _run(case)


def test_system_capabilities_expose_frontend_keys(monkeypatch) -> None:
    monkeypatch.delenv("WEKNORA_SANDBOX_DOCKER_ENABLED", raising=False)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            response = await client.get("/api/v1/system/capabilities", headers=_headers(login))
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["edition"] == "python"
            capabilities = data["capabilities"]
            assert set(capabilities) == {
                "organizations", "agents", "integrations.im", "integrations.embed",
                "integrations.api", "integrations.mcpserver", "settings.mcp",
                "settings.websearch", "settings.vectorstore", "settings.storage",
                "settings.sandbox", "settings.sandbox.docker", "settings.sandbox.host",
                "settings.sandbox.remote",
            }
            assert capabilities["settings.sandbox"]["supported"] is True
            assert capabilities["settings.sandbox.remote"]["supported"] is True
            assert capabilities["settings.sandbox.host"] == {
                "supported": False, "reason": "platform_unsupported"
            }
            assert capabilities["settings.sandbox.docker"] == {
                "supported": False, "reason": "docker_backend_disabled"
            }

    _run(case)


def test_parser_engine_discovery_and_sandbox_connectivity(monkeypatch) -> None:
    monkeypatch.setattr(system_router, "_probe_docreader", lambda addr: (True, ""))
    monkeypatch.setattr(
        system_router,
        "_fetch_remote_parser_engines",
        lambda addr: [{"Name": "mineru", "Description": "Remote MinerU", "FileTypes": ["pdf"], "Available": True}],
    )
    monkeypatch.setattr(
        infra_router,
        "_sandbox_http_probe",
        lambda url, headers=None: (True, "", 12),
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            saved = await client.put(
                "/api/v1/tenants/kv/parser-engine-config",
                headers=headers,
                json={"docreader_addr": "http://docreader.internal:50051"},
            )
            assert saved.status_code == 200, saved.text

            engines = await client.get("/api/v1/system/parser-engines", headers=headers)
            assert engines.status_code == 200, engines.text
            payload = engines.json()
            assert payload["connected"] is True
            assert payload["docreader_addr"] == "http://docreader.internal:50051"
            assert any(item["Name"] == "builtin" for item in payload["data"])
            assert any(item["Name"] == "mineru" for item in payload["data"])

            checked = await client.post(
                "/api/v1/system/parser-engines/check",
                headers=headers,
                json={"docreader_addr": "https://docreader.override"},
            )
            assert checked.status_code == 200, checked.text
            assert checked.json()["docreader_addr"] == "https://docreader.override"
            assert checked.json()["docreader_transport"] == "https"

            sandbox = await client.post(
                "/api/v1/system/sandbox-check",
                headers=headers,
                json={
                    "config": {
                        "sandbox_type": "cube",
                        "cube": {"api_url": "https://cube.test", "api_key": "***", "template_id": "tpl-1"},
                    },
                    "deep": False,
                },
            )
            assert sandbox.status_code == 200, sandbox.text
            result = sandbox.json()["data"]
            assert result["ok"] is True
            assert result["provider"] == "cube"
            checks = {item["name"]: item for item in result["checks"]}
            assert checks["api_url_reachable"]["ok"] is True
            assert checks["credential_valid"]["ok"] is True
            assert checks["template_exists"]["reason"] == "needs_deep_check"

    _run(case)


def test_web_search_provider_and_agent_tool(monkeypatch) -> None:
    monkeypatch.setenv("SSRF_WHITELIST", "searx.test")

    def fake_http(method, url, *, headers=None, body=None, timeout=20):
        if url.startswith("https://searx.test/search"):
            return 200, json.dumps({
                "results": [
                    {"title": "Release notes", "url": "https://example.test/release", "content": "v1 is healthy"}
                ]
            }).encode()
        if url.startswith("https://html.duckduckgo.com/html/"):
            return 200, b'<a class="result__a" href="https://example.test/ddg">DDG result</a><a class="result__snippet">Snippet</a>'
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(web_search_service, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            provider = await client.post(
                "/api/v1/web-search-providers",
                headers=headers,
                json={
                    "name": "Test SearXNG",
                    "provider": "searxng",
                    "parameters": {"base_url": "https://searx.test"},
                    "is_default": True,
                },
            )
            assert provider.status_code == 201, provider.text
            provider_id = provider.json()["data"]["id"]

            saved_test = await client.post(f"/api/v1/web-search-providers/{provider_id}/test", headers=headers)
            assert saved_test.status_code == 200, saved_test.text
            assert saved_test.json()["success"] is True

            raw_test = await client.post(
                "/api/v1/web-search-providers/test",
                headers=headers,
                json={"provider": "searxng", "parameters": {"base_url": "https://searx.test"}},
            )
            assert raw_test.status_code == 200, raw_test.text
            assert raw_test.json()["success"] is True

            result = agent_tools.execute_tool(
                agent_tools.builtin_web_search_tool(),
                {"query": "release", "limit": 3},
                tenant_id=login["active_tenant"]["id"],
                user_id=login["user"]["id"],
            )
            assert result["success"] is True
            assert json.loads(result["output"])[0]["url"] == "https://example.test/release"

    _run(case)
    ddg = web_search_service.search("duckduckgo", {}, "release", 1)
    assert ddg[0]["title"] == "DDG result"


def test_web_search_provider_type_metadata_matches_ui_contract() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            response = await client.get("/api/v1/web-search-providers/types", headers=_headers(login))
            assert response.status_code == 200, response.text
            items = response.json()["data"]
            assert [item["id"] for item in items] == [
                "brave",
                "serply",
                "duckduckgo",
                "bing",
                "google",
                "tavily",
                "ollama",
                "searxng",
                "baidu",
                "keenable",
                "metaso",
                "zhipu",
                "exa",
                "bocha",
            ]
            assert all("provider" not in item and "available" not in item for item in items)
            by_id = {item["id"]: item for item in items}
            assert by_id["google"]["requires_engine_id"] is True
            assert by_id["searxng"]["requires_base_url"] is True
            assert by_id["keenable"]["requires_api_key"] is False
            assert by_id["keenable"]["supports_optional_api_key"] is True
            assert [field["key"] for field in by_id["metaso"]["config_fields"]] == ["scope"]
            assert [field["key"] for field in by_id["zhipu"]["config_fields"]] == [
                "search_engine",
                "content_size",
            ]
            assert [field["key"] for field in by_id["exa"]["config_fields"]] == ["include_text"]
            assert [field["key"] for field in by_id["bocha"]["config_fields"]] == [
                "freshness",
                "summary",
            ]

    _run(case)


def test_web_search_provider_save_validation() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            invalid = [
                ({"name": "Brave", "provider": "brave", "parameters": {}}, "API key is required"),
                ({"name": "Google", "provider": "google", "parameters": {"api_key": "key"}}, "engine ID is required"),
                (
                    {"name": "Metaso", "provider": "metaso", "parameters": {"api_key": "key", "extra_config": {"scope": "news"}}},
                    "invalid Metaso search scope",
                ),
                (
                    {"name": "SearXNG", "provider": "searxng", "parameters": {"base_url": "https://searx.test/?q=bad"}},
                    "must not contain query or fragment",
                ),
                (
                    {"name": "Proxy", "provider": "duckduckgo", "parameters": {"proxy_url": "http://127.0.0.1:3128"}},
                    "invalid proxy_url",
                ),
                ({"name": "Unknown", "provider": "unknown", "parameters": {}}, "Unsupported web search provider"),
            ]
            for payload, message in invalid:
                response = await client.post("/api/v1/web-search-providers", headers=headers, json=payload)
                assert response.status_code == 400, response.text
                assert message in response.json()["error"]["message"]

            valid = await client.post(
                "/api/v1/web-search-providers",
                headers=headers,
                json={"name": "DuckDuckGo", "provider": "duckduckgo", "parameters": {}},
            )
            assert valid.status_code == 201, valid.text

    _run(case)


def test_ssrf_whitelist_system_setting_controls_web_search(monkeypatch) -> None:
    monkeypatch.delenv("SSRF_WHITELIST", raising=False)
    monkeypatch.delenv("SSRF_WHITELIST_EXTRA", raising=False)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            try:
                updated = await client.put(
                    "/api/v1/system/admin/settings/ssrf.whitelist",
                    headers=headers,
                    json={"value": ["private.search.test", "10.0.0.0/8"]},
                )
                assert updated.status_code == 200, updated.text
                assert updated.json()["value"] == ["private.search.test", "10.0.0.0/8"]
                assert web_search_service.url_guard.is_whitelisted("private.search.test")
                assert web_search_service.url_guard.is_whitelisted("10.4.3.2")

                provider = await client.post(
                    "/api/v1/web-search-providers",
                    headers=headers,
                    json={
                        "name": "Private SearXNG",
                        "provider": "searxng",
                        "parameters": {"base_url": "http://private.search.test:8080"},
                    },
                )
                assert provider.status_code == 201, provider.text

                invalid = await client.put(
                    "/api/v1/system/admin/settings/ssrf.whitelist",
                    headers=headers,
                    json={"value": ["foo.*.bar"]},
                )
                assert invalid.status_code == 400, invalid.text
                assert "unsupported wildcard pattern" in invalid.json()["error"]["message"]
            finally:
                await client.delete("/api/v1/system/admin/settings/ssrf.whitelist", headers=headers)
            assert not web_search_service.url_guard.is_whitelisted("private.search.test")

    _run(case)


def test_additional_web_search_provider_adapters(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, str], dict[str, object] | None]] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=20):
        calls.append((method, url, dict(headers or {}), body))
        if url.startswith("https://api.serply.io/v1/search?"):
            return 200, json.dumps({
                "results": [{"title": "Serply result", "link": "https://serply.test", "description": "serply snippet"}]
            }).encode()
        if url == "https://qianfan.baidubce.com/v2/ai_search/web_search":
            return 200, json.dumps({
                "references": [{"title": "Baidu result", "url": "https://baidu.test", "content": "baidu content", "date": "2026-10-01"}]
            }).encode()
        if url in {"https://api.keenable.ai/v1/search", "https://api.keenable.ai/v1/search/public"}:
            return 200, json.dumps({
                "results": [
                    {"title": "Keenable one", "url": "https://keenable.test/1", "description": "description", "snippet": "snippet", "published_at": "2026-10-02"},
                    {"title": "Keenable two", "url": "https://keenable.test/2", "snippet": "fallback snippet"},
                ]
            }).encode()
        if url == "https://metaso.cn/api/v1/search":
            return 200, json.dumps({
                "webpages": [{"title": "Wrong scope", "link": "https://metaso.test/wrong"}],
                "documents": [{"title": "Metaso result", "link": "https://metaso.test", "summary": "metaso summary", "snippet": "metaso snippet", "date": "2026-10-03"}],
            }).encode()
        if url == "https://open.bigmodel.cn/api/paas/v4/web_search":
            return 200, json.dumps({
                "search_result": [{"title": "Zhipu result", "link": "https://zhipu.test", "content": "zhipu content", "publish_date": "2026-10-04"}]
            }).encode()
        if url == "https://api.exa.ai/search":
            return 200, json.dumps({
                "results": [{"title": "Exa result", "url": "https://exa.test", "publishedDate": "2026-10-05", "highlights": ["first highlight", "second highlight"], "text": "full exa text"}]
            }).encode()
        if url == "https://api.bochaai.com/v1/web-search":
            return 200, json.dumps({
                "code": "200",
                "data": {"webPages": {"value": [{"name": "Bocha result", "url": "https://bocha.test", "summary": "bocha summary", "snippet": "bocha snippet", "dateLastCrawled": "2026-10-06"}]}},
            }).encode()
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(web_search_service, "_http", fake_http)

    serply = web_search_service.search("serply", {"api_key": "serply-key"}, "release", 3)
    assert serply == [{"title": "Serply result", "url": "https://serply.test", "content": "serply snippet", "source": "serply"}]
    assert calls[-1][0] == "GET"
    assert urllib.parse.parse_qs(urllib.parse.urlparse(calls[-1][1]).query) == {"q": ["release"], "num": ["3"]}
    assert calls[-1][2]["X-Api-Key"] == "serply-key"
    assert calls[-1][2]["User-Agent"] == "WeKnora/1.0"

    baidu_query = "a" * 70 + "中文"
    baidu = web_search_service.search("baidu", {"api_key": "baidu-key"}, baidu_query, 2)
    assert baidu == [{"title": "Baidu result", "url": "https://baidu.test", "content": "baidu content", "published_date": "2026-10-01", "source": "baidu"}]
    assert calls[-1][2]["Authorization"] == "Bearer baidu-key"
    assert calls[-1][3]["messages"][0]["content"] == "a" * 70 + "中"
    assert calls[-1][3]["resource_type_filter"] == [{"type": "web", "top_k": 2}]

    keenable = web_search_service.search("keenable", {}, "release", 2)
    assert [item["content"] for item in keenable] == ["description", "fallback snippet"]
    assert calls[-1][1].endswith("/public")
    assert "X-API-Key" not in calls[-1][2]
    web_search_service.search("keenable", {"api_key": "keyless-optional"}, "release", 2)
    assert calls[-1][1].endswith("/v1/search")
    assert calls[-1][2]["X-API-Key"] == "keyless-optional"

    metaso = web_search_service.search("metaso", {"api_key": "metaso-key", "extra_config": {"scope": "document"}}, "release", 2)
    assert metaso == [{"title": "Metaso result", "url": "https://metaso.test", "content": "metaso summary", "published_date": "2026-10-03", "source": "metaso"}]
    assert calls[-1][3] == {
        "q": "release",
        "scope": "document",
        "size": 2,
        "includeSummary": True,
        "includeRawContent": False,
        "conciseSnippet": True,
    }

    zhipu = web_search_service.search(
        "zhipu",
        {"api_key": "zhipu-key", "extra_config": {"search_engine": "search_pro", "content_size": "high"}},
        "启" * 75,
        2,
    )
    assert zhipu == [{"title": "Zhipu result", "url": "https://zhipu.test", "content": "zhipu content", "published_date": "2026-10-04", "source": "zhipu"}]
    assert calls[-1][3] == {
        "search_query": "启" * 70,
        "search_engine": "search_pro",
        "search_intent": False,
        "count": 2,
        "content_size": "high",
    }

    exa = web_search_service.search("exa", {"api_key": "exa-key", "extra_config": {"include_text": "true"}}, "release", 2)
    assert exa == [{
        "title": "Exa result",
        "url": "https://exa.test",
        "content": "full exa text",
        "snippet": "first highlight\nsecond highlight",
        "published_date": "2026-10-05",
        "source": "exa",
    }]
    assert calls[-1][2]["x-api-key"] == "exa-key"
    assert calls[-1][3]["contents"] == {"highlights": True, "text": True}

    bocha = web_search_service.search(
        "bocha",
        {"api_key": "bocha-key", "extra_config": {"freshness": "oneWeek", "summary": "false"}},
        "release",
        2,
    )
    assert bocha == [{"title": "Bocha result", "url": "https://bocha.test", "content": "bocha summary", "published_date": "2026-10-06", "source": "bocha"}]
    assert calls[-1][3] == {"query": "release", "freshness": "oneWeek", "summary": False, "count": 2}


def test_web_search_proxy_url_is_forwarded(monkeypatch) -> None:
    monkeypatch.setenv("SSRF_WHITELIST", "proxy.test")
    seen_proxies: list[str] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=20, proxy_url=""):
        seen_proxies.append(proxy_url)
        return 200, json.dumps({
            "results": [{"title": "Proxied result", "link": "https://result.test", "description": "through proxy"}]
        }).encode()

    monkeypatch.setattr(web_search_service, "_http", fake_http)
    results = web_search_service.search(
        "serply",
        {"api_key": "serply-key", "proxy_url": "http://proxy.test:3128"},
        "release",
        1,
    )
    assert results[0]["url"] == "https://result.test"
    assert seen_proxies == ["http://proxy.test:3128"]


def test_web_search_http_applies_proxy_and_validates_redirects(monkeypatch) -> None:
    validated: list[str] = []
    proxy_checks: list[str] = []
    handlers: list[object] = []

    class FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

    class FakeOpener:
        def open(self, request, timeout):
            assert request.full_url == "https://api.search.test/v1"
            assert timeout == 7
            return FakeResponse()

    def fake_build_opener(*items):
        handlers.extend(items)
        return FakeOpener()

    monkeypatch.setattr(
        web_search_service.url_guard,
        "validate_url_for_ssrf",
        lambda url: validated.append(url),
    )
    monkeypatch.setattr(
        web_search_service.url_guard,
        "validate_proxy_url",
        lambda url: proxy_checks.append(url),
    )
    monkeypatch.setattr(web_search_service.urllib.request, "build_opener", fake_build_opener)

    status, body = web_search_service._http(
        "GET",
        "https://api.search.test/v1",
        timeout=7,
        proxy_url="http://proxy.test:3128",
    )
    assert (status, body) == (200, b'{"ok": true}')
    assert validated == ["https://api.search.test/v1"]
    assert proxy_checks == ["http://proxy.test:3128"]
    proxy_handlers = [handler for handler in handlers if getattr(handler, "proxies", None)]
    assert proxy_handlers[0].proxies == {
        "http": "http://proxy.test:3128",
        "https": "http://proxy.test:3128",
    }

    monkeypatch.setattr(
        web_search_service.urllib.request.HTTPRedirectHandler,
        "redirect_request",
        lambda *args: "redirected",
    )
    redirect_handler = next(handler for handler in handlers if isinstance(handler, web_search_service._SafeRedirectHandler))
    assert redirect_handler.redirect_request(None, None, 302, "", {}, "https://redirect.test") == "redirected"
    assert validated[-1] == "https://redirect.test"


def test_web_search_http_uses_pinned_request_path_without_proxy(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_request(method, url, *, headers=None, body=None, timeout=30):
        seen.update({"method": method, "url": url, "headers": headers, "body": body, "timeout": timeout})
        return 200, {}, b'{"ok": true}', url

    monkeypatch.setattr(web_search_service.url_guard, "request_url", fake_request)
    monkeypatch.setattr(web_search_service.url_guard, "validate_url_for_ssrf", lambda url: None)
    status, body = web_search_service._http(
        "POST",
        "https://api.search.test/v1",
        headers={"X-Test": "yes"},
        body={"query": "release"},
        timeout=7,
    )
    assert (status, body) == (200, b'{"ok": true}')
    assert seen == {
        "method": "POST",
        "url": "https://api.search.test/v1",
        "headers": {
            "User-Agent": "Mozilla/5.0 (compatible; PyWeKnora/1.0)",
            "Content-Type": "application/json",
            "X-Test": "yes",
        },
        "body": b'{"query": "release"}',
        "timeout": 7,
    }


def test_web_search_http_rejects_unsafe_proxy(monkeypatch) -> None:
    monkeypatch.setattr(web_search_service.url_guard, "validate_url_for_ssrf", lambda url: None)

    def reject_proxy(url):
        raise web_search_service.url_guard.URLGuardError("loopback address")

    monkeypatch.setattr(web_search_service.url_guard, "validate_proxy_url", reject_proxy)
    with pytest.raises(web_search_service.WebSearchError, match="Invalid proxy_url"):
        web_search_service._http("GET", "https://api.search.test", proxy_url="http://127.0.0.1:3128")


def test_wiki_page_is_generated_for_manual_knowledge() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Wiki smoke", "wiki_config": {"enabled": True}},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "部署手册", "content": "服务启动后检查健康接口，再验证登录流程。"},
            )
            assert manual.status_code == 201, manual.text

            pages = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages",
                headers=headers,
            )
            assert pages.status_code == 200, pages.text
            slugs = [item["slug"] for item in pages.json()["data"]["pages"]]
            assert any(slug.startswith("doc-") for slug in slugs)

            page = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages/{slugs[0]}",
                headers=headers,
            )
            assert page.status_code == 200, page.text
            assert page.json()["data"]["chunk_refs"]

            stats = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/stats",
                headers=headers,
            )
            assert stats.status_code == 200, stats.text
            assert stats.json()["data"]["pending_tasks"] == 0
            assert stats.json()["data"]["is_active"] is False

            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO task_pending_ops(tenant_id, task_type, scope, scope_id, op) VALUES (?, 'wiki:ingest', 'knowledge_base', ?, 'ingest')",
                    (login["active_tenant"]["id"], kb_id),
                )
            try:
                queued = await client.get(
                    f"/api/v1/knowledgebase/{kb_id}/wiki/stats",
                    headers=headers,
                )
                assert queued.json()["data"]["pending_tasks"] == 1
                assert queued.json()["data"]["is_active"] is True
            finally:
                with db.transaction() as conn:
                    conn.execute("DELETE FROM task_pending_ops WHERE scope_id=?", (kb_id,))

    _run(case)


def test_storage_quota_failure_leaves_a_failed_knowledge_without_chunks() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
            assert tenant
            original_quota = int(tenant["storage_quota"])
            original_used = int(tenant["storage_used"])
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Storage quota failure"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=16, storage_used=0 WHERE id=?",
                        (tenant_id,),
                    )
                response = await client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                    headers=headers,
                    json={"title": "Too large", "content": "x" * 64},
                )
                assert response.status_code == 201, response.text
                knowledge = response.json()["data"]
                assert knowledge["parse_status"] == "failed"
                assert knowledge["enable_status"] == "disabled"
                assert knowledge["error_message"] == "存储空间不足"
                assert int(knowledge["storage_size"]) == 0
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == 0
                assert db.fetch_one(
                    "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL",
                    (knowledge["id"],),
                )["n"] == 0

                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=? WHERE id=?",
                        (original_used + 1024, tenant_id),
                    )
                retried = await client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                    headers=headers,
                    json={"title": "Fits now", "content": "x" * 64},
                )
                assert retried.status_code == 201, retried.text
                assert retried.json()["data"]["parse_status"] == "completed"
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == 64
            finally:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=?, storage_used=? WHERE id=?",
                        (original_quota, original_used, tenant_id),
                    )

    _run(case)


def test_storage_usage_tracks_manual_create_update_and_delete() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
            assert tenant
            original_quota = int(tenant["storage_quota"])
            original_used = int(tenant["storage_used"])
            baseline = original_used
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Storage accounting"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            try:
                initial_content = "initial storage content" * 4
                created = await client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                    headers=headers,
                    json={"title": "Accounting", "content": initial_content},
                )
                assert created.status_code == 201, created.text
                knowledge = created.json()["data"]
                initial_size = len(initial_content.encode())
                assert int(knowledge["storage_size"]) == initial_size
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == baseline + initial_size

                shorter_content = "short content"
                updated = await client.put(
                    f"/api/v1/knowledge/manual/{knowledge['id']}",
                    headers=headers,
                    json={"content": shorter_content},
                )
                assert updated.status_code == 200, updated.text
                shorter_size = len(shorter_content.encode())
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == baseline + shorter_size
                chunks_before = db.fetch_all(
                    "SELECT id, content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
                    (knowledge["id"],),
                )
                assert chunks_before

                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=? WHERE id=?",
                        (baseline + shorter_size + 5, tenant_id),
                    )
                refused = await client.put(
                    f"/api/v1/knowledge/manual/{knowledge['id']}",
                    headers=headers,
                    json={"content": "z" * 128},
                )
                assert refused.status_code == 400, refused.text
                assert refused.json()["error"]["message"] == "Storage quota exceeded"
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == baseline + shorter_size
                assert db.fetch_all(
                    "SELECT id, content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
                    (knowledge["id"],),
                ) == chunks_before

                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=? WHERE id=?",
                        (original_quota, tenant_id),
                    )
                deleted = await client.delete(
                    f"/api/v1/knowledge/{knowledge['id']}",
                    headers=headers,
                )
                assert deleted.status_code == 200, deleted.text
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == baseline
            finally:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=?, storage_used=? WHERE id=?",
                        (original_quota, original_used, tenant_id),
                    )

    _run(case)


def test_resource_catalog_binds_markdown_images_and_releases_only_last_owner() -> None:
    from app.services import resource_catalog

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Resource catalog"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            handle = uuid.uuid4().hex[:22]
            reference = "resource://" + handle
            physical = app_config.FILES_DIR / str(tenant_id) / "exports" / f"{handle}.png"
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_bytes(b"markdown-only-image")
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO resources(id, handle, tenant_id, provider, physical_path, location_hash, state) "
                    "VALUES (?, ?, ?, 'local', ?, ?, 'active')",
                    (db.new_id(), handle, tenant_id, str(physical), hashlib.sha256(str(physical).encode()).hexdigest()),
                )

            content = f"See ![chart]({reference}) in the report."
            first = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "First owner", "content": content},
            )
            assert first.status_code == 201, first.text
            first_id = first.json()["data"]["id"]
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM resource_bindings WHERE owner_id=? AND relation='attachment'",
                (first_id,),
            )["n"] == 1

            served = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/files?"
                + urllib.parse.urlencode({"file_path": reference}),
                headers=headers,
            )
            assert served.status_code == 200, served.text
            assert served.content == b"markdown-only-image"

            reparsed = await client.post(
                f"/api/v1/knowledge/{first_id}/reparse", headers=headers, json={}
            )
            assert reparsed.status_code == 200, reparsed.text
            assert physical.exists()
            assert db.fetch_one(
                "SELECT state FROM resources WHERE handle=?", (handle,)
            )["state"] == "active"
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM resource_bindings WHERE owner_id=?",
                (first_id,),
            )["n"] == 1

            second = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Second owner", "content": content},
            )
            assert second.status_code == 201, second.text
            second_id = second.json()["data"]["id"]

            deleted = await client.delete(
                f"/api/v1/knowledge/{first_id}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
            assert physical.exists()
            assert db.fetch_one(
                "SELECT state FROM resources WHERE handle=?", (handle,)
            )["state"] == "active"
            assert db.fetch_one(
                "SELECT 1 FROM resource_bindings WHERE owner_id=?", (first_id,)
            ) is None
            assert db.fetch_one(
                "SELECT 1 FROM resource_bindings WHERE owner_id=?", (second_id,)
            ) is not None

            deleted = await client.delete(
                f"/api/v1/knowledge/{second_id}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
            assert not physical.exists()
            resource = db.fetch_one(
                "SELECT state, deleted_at FROM resources WHERE handle=?", (handle,)
            )
            assert resource["state"] == "deleted"
            assert resource["deleted_at"]
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM resource_bindings WHERE owner_id IN (?, ?)",
                (first_id, second_id),
            )["n"] == 0

    _run(case)


def test_resource_reference_scanner_rejects_truncated_handles() -> None:
    from app.services import resource_catalog

    handle = "a" * 22
    reference = "resource://" + handle
    assert resource_catalog.scan_references(f"before {reference} after") == [reference]
    assert resource_catalog.scan_references(reference + "x") == []
    assert resource_catalog.scan_references("resource://short") == []


def test_kb_clone_claims_markdown_only_resource_for_target_knowledge() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            source = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Resource clone source"},
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]
            target = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Resource clone target"},
            )
            assert target.status_code == 201, target.text
            target_id = target.json()["data"]["id"]
            handle = uuid.uuid4().hex[:22]
            reference = "resource://" + handle
            physical = app_config.FILES_DIR / str(tenant_id) / "exports" / f"clone-{handle}.png"
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_bytes(b"shared-clone-image")
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO resources(id, handle, tenant_id, provider, physical_path, location_hash, state) "
                    "VALUES (?, ?, ?, 'local', ?, ?, 'active')",
                    (db.new_id(), handle, tenant_id, str(physical), hashlib.sha256(str(physical).encode()).hexdigest()),
                )
            original = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "Shared image", "content": f"![shared]({reference})"},
            )
            assert original.status_code == 201, original.text
            original_id = original.json()["data"]["id"]
            started = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": target_id},
            )
            assert started.status_code == 200, started.text
            task_id = started.json()["data"]["task_id"]
            progress = {}
            for _ in range(500):
                response = await client.get(
                    f"/api/v1/knowledge-bases/copy/progress/{task_id}", headers=headers
                )
                progress = response.json()["data"]
                if progress["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert progress["status"] == "completed", progress
            cloned = db.fetch_one(
                "SELECT * FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (target_id,),
            )
            assert cloned is not None
            assert db.fetch_one(
                "SELECT 1 FROM resource_bindings WHERE owner_id=?", (cloned["id"],)
            ) is not None
            cloned_chunk = db.fetch_one(
                "SELECT image_info FROM chunks WHERE knowledge_id=? AND chunk_type='image' AND deleted_at IS NULL",
                (cloned["id"],),
            )
            cloned_reference = json.loads(cloned_chunk["image_info"])[0]["url"]
            cloned_handle = cloned_reference.removeprefix("resource://")
            cloned_physical = Path(
                db.fetch_one(
                    "SELECT physical_path FROM resources WHERE handle=?", (cloned_handle,)
                )["physical_path"]
            )
            assert cloned_reference.startswith("resource://")
            assert cloned_physical.exists()

            deleted = await client.delete(
                f"/api/v1/knowledge/{original_id}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
            assert not physical.exists()
            assert cloned_physical.exists()
            deleted = await client.delete(
                f"/api/v1/knowledge/{cloned['id']}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
            assert not cloned_physical.exists()
            assert db.fetch_one(
                "SELECT state FROM resources WHERE handle=?", (cloned_handle,)
            )["state"] == "deleted"

    _run(case)


def test_clone_storage_quota_failure_removes_copied_files_and_rows() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
            original_quota = int(tenant["storage_quota"])
            original_used = int(tenant["storage_used"])
            source = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Quota clone source"},
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]
            target = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Quota clone target"},
            )
            assert target.status_code == 201, target.text
            target_id = target.json()["data"]["id"]
            content = b"clone quota content " * 8
            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/file",
                headers=headers,
                files={"file": ("quota-source.txt", content, "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            size = int(uploaded.json()["data"]["storage_size"])
            used_after_upload = db.fetch_one(
                "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
            )["storage_used"]
            files_before = sorted(
                path for path in (app_config.FILES_DIR / str(tenant_id)).rglob("*") if path.is_file()
            )
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=? WHERE id=?",
                        (original_used + size + 1, tenant_id),
                    )
                started = await client.post(
                    "/api/v1/knowledge-bases/copy",
                    headers=headers,
                    json={"source_id": source_id, "target_id": target_id},
                )
                assert started.status_code == 200, started.text
                task_id = started.json()["data"]["task_id"]
                progress = {}
                for _ in range(500):
                    response = await client.get(
                        f"/api/v1/knowledge-bases/copy/progress/{task_id}",
                        headers=headers,
                    )
                    assert response.status_code == 200, response.text
                    progress = response.json()["data"]
                    if progress["status"] in {"completed", "failed"} or (
                        progress["status"] == "pending" and progress.get("error")
                    ):
                        break
                    await asyncio.sleep(0.01)
                assert progress["status"] == "pending", progress
                assert progress["error"] == "Storage quota exceeded"
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE py_task_results SET status='cancelled', worker_token='', lease_until='', retry_after='' WHERE id=?",
                        (task_id,),
                    )
                assert db.fetch_one(
                    "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
                    (target_id,),
                )["n"] == 0
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == used_after_upload
                assert sorted(
                    path for path in (app_config.FILES_DIR / str(tenant_id)).rglob("*") if path.is_file()
                ) == files_before
            finally:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=?, storage_used=? WHERE id=?",
                        (original_quota, original_used, tenant_id),
                    )

    _run(case)


def test_datasource_update_storage_quota_failure_keeps_existing_chunks(monkeypatch) -> None:
    from app.services import datasource as datasource_connector

    feed_text = ["short datasource body"]

    def feed() -> bytes:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Quota feed</title><link>http://example.test/</link>
<description>Quota feed</description><item><guid>article-1</guid><title>Quota article</title>
<link>http://example.test/articles/1</link><description><![CDATA[<p>{feed_text[0]}</p>]]></description></item>
</channel></rss>""".encode()

    monkeypatch.setattr(
        datasource_connector,
        "_http_get",
        lambda _url, headers=None, timeout=30: feed(),
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
            original_quota = int(tenant["storage_quota"])
            original_used = int(tenant["storage_used"])
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Datasource quota"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            feed_url = "http://feed.test/quota.xml"
            created = await client.post(
                "/api/v1/datasource",
                headers=headers,
                json={
                    "name": "Quota feed",
                    "type": "rss",
                    "knowledge_base_id": kb_id,
                    "sync_mode": "incremental",
                    "config": {
                        "credentials": {},
                        "resource_ids": [feed_url],
                        "settings": {"feed_urls": feed_url},
                    },
                },
            )
            assert created.status_code == 201, created.text
            source_id = created.json()["data"]["id"]
            try:
                first = await client.post(
                    f"/api/v1/datasource/{source_id}/sync", headers=headers, json={}
                )
                assert first.status_code == 200, first.text
                knowledge = db.fetch_one(
                    "SELECT * FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
                    (kb_id,),
                )
                assert knowledge is not None
                chunks_before = db.fetch_all(
                    "SELECT id, content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
                    (knowledge["id"],),
                )
                used_before = db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"]
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=? WHERE id=?",
                        (used_before + 5, tenant_id),
                    )
                feed_text[0] = "expanded datasource body " * 40
                second = await client.post(
                    f"/api/v1/datasource/{source_id}/sync", headers=headers, json={}
                )
                assert second.status_code == 200, second.text
                assert second.json()["data"]["items_failed"] == 1
                assert db.fetch_all(
                    "SELECT id, content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
                    (knowledge["id"],),
                ) == chunks_before
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == used_before
            finally:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=?, storage_used=? WHERE id=?",
                        (original_quota, original_used, tenant_id),
                    )

    _run(case)


def test_reparse_storage_quota_failure_preserves_chunks_and_usage() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            tenant = db.fetch_one("SELECT * FROM tenants WHERE id=?", (tenant_id,))
            original_quota = int(tenant["storage_quota"])
            original_used = int(tenant["storage_used"])
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Reparse quota"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            created = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Reparse quota", "content": "original"},
            )
            assert created.status_code == 201, created.text
            knowledge_id = created.json()["data"]["id"]
            try:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE chunks SET content=?, source_content=? WHERE knowledge_id=? AND deleted_at IS NULL",
                        ("x" * 128, "x" * 128, knowledge_id),
                    )
                chunks_before = db.fetch_all(
                    "SELECT id, content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
                    (knowledge_id,),
                )
                used_before = db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"]
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=? WHERE id=?",
                        (used_before + 5, tenant_id),
                    )
                refused = await client.post(
                    f"/api/v1/knowledge/{knowledge_id}/reparse",
                    headers=headers,
                    json={},
                )
                assert refused.status_code == 400, refused.text
                assert refused.json()["error"]["message"] == "Storage quota exceeded"
                assert db.fetch_all(
                    "SELECT id, content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
                    (knowledge_id,),
                ) == chunks_before
                assert db.fetch_one(
                    "SELECT storage_used FROM tenants WHERE id=?", (tenant_id,)
                )["storage_used"] == used_before
            finally:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE tenants SET storage_quota=?, storage_used=? WHERE id=?",
                        (original_quota, original_used, tenant_id),
                    )

    _run(case)


def test_wiki_ingest_refuses_a_rewrite_that_drops_table_rows() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Wiki row guard", "wiki_config": {"enabled": True}},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Certificate ledger", "content": "Initial source text."},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]

            pages = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages",
                headers=headers,
            )
            assert pages.status_code == 200, pages.text
            slug = next(
                item["slug"] for item in pages.json()["data"]["pages"] if item["slug"].startswith("doc-")
            )
            table_content = (
                "# Certificate ledger\n\n| Holder | Certificate |\n| --- | --- |\n"
                "| 张三 | CERT-2020 |\n| 李四 | CERT-2020 |\n"
                "| 王五 | CERT-2020 |\n| 赵六 | CERT-2020 |\n"
            )
            updated_page = await client.put(
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages/{slug}",
                headers=headers,
                json={"content": table_content},
            )
            assert updated_page.status_code == 200, updated_page.text
            before = db.fetch_one(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug=?",
                (kb_id, slug),
            )
            assert before
            revisions_before = db.fetch_one(
                "SELECT COUNT(*) AS n FROM wiki_page_revisions WHERE page_id=?",
                (before["id"],),
            )["n"]

            changed = await client.put(
                f"/api/v1/knowledge/manual/{knowledge_id}",
                headers=headers,
                json={"content": "Replacement source text without a table."},
            )
            assert changed.status_code == 200, changed.text
            kb_row = db.fetch_one("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,))
            knowledge_row = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
            assert kb_row and knowledge_row
            assert wiki_ingest.ingest_knowledge(kb_row, knowledge_row) is None

            after = db.fetch_one(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND slug=?",
                (kb_id, slug),
            )
            assert after
            assert after["content"] == table_content
            assert after["version"] == before["version"]
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM wiki_page_revisions WHERE page_id=?",
                (before["id"],),
            )["n"] == revisions_before

    _run(case)


def test_wiki_graph_counts_distinct_visible_neighbors() -> None:
    pages = [
        {
            "slug": "p", "title": "P", "page_type": "entity", "updated_at": "2026-01-01 00:00:00",
            "source_refs": [], "out_links": ["q", "q", "gone", "p", "idx"], "in_links": ["q"],
        },
        {
            "slug": "q", "title": "Q", "page_type": "entity", "updated_at": "2026-01-01 00:00:00",
            "source_refs": [], "out_links": ["p"], "in_links": ["p"],
        },
        {
            "slug": "idx", "title": "Index", "page_type": "index", "updated_at": "2026-01-01 00:00:00",
            "source_refs": [], "out_links": [], "in_links": ["p"],
        },
    ]

    graph = wiki_router._build_wiki_graph(
        pages, mode="overview", center="", depth=1, types=["entity"], limit=500, familiar_ids=set()
    )

    assert {node["slug"]: node["link_count"] for node in graph["nodes"]} == {"p": 1, "q": 1}
    assert graph["meta"] == {"mode": "overview", "total": 2, "returned": 2, "truncated": False}
    assert {(edge["source"], edge["target"]) for edge in graph["edges"]} == {("p", "q"), ("q", "p")}


def test_wiki_graph_subset_contract_and_familiar_sources() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Wiki graph contract " + uuid.uuid4().hex[:8], "wiki_config": {"enabled": True}},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            async def create_page(
                title: str, slug: str, page_type: str, content: str = "", source_refs: list | None = None
            ) -> None:
                response = await client.post(
                    f"/api/v1/knowledgebase/{kb_id}/wiki/pages",
                    headers=headers,
                    json={
                        "title": title,
                        "slug": slug,
                        "page_type": page_type,
                        "content": content,
                        "source_refs": source_refs or [],
                    },
                )
                assert response.status_code == 201, response.text

            await create_page("Index", "index", "index")
            await create_page(
                "Ollama Tags", "summary/ollama", "summary", "[[concept/linked]]",
                [{"knowledge_id": "doc-1", "title": "Ollama"}],
            )
            await create_page(
                "Linked Concept", "concept/linked", "concept", "[[summary/ollama]]",
                [{"knowledge_id": "doc-3"}],
            )
            await create_page(
                "Qwen", "entity/qwen", "entity", "", [{"knowledge_id": "doc-1"}],
            )
            await create_page(
                "Other", "concept/other", "concept", "", [{"knowledge_id": "doc-2"}],
            )
            await create_page("Isolated", "entity/isolated", "entity")

            overview = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/graph", headers=headers
            )
            assert overview.status_code == 200, overview.text
            graph = overview.json()["data"]
            assert [node["slug"] for node in graph["nodes"]] == [
                "summary/ollama", "concept/linked", "entity/qwen", "concept/other", "entity/isolated",
            ]
            assert {node["slug"]: node["link_count"] for node in graph["nodes"]} == {
                "summary/ollama": 2,
                "concept/linked": 1,
                "entity/qwen": 1,
                "concept/other": 0,
                "entity/isolated": 0,
            }
            assert "index" not in {node["slug"] for node in graph["nodes"]}
            assert {(edge["source"], edge["target"]) for edge in graph["edges"]} == {
                ("summary/ollama", "concept/linked"),
                ("concept/linked", "summary/ollama"),
                ("summary/ollama", "entity/qwen"),
            }
            assert graph["meta"] == {"mode": "overview", "total": 5, "returned": 5, "truncated": False}

            entities = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/graph?types=entity", headers=headers
            )
            assert {node["slug"] for node in entities.json()["data"]["nodes"]} == {
                "entity/qwen", "entity/isolated",
            }
            assert entities.json()["data"]["meta"]["total"] == 2

            index_only = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/graph?types=index", headers=headers
            )
            assert {node["slug"] for node in index_only.json()["data"]["nodes"]} == {"index"}
            assert index_only.json()["data"]["meta"]["total"] == 1

            ego = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/graph?mode=ego&center=entity/qwen&depth=1",
                headers=headers,
            )
            assert ego.status_code == 200, ego.text
            assert {node["slug"] for node in ego.json()["data"]["nodes"]} == {
                "entity/qwen", "summary/ollama",
            }
            assert ego.json()["data"]["meta"]["center"] == "entity/qwen"
            assert ego.json()["data"]["meta"]["depth"] == 1

            limited = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/graph?limit=1", headers=headers
            )
            assert [node["slug"] for node in limited.json()["data"]["nodes"]] == ["summary/ollama"]
            assert limited.json()["data"]["meta"]["truncated"] is True

            for query in ("mode=invalid", "mode=ego", "depth=zero", "limit=0"):
                invalid = await client.get(
                    f"/api/v1/knowledgebase/{kb_id}/wiki/graph?{query}", headers=headers
                )
                assert invalid.status_code == 400, (query, invalid.text)
            missing_center = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/graph?mode=ego&center=missing", headers=headers
            )
            assert missing_center.status_code == 500, missing_center.text

            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO memory_doc_affinity"
                    "(id, tenant_id, subject_id, knowledge_id, knowledge_base_id, title, hits, last_used_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (db.new_id(), tenant_id, login["user"]["id"], "doc-1", kb_id, "Ollama", 2, db.utcnow()),
                )
            familiar = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/graph", headers=headers
            )
            familiar_graph = familiar.json()["data"]
            familiar_slugs = {node["slug"] for node in familiar_graph["nodes"] if node.get("familiar")}
            assert familiar_slugs == {"summary/ollama", "entity/qwen"}
            assert familiar_graph["meta"]["familiar_count"] == 2

    _run(case)


def test_shared_knowledge_base_is_readable_but_not_writable() -> None:
    async def case() -> None:
        async with await _client() as client:
            owner = await _login(client)
            owner_headers = _headers(owner)

            suffix = uuid.uuid4().hex[:10]
            guest_credentials = {
                "username": "guest-" + suffix,
                "email": f"guest-{suffix}@example.com",
                "password": "GuestPass123",
            }
            registered = await client.post("/api/v1/auth/register", json=guest_credentials)
            assert registered.status_code == 201, registered.text
            guest_tenant_id = registered.json()["user"]["tenant_id"]
            guest = await _login(
                client,
                {"email": guest_credentials["email"], "password": guest_credentials["password"]},
            )
            guest_headers = _headers(guest)

            organization = await client.post(
                "/api/v1/organizations",
                headers=owner_headers,
                json={"name": "Share smoke " + suffix},
            )
            assert organization.status_code == 201, organization.text
            org_id = organization.json()["data"]["id"]

            invited = await client.post(
                f"/api/v1/organizations/{org_id}/invite",
                headers=owner_headers,
                json={"tenant_id": guest_tenant_id, "role": "viewer"},
            )
            assert invited.status_code == 200, invited.text

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=owner_headers,
                json={"name": "Shared smoke " + suffix, "wiki_config": {"enabled": True}},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=owner_headers,
                json={"title": "共享文档", "content": "租户成员可以读取这段共享知识。"},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]
            tag = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/tags",
                headers=owner_headers,
                json={"name": "shared-tag"},
            )
            assert tag.status_code == 201, tag.text
            tag_id = tag.json()["data"]["id"]
            faq = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entry",
                headers=owner_headers,
                json={"standard_question": "Shared question?", "answer": "Shared answer."},
            )
            assert faq.status_code == 201, faq.text
            faq_id = faq.json()["data"]["id"]
            llm = await client.post(
                "/api/v1/models",
                headers=owner_headers,
                json={
                    "name": "shared-llm-" + suffix,
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {"base_url": "https://shared-llm.test/v1", "api_key": "shared-secret"},
                },
            )
            assert llm.status_code == 201, llm.text
            initialized = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=owner_headers,
                json={"llmModelId": llm.json()["data"]["id"]},
            )
            assert initialized.status_code == 200, initialized.text

            shared = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/shares",
                headers=owner_headers,
                json={"organization_id": org_id, "permission": "viewer"},
            )
            assert shared.status_code == 201, shared.text

            detail = await client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=guest_headers)
            assert detail.status_code == 200, detail.text

            documents = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge",
                headers=guest_headers,
            )
            assert documents.status_code == 200, documents.text
            assert any(item["id"] == knowledge_id for item in documents.json()["data"])

            chunks = await client.get(f"/api/v1/chunks/{knowledge_id}", headers=guest_headers)
            assert chunks.status_code == 200, chunks.text

            wiki_pages = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages",
                headers=guest_headers,
            )
            assert wiki_pages.status_code == 200, wiki_pages.text
            assert any(page["title"] == "共享文档" for page in wiki_pages.json()["data"]["pages"])

            wiki_search = await client.get(
                f"/api/v1/knowledgebase/{kb_id}/wiki/search?q=共享",
                headers=guest_headers,
            )
            assert wiki_search.status_code == 200, wiki_search.text
            assert wiki_search.json()["data"]

            shared_tags = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/tags", headers=guest_headers
            )
            assert shared_tags.status_code == 200, shared_tags.text
            assert any(item["id"] == tag_id for item in shared_tags.json()["data"])
            shared_faq = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries/{faq_id}",
                headers=guest_headers,
            )
            assert shared_faq.status_code == 200, shared_faq.text
            assert shared_faq.json()["data"]["standard_question"] == "Shared question?"
            shared_config = await client.get(
                f"/api/v1/initialization/config/{kb_id}", headers=guest_headers
            )
            assert shared_config.status_code == 200, shared_config.text
            shared_llm = shared_config.json()["data"]["llm"]
            assert shared_llm["modelName"] == "shared-llm-" + suffix
            assert shared_llm["baseUrl"] == ""
            assert shared_llm["credentials"]["apiKey"] is True

            denied_download = await client.get(
                f"/api/v1/knowledge/{knowledge_id}/download", headers=guest_headers
            )
            assert denied_download.status_code == 403, denied_download.text
            denied_batch_download = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/batch-download",
                headers=guest_headers,
                json={"ids": [knowledge_id]},
            )
            assert denied_batch_download.status_code == 403, denied_batch_download.text

            denied = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=guest_headers,
                json={"name": "must not change"},
            )
            assert denied.status_code == 403

    _run(case)


def test_shared_editor_updates_kb_but_only_share_admin_updates_vlm() -> None:
    async def case() -> None:
        async with await _client() as client:
            owner = await _login(client)
            owner_headers = _headers(owner)

            suffix = uuid.uuid4().hex[:10]
            guest_credentials = {
                "username": "editor-" + suffix,
                "email": f"editor-{suffix}@example.com",
                "password": "EditorPass123",
            }
            registered = await client.post("/api/v1/auth/register", json=guest_credentials)
            assert registered.status_code == 201, registered.text
            guest_tenant_id = registered.json()["user"]["tenant_id"]
            guest = await _login(
                client,
                {"email": guest_credentials["email"], "password": guest_credentials["password"]},
            )
            guest_headers = _headers(guest)

            organization = await client.post(
                "/api/v1/organizations",
                headers=owner_headers,
                json={"name": "VLM share " + suffix},
            )
            assert organization.status_code == 201, organization.text
            org_id = organization.json()["data"]["id"]
            invited = await client.post(
                f"/api/v1/organizations/{org_id}/invite",
                headers=owner_headers,
                json={"tenant_id": guest_tenant_id, "role": "editor"},
            )
            assert invited.status_code == 200, invited.text

            created = await client.post(
                "/api/v1/knowledge-bases",
                headers=owner_headers,
                json={"name": "Editor shared " + suffix},
            )
            assert created.status_code == 201, created.text
            kb_id = created.json()["data"]["id"]
            shared = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/shares",
                headers=owner_headers,
                json={"organization_id": org_id, "permission": "editor"},
            )
            assert shared.status_code == 201, shared.text

            updated = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=guest_headers,
                json={"name": "Editor renamed " + suffix, "description": "shared edit"},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["name"] == "Editor renamed " + suffix
            row = db.fetch_one("SELECT name, description FROM knowledge_bases WHERE id=?", (kb_id,))
            assert row == {"name": "Editor renamed " + suffix, "description": "shared edit"}

            denied = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=guest_headers,
                json={
                    "name": "must not change " + suffix,
                    "vlm_config": {"enabled": False},
                },
            )
            assert denied.status_code == 403, denied.text
            row = db.fetch_one("SELECT name, vlm_config FROM knowledge_bases WHERE id=?", (kb_id,))
            assert row["name"] == "Editor renamed " + suffix
            assert json.loads(row["vlm_config"]) == {}

            now = db.utcnow()
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE kb_shares SET permission='admin', updated_at=? "
                    "WHERE knowledge_base_id=? AND organization_id=?",
                    (now, kb_id, org_id),
                )
                conn.execute(
                    "UPDATE organization_tenant_members SET role='admin', updated_at=? "
                    "WHERE organization_id=? AND tenant_id=?",
                    (now, org_id, guest_tenant_id),
                )

            vlm = await client.post(
                "/api/v1/models",
                headers=guest_headers,
                json={
                    "name": "shared-vlm-" + suffix,
                    "type": "VLLM",
                    "source": "remote",
                    "parameters": {"base_url": "https://shared-vlm.test/v1"},
                },
            )
            assert vlm.status_code == 201, vlm.text
            allowed = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=guest_headers,
                json={
                    "vlm_config": {
                        "enabled": True,
                        "model_id": vlm.json()["data"]["id"],
                    }
                },
            )
            assert allowed.status_code == 200, allowed.text
            assert allowed.json()["data"]["vlm_config"]["enabled"] is True
            stored = json.loads(db.fetch_one(
                "SELECT vlm_config FROM knowledge_bases WHERE id=?", (kb_id,)
            )["vlm_config"])
            assert stored["enabled"] is True
            assert stored["model_id"] == vlm.json()["data"]["id"]

    _run(case)


def test_rss_datasource_syncs_and_skips_unchanged_items(monkeypatch) -> None:
    from app.services import datasource as datasource_connector

    monkeypatch.setattr(
        datasource_connector,
        "_http_get",
        lambda _url, headers=None, timeout=30: RSS_FEED.encode(),
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Datasource smoke"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            types = await client.get("/api/v1/datasource/types", headers=headers)
            assert types.status_code == 200, types.text
            type_ids = {item["type"] for item in types.json()["data"]}
            assert {"rss", "web_crawler"}.issubset(type_ids)

            feed_url = "http://feed.test/feed.xml"
            validated = await client.post(
                "/api/v1/datasource/validate-credentials",
                headers=headers,
                json={"type": "rss", "credentials": {"feed_urls": feed_url}},
            )
            assert validated.status_code == 200, validated.text

            created = await client.post(
                "/api/v1/datasource",
                headers=headers,
                json={
                    "name": "Smoke feed",
                    "type": "rss",
                    "knowledge_base_id": kb_id,
                    "sync_mode": "incremental",
                    "sync_deletions": True,
                    "config": {
                        "credentials": {},
                        "resource_ids": [feed_url],
                        "settings": {"feed_urls": feed_url},
                    },
                },
            )
            assert created.status_code == 201, created.text
            source_id = created.json()["data"]["id"]

            resources = await client.get(
                f"/api/v1/datasource/{source_id}/resources",
                headers=headers,
            )
            assert resources.status_code == 200, resources.text
            assert [item["external_id"] for item in resources.json()["data"]] == [feed_url]

            first = await client.post(f"/api/v1/datasource/{source_id}/sync", headers=headers, json={})
            assert first.status_code == 200, first.text
            first_log = first.json()["data"]
            assert first_log["status"] == "success"
            assert first_log["items_created"] == 1
            assert first_log["items_failed"] == 0

            documents = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge",
                headers=headers,
            )
            assert documents.status_code == 200, documents.text
            synced = [item for item in documents.json()["data"] if item["title"] == "Release notes"]
            assert len(synced) == 1

            detail = await client.get(f"/api/v1/datasource/{source_id}", headers=headers)
            assert detail.status_code == 200, detail.text
            assert detail.json()["data"]["latest_sync_log"]["items_created"] == 1

            second = await client.post(f"/api/v1/datasource/{source_id}/sync", headers=headers, json={})
            assert second.status_code == 200, second.text
            second_log = second.json()["data"]
            assert second_log["status"] == "success"
            assert second_log["items_created"] == 0
            assert second_log["items_skipped"] == 1

    _run(case)


def test_web_datasource_extracts_page_content(monkeypatch) -> None:
    from app.services import datasource as datasource_connector

    page_url = "http://pages.test/operations.html"
    monkeypatch.setattr(
        datasource_connector,
        "_http_get",
        lambda url, headers=None, timeout=30: HTML_PAGE.encode() if url == page_url else b"",
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Web datasource smoke"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            created = await client.post(
                "/api/v1/datasource",
                headers=headers,
                json={
                    "name": "Smoke page",
                    "type": "web_crawler",
                    "knowledge_base_id": kb_id,
                    "config": {
                        "credentials": {},
                        "resource_ids": [page_url],
                        "settings": {"urls": page_url},
                    },
                },
            )
            assert created.status_code == 201, created.text
            source_id = created.json()["data"]["id"]

            sync = await client.post(f"/api/v1/datasource/{source_id}/sync", headers=headers, json={})
            assert sync.status_code == 200, sync.text
            assert sync.json()["data"]["items_created"] == 1

            documents = await client.get(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge",
                headers=headers,
            )
            assert documents.status_code == 200, documents.text
            synced = [item for item in documents.json()["data"] if item["title"] == "Operations page"]
            assert len(synced) == 1

            chunks = await client.get(f"/api/v1/chunks/{synced[0]['id']}", headers=headers)
            assert chunks.status_code == 200, chunks.text
            text = " ".join(item["content"] for item in chunks.json()["data"])
            assert "Check the health endpoint" in text
            assert "ignored()" not in text

    _run(case)


def test_dingtalk_datasource_syncs_and_reconciles_deletions(monkeypatch) -> None:
    from app.services import dingtalk

    workspace = dingtalk.Workspace("space", "root", "Team space")
    root_nodes = [dingtalk.Node(
        "doc-1", workspace_id="space", name="Roadmap", type="FILE",
        category="ALIDOC", extension="adoc", modified_timestamp=1_700_000_000_000,
    )]
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_workspaces", lambda self: [workspace])
    monkeypatch.setattr(dingtalk.DingTalkClient, "list_nodes", lambda self, parent: list(root_nodes) if parent == "root" else [])
    monkeypatch.setattr(dingtalk.DingTalkClient, "document_blocks", lambda self, document_id: [
        {"blockType": "paragraph", "children": [{"elementType": "text", "text": "Q3 goals"}]},
    ])

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "DingTalk datasource smoke"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            created = await client.post(
                "/api/v1/datasource",
                headers=headers,
                json={
                    "name": "DingTalk docs",
                    "type": "dingtalk",
                    "knowledge_base_id": kb_id,
                    "sync_deletions": True,
                    "config": {
                        "credentials": {
                            "client_id": "ding-app",
                            "client_secret": "secret",
                            "operator_id": "union-id",
                        },
                        "resource_ids": ["space"],
                        "settings": {},
                    },
                },
            )
            assert created.status_code == 201, created.text
            source_id = created.json()["data"]["id"]

            resources = await client.get(f"/api/v1/datasource/{source_id}/resources", headers=headers)
            assert resources.status_code == 200, resources.text
            assert resources.json()["data"][0]["type"] == "wiki_space"

            first = await client.post(f"/api/v1/datasource/{source_id}/sync", headers=headers, json={})
            assert first.status_code == 200, first.text
            assert first.json()["data"]["items_created"] == 1
            assert first.json()["data"]["status"] == "success"

            stored = db.fetch_one(
                "SELECT metadata FROM knowledges WHERE knowledge_base_id=? AND title='Roadmap' AND deleted_at IS NULL",
                (kb_id,),
            )
            stored_metadata = json.loads(stored["metadata"])
            assert stored_metadata["workspace_id"] == "space"
            assert stored_metadata["node_id"] == "doc-1"

            root_nodes.clear()
            second = await client.post(f"/api/v1/datasource/{source_id}/sync", headers=headers, json={})
            assert second.status_code == 200, second.text
            assert second.json()["data"]["items_deleted"] == 1
            assert second.json()["data"]["status"] == "success"

            documents = await client.get(f"/api/v1/knowledge-bases/{kb_id}/knowledge", headers=headers)
            assert documents.status_code == 200, documents.text
            assert all(item["title"] != "Roadmap" for item in documents.json()["data"])

    _run(case)


def test_seafile_datasource_syncs_browses_and_reconciles_deletions(monkeypatch) -> None:
    from app.services import seafile

    repo_id = "11111111-2222-3333-4444-555555555555"
    files = {"/docs/guide.md": b"# Guide\n\nThe Seafile sync marker is present."}
    calls: list[tuple[str, dict[str, str]]] = []

    def request(method, url, *, headers=None, timeout=30, max_bytes=0, **_kwargs):
        del method, timeout, max_bytes
        calls.append((url, dict(headers or {})))
        parsed = urllib.parse.urlsplit(url)
        if parsed.path == "/api2/auth/ping/":
            return 200, {}, json.dumps("pong").encode(), url
        if parsed.path == "/api2/repos/":
            return 200, {}, json.dumps([
                {"id": repo_id, "name": "Library", "type": "repo", "encrypted": False},
            ]).encode(), url
        if parsed.path.startswith("/files/"):
            return 200, {}, files["/docs/guide.md"], url
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api2", "repos"]:
            path = urllib.parse.parse_qs(parsed.query).get("p", [""])[0]
            if parts[3] == "dir":
                if path == "/":
                    entries = [{"id": "docs", "name": "docs", "type": "dir", "mtime": 1}]
                elif path == "/docs":
                    entries = [
                        {
                            "id": "guide-oid",
                            "name": name.rsplit("/", 1)[-1],
                            "type": "file",
                            "mtime": 1,
                            "size": len(body),
                        }
                        for name, body in files.items()
                    ]
                else:
                    return 404, {}, b"missing", url
                return 200, {}, json.dumps(entries).encode(), url
            if parts[3] == "file" and path in files:
                return 200, {"oid": "guide-oid"}, json.dumps(
                    "https://files.test/files/guide"
                ).encode(), url
            return 404, {}, b"missing", url
        raise AssertionError(url)

    monkeypatch.setattr(seafile.url_guard, "request_url", request)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Seafile datasource smoke"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            validated = await client.post(
                "/api/v1/datasource/validate-credentials",
                headers=headers,
                json={
                    "type": "seafile",
                    "credentials": {
                        "base_url": "https://seafile.test",
                        "api_token": "secret-token",
                    },
                },
            )
            assert validated.status_code == 200, validated.text

            created = await client.post(
                "/api/v1/datasource",
                headers=headers,
                json={
                    "name": "Seafile docs",
                    "type": "seafile",
                    "knowledge_base_id": kb_id,
                    "sync_deletions": True,
                    "config": {
                        "credentials": {
                            "base_url": "https://seafile.test",
                            "api_token": "secret-token",
                        },
                        "resource_ids": [repo_id + ":/docs"],
                        "settings": {},
                    },
                },
            )
            assert created.status_code == 201, created.text
            source_id = created.json()["data"]["id"]

            libraries = await client.get(
                f"/api/v1/datasource/{source_id}/resources", headers=headers
            )
            assert libraries.status_code == 200, libraries.text
            assert libraries.json()["data"][0]["type"] == "library"
            children = await client.get(
                f"/api/v1/datasource/{source_id}/resources?parent_id={repo_id}%3A%2Fdocs",
                headers=headers,
            )
            assert children.status_code == 200, children.text
            assert children.json()["data"][0]["name"] == "guide.md"
            ancestors = await client.post(
                f"/api/v1/datasource/{source_id}/resource-ancestors",
                headers=headers,
                json={"resource_ids": [repo_id + ":/docs/guide.md"]},
            )
            assert ancestors.status_code == 200, ancestors.text
            assert ancestors.json()["data"]["ancestors"] == [repo_id + ":/", repo_id + ":/docs"]

            first = await client.post(
                f"/api/v1/datasource/{source_id}/sync", headers=headers, json={}
            )
            assert first.status_code == 200, first.text
            assert first.json()["data"]["items_created"] == 1
            document = db.fetch_one(
                "SELECT title, file_name, file_type, metadata FROM knowledges "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (kb_id,),
            )
            assert document["title"] == "Library/docs/guide.md"
            assert document["file_name"] == "Library/docs/guide.md"
            assert document["file_type"] == "md"
            assert json.loads(document["metadata"])["seafile_repo_id"] == repo_id
            assert json.loads(document["metadata"])["seafile_path"] == "/docs/guide.md"

            listed = await client.get(
                f"/api/v1/datasource?kb_id={kb_id}", headers=headers
            )
            assert listed.status_code == 200, listed.text
            assert listed.json()["data"][0]["last_sync_cursor"] is None
            detail = await client.get(f"/api/v1/datasource/{source_id}", headers=headers)
            assert detail.status_code == 200, detail.text
            assert detail.json()["data"]["last_sync_cursor"]["connector_cursor"]["items"]

            second = await client.post(
                f"/api/v1/datasource/{source_id}/sync", headers=headers, json={}
            )
            assert second.status_code == 200, second.text
            assert second.json()["data"]["items_created"] == 0
            assert second.json()["data"]["items_deleted"] == 0

            updated_source = await client.put(
                f"/api/v1/datasource/{source_id}", headers=headers, json={"sync_mode": "full"}
            )
            assert updated_source.status_code == 200, updated_source.text
            full = await client.post(
                f"/api/v1/datasource/{source_id}/sync", headers=headers, json={}
            )
            assert full.status_code == 200, full.text
            assert full.json()["data"]["items_created"] == 0
            assert full.json()["data"]["items_updated"] == 1
            assert full.json()["data"]["items_skipped"] == 0
            bound = db.fetch_one(
                "SELECT last_sync_cursor FROM data_sources WHERE id=?", (source_id,)
            )
            connector_cursor = json.loads(bound["last_sync_cursor"])["connector_cursor"]
            assert connector_cursor["repo_id"] == repo_id
            assert connector_cursor["repo_name"] == "Library"

            files.clear()
            third = await client.post(
                f"/api/v1/datasource/{source_id}/sync", headers=headers, json={}
            )
            assert third.status_code == 200, third.text
            assert third.json()["data"]["items_deleted"] == 1
            remaining = db.fetch_one(
                "SELECT id FROM knowledges WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (kb_id,),
            )
            assert remaining is None

    _run(case)


def test_mcp_usage_instructions_support_traditional_chinese() -> None:
    text = infra_router._mcp_usage_instructions(
        "zh-TW", "Demo service", ["search_knowledge", "read_document"]
    )
    assert "當使用者的問題適合使用 Demo service 時" in text
    assert "search_knowledge、read_document" in text
    assert "請根據工具返回的結果作答" in text


def test_regenerate_summary_uses_kb_summary_model(monkeypatch) -> None:
    from app.routers import knowledge as knowledge_router

    monkeypatch.setattr(knowledge_router.model_runtime, "available", lambda _runtime: True)
    monkeypatch.setattr(
        knowledge_router.model_runtime,
        "chat_completion",
        lambda *_args, **_kwargs: {
            "choices": [
                {
                    "message": {"content": json.dumps({"summary": "LLM document summary"})},
                    "finish_reason": "stop",
                }
            ]
        },
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Summary model KB"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Summary source", "content": "The document explains release checks."},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]

            summary = await client.post(
                f"/api/v1/knowledge/{knowledge_id}/regenerate-summary", headers=headers
            )
            assert summary.status_code == 200, summary.text
            assert summary.json()["data"]["description"] == "LLM document summary"
            saved = db.fetch_one("SELECT description, summary_status FROM knowledges WHERE id=?", (knowledge_id,))
            assert saved == {"description": "LLM document summary", "summary_status": "completed"}

    _run(case)


def test_initial_summary_is_async_and_reparse_requeues(monkeypatch) -> None:
    from app.services import document_summary

    calls: list[list[dict[str, str]]] = []

    def complete(_runtime, messages, **_kwargs):
        calls.append(messages)
        summary = "Initial async summary" if len(calls) == 1 else "Reparsed async summary"
        return {
            "choices": [
                {
                    "message": {"content": json.dumps({"summary": summary})},
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(document_summary.model_runtime, "available", lambda _runtime: True)
    monkeypatch.setattr(document_summary.model_runtime, "chat_completion", complete)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = {**_headers(login), "Accept-Language": "en-US"}
            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Async summary KB"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={
                    "title": "Async summary source",
                    "content": "The document explains how release checks are completed.",
                },
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]
            assert manual.json()["data"]["summary_status"] in {"pending", "processing", "completed"}

            await _wait_for_summary(
                knowledge_id, status={"completed"}, description="Initial async summary"
            )
            first_tasks = db.fetch_all(
                "SELECT * FROM py_task_results WHERE kind='summary_generation' AND payload LIKE ?",
                (f"%{knowledge_id}%",),
            )
            assert len(first_tasks) == 1
            assert json.loads(first_tasks[0]["payload"])["language"] == "English"
            assert first_tasks[0]["status"] == "completed"
            assert "English" in calls[0][0]["content"]

            reparsed = await client.post(
                f"/api/v1/knowledge/{knowledge_id}/reparse", headers=headers, json={}
            )
            assert reparsed.status_code == 200, reparsed.text
            await _wait_for_summary(
                knowledge_id, status={"completed"}, description="Reparsed async summary"
            )
            second_tasks = db.fetch_all(
                "SELECT * FROM py_task_results WHERE kind='summary_generation' AND payload LIKE ?",
                (f"%{knowledge_id}%",),
            )
            assert len(second_tasks) == 2
            assert all(task["status"] == "completed" for task in second_tasks)
            assert len(calls) == 2

    _run(case)


def test_initial_summary_falls_back_when_model_is_unavailable(monkeypatch) -> None:
    from app.services import document_summary

    monkeypatch.setattr(document_summary.model_runtime, "available", lambda _runtime: False)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Fallback summary KB"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            content = "Release checklist: verify migrations before deployment."
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "Fallback summary source", "content": content},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]

            saved = await _wait_for_summary(
                knowledge_id, status={"failed"}, description=content
            )
            assert saved["summary_status"] == "failed"
            for _ in range(200):
                task = db.fetch_one(
                    "SELECT status, error FROM py_task_results "
                    "WHERE kind='summary_generation' AND payload LIKE ?",
                    (f"%{knowledge_id}%",),
                )
                if task and task["status"] == "failed":
                    break
                await asyncio.sleep(0.01)
            assert task is not None
            assert task["status"] == "failed"
            assert "unavailable" in task["error"]

            gated = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("gated.txt", b"Summary stays disabled for this upload.", "text/plain")},
                data={"process_config": json.dumps({"summary_enabled": False})},
            )
            assert gated.status_code == 200, gated.text
            gated_id = gated.json()["data"]["id"]
            await asyncio.sleep(0.05)
            gated_row = db.fetch_one(
                "SELECT summary_status FROM knowledges WHERE id=?", (gated_id,)
            )
            assert gated_row == {"summary_status": "none"}
            assert db.fetch_one(
                "SELECT 1 FROM py_task_results WHERE kind='summary_generation' AND payload LIKE ?",
                (f"%{gated_id}%",),
            ) is None

    _run(case)


def test_im_channel_lease_is_exclusive_and_releasable() -> None:
    from app.services import im_leases

    db.init_db()
    channel_id = f"lease-test-{db.new_id()}"
    try:
        assert im_leases.acquire(channel_id, "owner-a") is True
        assert im_leases.renew(channel_id, "owner-a") is True
        assert im_leases.acquire(channel_id, "owner-b") is False
        im_leases.release(channel_id, "owner-a")
        assert im_leases.acquire(channel_id, "owner-b") is True
    finally:
        im_leases.release(channel_id, "owner-b")


def test_embed_channel_can_use_session_memory() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            channel = await client.post(
                "/api/v1/agents/builtin-quick-answer/embed-channels",
                headers=headers,
                json={"name": "Memory embed", "allow_memory": True},
            )
            assert channel.status_code == 201, channel.text
            channel_data = channel.json()["data"]
            channel_id = channel_data["id"]
            embed_headers = {"Authorization": "Embed " + channel_data["publish_token"]}

            session = await client.post(
                f"/api/v1/embed/{channel_id}/sessions",
                headers=embed_headers,
            )
            assert session.status_code == 201, session.text
            session_data = session.json()["data"]
            chat_headers = {**embed_headers, "X-Embed-Session": session_data["sig"]}

            first = await client.post(
                f"/api/v1/embed/{channel_id}/agent-chat/{session_data['id']}",
                headers=chat_headers,
                json={"query": "记住：我的项目代号是北辰"},
            )
            assert first.status_code == 200, first.text
            assert any(event.get("response_type") == "complete" for event in _events(first))

            second = await client.post(
                f"/api/v1/embed/{channel_id}/agent-chat/{session_data['id']}",
                headers=chat_headers,
                json={"query": "我的项目代号是什么？"},
            )
            assert second.status_code == 200, second.text
            recalled = [event for event in _events(second) if event.get("response_type") == "memory_recalled"]
            assert recalled
            assert any("北辰" in item["content"] for item in recalled[0]["data"]["memories"])

    _run(case)


def test_openrouter_legacy_thinking_control_keeps_native_reasoning(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        calls.append(json.loads(body.decode()) if body else {})
        return 200, {}, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        ).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    def runtime(thinking_control: str) -> dict:
        return model_runtime.runtime_config(
            {
                "id": "openrouter-model",
                "name": "qwen/qwen3.7-flash",
                "type": "KnowledgeQA",
                "source": "remote",
                "parameters": {
                    "provider": "openrouter",
                    "base_url": "https://openrouter.test/api/v1",
                    "thinking": False,
                    "extra_config": {"thinking_control": thinking_control},
                },
            },
            "KnowledgeQA",
        )

    model_runtime.chat_completion(runtime("none"), [{"role": "user", "content": "Hi"}])
    assert calls[0]["reasoning"] == {"enabled": False}
    assert "chat_template_kwargs" not in calls[0]

    model_runtime.chat_completion(runtime("chat_template_kwargs"), [{"role": "user", "content": "Hi"}])
    assert "reasoning" not in calls[1]
    assert calls[1]["chat_template_kwargs"] == {"enable_thinking": False}


def test_model_copy_preserves_source_configuration_and_validates_display_name() -> None:
    suffix = uuid.uuid4().hex[:10]

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": f"gpt-copy-{suffix}",
                    "display_name": f"Copy source {suffix}",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "description": "primary model",
                    "is_default": True,
                    "parameters": {
                        "provider": "openai-compatible",
                        "base_url": "https://models.test/v1",
                        "api_key": "sk-copy-source",
                        "app_secret": "app-copy-source",
                        "extra_config": {
                            "region": "us",
                            "secret_key": "stored-secret",
                        },
                        "spec": {
                            "api": "openai-completions",
                            "compat": {"temperature": "0.2"},
                        },
                    },
                },
            )
            assert created.status_code == 201, created.text
            source = created.json()["data"]
            source_id = source["id"]
            with db.transaction() as conn:
                conn.execute("UPDATE models SET status='disabled' WHERE id=?", (source_id,))

            missing_name = await client.post(f"/api/v1/models/{source_id}/copy", headers=headers)
            assert missing_name.status_code == 400, missing_name.text
            blank_name = await client.post(
                f"/api/v1/models/{source_id}/copy",
                headers=headers,
                json={"display_name": "   "},
            )
            assert blank_name.status_code == 400, blank_name.text
            long_name = await client.post(
                f"/api/v1/models/{source_id}/copy",
                headers=headers,
                json={"display_name": "名" * 256},
            )
            assert long_name.status_code == 400, long_name.text

            copied = await client.post(
                f"/api/v1/models/{source_id}/copy",
                headers=headers,
                json={"display_name": "  生产副本\n"},
            )
            assert copied.status_code == 201, copied.text
            clone = copied.json()["data"]
            assert clone["id"] != source_id
            assert clone["name"] == source["name"]
            assert clone["display_name"] == "生产副本"
            assert clone["type"] == source["type"]
            assert clone["source"] == source["source"]
            assert clone["description"] == "primary model"
            assert clone["status"] == "active"
            assert clone["is_default"] is False
            assert clone["is_builtin"] is False
            assert clone["credentials"]["api_key"]["configured"] is True
            assert clone["credentials"]["app_secret"]["configured"] is True
            assert clone["parameters"]["spec"]["compat"] == {"temperature": "0.2"}
            assert clone["parameters"]["extra_config"] == {
                "region": "us",
                "secret_key": "[REDACTED]",
            }
            serialized = json.dumps(clone)
            assert "sk-copy-source" not in serialized
            assert "app-copy-source" not in serialized
            assert "stored-secret" not in serialized

            stored = db.fetch_one("SELECT parameters FROM models WHERE id=?", (clone["id"],))
            assert stored is not None
            stored_parameters = json.loads(stored["parameters"])
            assert stored_parameters["api_key"] == "sk-copy-source"
            assert stored_parameters["app_secret"] == "app-copy-source"
            assert stored_parameters["extra_config"]["secret_key"] == "stored-secret"
            assert stored_parameters["spec"]["compat"] == {"temperature": "0.2"}

            max_length_copy = await client.post(
                f"/api/v1/models/{source_id}/copy",
                headers=headers,
                json={"display_name": "名" * 255},
            )
            assert max_length_copy.status_code == 201, max_length_copy.text
            assert len(max_length_copy.json()["data"]["display_name"]) == 255

            builtin_id = f"builtin-copy-{suffix}"
            now = db.utcnow()
            with db.transaction() as conn:
                db.insert(
                    "models",
                    {
                        "id": builtin_id,
                        "tenant_id": login["active_tenant"]["id"],
                        "name": f"builtin-{suffix}",
                        "display_name": "Builtin model",
                        "type": "KnowledgeQA",
                        "source": "remote",
                        "description": "",
                        "parameters": "{}",
                        "is_default": 0,
                        "is_builtin": 1,
                        "managed_by": "",
                        "status": "active",
                        "created_at": now,
                        "updated_at": now,
                    },
                    conn,
                )
            builtin_copy = await client.post(
                f"/api/v1/models/{builtin_id}/copy",
                headers=headers,
                json={"display_name": "Builtin copy"},
            )
            assert builtin_copy.status_code == 400, builtin_copy.text
            assert "builtin" in builtin_copy.json()["error"]["message"]

            missing_source = await client.post(
                f"/api/v1/models/missing-{suffix}/copy",
                headers=headers,
                json={"display_name": "Missing copy"},
            )
            assert missing_source.status_code == 404, missing_source.text

    _run(case)


def test_model_management_uses_saved_configuration(monkeypatch) -> None:
    from app.services import model_runtime

    upstream_calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        upstream_calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "body": json.loads(body.decode()) if body else None,
            }
        )
        return 200, {}, json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        ).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = login["active_tenant"]["id"]

            providers = await client.get("/api/v1/models/providers?model_type=chat", headers=headers)
            assert providers.status_code == 200, providers.text
            openai = next(item for item in providers.json()["data"] if item["value"] == "openai")
            assert openai["label"] == "OpenAI"
            assert openai["defaultUrls"]["chat"] == "https://api.openai.com/v1"
            assert "chat" in openai["modelTypes"]
            assert openai["models"] if "models" in openai else True

            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "gpt-test",
                    "display_name": "Test chat model",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "is_default": True,
                    "parameters": {
                        "base_url": "https://models.test/v1",
                        "api_key": "sk-secret-value",
                        "provider": "openai-compatible",
                    },
                },
            )
            assert created.status_code == 201, created.text
            model = created.json()["data"]
            model_id = model["id"]
            assert "api_key" not in model["parameters"]
            assert model["credentials"]["api_key"]["configured"] is True

            listed = await client.get("/api/v1/models", headers=headers)
            assert listed.status_code == 200, listed.text
            saved = next(item for item in listed.json()["data"] if item["id"] == model_id)
            assert "sk-secret-value" not in json.dumps(saved)
            assert saved["parameters"]["base_url"] == "https://models.test/v1"

            credentials = await client.put(
                f"/api/v1/models/{model_id}/credentials",
                headers=headers,
                json={"api_key": "sk-new-secret"},
            )
            assert credentials.status_code == 200, credentials.text
            assert credentials.json()["fields"]["api_key"]["configured"] is True

            debug = await client.post(
                f"/api/v1/models/{model_id}/debug",
                headers=headers,
                data={"input": "ping", "documents": "[]", "options": "{}"},
            )
            assert debug.status_code == 200, debug.text
            result = debug.json()["data"]
            assert result["ok"] is True, result
            assert result["raw_response"]["choices"][0]["message"]["content"] == "pong"
            assert upstream_calls[-1]["url"] == "https://models.test/v1/chat/completions"
            assert upstream_calls[-1]["headers"]["Authorization"] == "Bearer sk-new-secret"

            runtime = model_runtime.chat_runtime(tenant_id, {"model_id": model_id}, {})
            assert runtime["id"] == model_id
            assert runtime["base_url"] == "https://models.test/v1"
            assert runtime["api_key"] == "sk-new-secret"

    _run(case)


def test_native_multimodal_embedding_protocols_encode_text(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else None
        calls.append({"url": url, "body": payload})
        if url.endswith(":batchEmbedContents"):
            return 200, {}, json.dumps({"embeddings": [{"values": [1.0, 2.0]}, {"values": [3.0, 4.0]}]}).encode()
        if url.endswith("/multimodal-embedding/multimodal-embedding"):
            return 200, {}, json.dumps(
                {"output": {"embeddings": [{"index": 0, "embedding": [1.0, 2.0]}, {"index": 1, "embedding": [3.0, 4.0]}]}}
            ).encode()
        return 200, {}, json.dumps({"data": {"embedding": [1.0, 2.0]}}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    ark = {
        "id": "ark-text",
        "name": "doubao-embedding-vision-251215",
        "provider": "volcengine",
        "base_url": "https://ark.test/api/v3",
        "headers": {},
        "spec": {"input": ["text", "image"]},
    }
    vectors = model_runtime.embedding(ark, ["a", "b"])
    assert vectors == [[1.0, 2.0], [1.0, 2.0]]
    assert [call["url"] for call in calls] == [
        "https://ark.test/api/v3/embeddings/multimodal",
        "https://ark.test/api/v3/embeddings/multimodal",
    ]
    assert calls[0]["body"] == {
        "model": "doubao-embedding-vision-251215",
        "encoding_format": "float",
        "input": [{"type": "text", "text": "a"}],
    }

    calls.clear()
    dashscope = {
        "id": "dash-text",
        "name": "qwen3-vl-embedding",
        "provider": "aliyun",
        "base_url": "https://dashscope.test/compatible-mode/v1",
        "headers": {},
        "spec": {"input": ["text", "image"]},
    }
    vectors = model_runtime.embedding(dashscope, ["a", "b"])
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]
    assert calls == [{
        "url": "https://dashscope.test/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
        "body": {
            "model": "qwen3-vl-embedding",
            "input": {"contents": [{"text": "a"}, {"text": "b"}]},
        },
    }]

    calls.clear()
    gemini = {
        "id": "gemini-text",
        "name": "gemini-embedding-2",
        "provider": "gemini",
        "base_url": "https://gemini.test/v1beta/openai",
        "headers": {},
        "spec": {},
    }
    vectors = model_runtime.embedding(gemini, ["a", "b"])
    assert vectors == [[1.0, 2.0], [3.0, 4.0]]
    assert calls == [{
        "url": "https://gemini.test/v1beta/models/gemini-embedding-2:batchEmbedContents",
        "body": {"requests": [
            {"model": "models/gemini-embedding-2", "content": {"parts": [{"text": "a"}]}},
            {"model": "models/gemini-embedding-2", "content": {"parts": [{"text": "b"}]}},
        ]},
    }]


def test_qwen3_vl_messages_batching_preserves_input_order(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else {}
        calls.append(payload)
        messages = payload.get("messages") or []
        conversations = messages if messages and isinstance(messages[0], list) else [messages]
        answers = []
        for index in range(len(conversations) - 1, -1, -1):
            part = conversations[index][0]["content"][0]
            if part["type"] == "text":
                value = ord(part["text"]) - ord("a")
            else:
                value = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])[-1]
            answers.append({"index": index, "embedding": [float(value)]})
        return 200, {}, json.dumps({"data": [
            answer for answer in answers
        ]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)
    runtime = {
        "name": "Qwen/Qwen3-VL-Embedding-8B",
        "provider": "generic",
        "base_url": "https://vllm.test/v1",
        "headers": {},
        "spec": {
            "input": ["text", "image"],
            "compat": {
                "batch_messages": True,
                "max_image_batch_size": 5,
                "extra_body": {"add_special_tokens": True},
            },
        },
    }
    assert model_runtime.embedding(runtime, ["a", "b"]) == [[0.0], [1.0]]
    assert len(calls) == 1
    assert calls[0]["messages"][1][0]["content"] == [{"type": "text", "text": "b"}]
    assert calls[0]["encoding_format"] == "float"
    assert calls[0]["add_special_tokens"] is True

    calls.clear()
    images = [b"\x89PNG\r\n\x1a\n" + bytes([index]) for index in range(6)]
    assert model_runtime.image_embeddings(runtime, images) == [
        [0.0], [1.0], [2.0], [3.0], [4.0], [5.0]
    ]
    assert len(calls) == 2
    assert [len(call["messages"]) for call in calls] == [5, 1]

    calls.clear()
    sequential = {**runtime, "spec": {**runtime["spec"], "compat": {
        "batch_messages": False,
        "extra_body": {"add_special_tokens": True},
    }}}
    assert model_runtime.embedding(sequential, ["a", "b"]) == [[0.0], [1.0]]
    assert len(calls) == 2
    assert isinstance(calls[0]["messages"][0], dict)


def test_model_debug_image_embedding_wire_formats_and_validation(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []
    png = b"\x89PNG\r\n\x1a\n" + b"image-bytes"
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode()

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else None
        calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": payload})
        if url.endswith(":batchEmbedContents"):
            return 200, {}, json.dumps({"embeddings": [{"values": [1.0, 2.0, 3.0]}]}).encode()
        if url.endswith("/embeddings/multimodal"):
            return 200, {}, json.dumps({"data": {"embedding": [1.0, 2.0, 3.0]}}).encode()
        if url.endswith("/multimodal-embedding/multimodal-embedding"):
            return 200, {}, json.dumps(
                {"output": {"embeddings": [{"index": 0, "embedding": [1.0, 2.0, 3.0]}]}}
            ).encode()
        return 200, {}, json.dumps({"data": [{"embedding": [1.0, 2.0, 3.0]}]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]
            cases = [
                {
                    "name": "doubao-embedding-vision-251215",
                    "provider": "volcengine",
                    "base_url": "https://ark.test/api/v3",
                    "spec": {"input": ["text", "image"]},
                    "url": "https://ark.test/api/v3/embeddings/multimodal",
                    "body": {
                        "model": "doubao-embedding-vision-251215",
                        "encoding_format": "float",
                        "input": [{"type": "image_url", "image_url": {"url": data_uri}}],
                    },
                },
                {
                    "name": "tongyi-embedding-vision-plus",
                    "provider": "aliyun",
                    "base_url": "https://dashscope.test/compatible-mode/v1",
                    "spec": {"input": ["text", "image"]},
                    "url": "https://dashscope.test/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
                    "body": {"model": "tongyi-embedding-vision-plus", "input": {"contents": [{"image": data_uri}]}},
                },
                {
                    "name": "gemini-embedding-2",
                    "provider": "gemini",
                    "base_url": "https://gemini.test/v1beta",
                    "spec": {},
                    "url": "https://gemini.test/v1beta/models/gemini-embedding-2:batchEmbedContents",
                    "body": {
                        "requests": [{
                            "model": "models/gemini-embedding-2",
                            "content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(png).decode()}}]},
                        }],
                    },
                },
                {
                    "name": "jina-embeddings-v5-omni-small",
                    "provider": "jina",
                    "base_url": "https://jina.test/v1",
                    "spec": {},
                    "url": "https://jina.test/v1/embeddings",
                    "body": {"model": "jina-embeddings-v5-omni-small", "truncate": True, "input": [{"image": data_uri}]},
                },
                {
                    "name": "TIGER-Lab/VLM2Vec-Full",
                    "provider": "generic",
                    "base_url": "https://vllm.test/v1",
                    "spec": {"input": ["text", "image"]},
                    "url": "https://vllm.test/v1/embeddings",
                    "body": {
                        "model": "TIGER-Lab/VLM2Vec-Full",
                        "encoding_format": "float",
                        "truncate_prompt_tokens": 511,
                        "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": data_uri}}]}],
                    },
                },
            ]
            model_ids: dict[str, str] = {}
            for item in cases:
                created = await client.post(
                    "/api/v1/models",
                    headers=headers,
                    json={
                        "name": item["name"],
                        "display_name": item["name"] + " " + suffix,
                        "type": "Embedding",
                        "source": "remote",
                        "parameters": {
                            "base_url": item["base_url"],
                            "api_key": "embed-key",
                            "provider": item["provider"],
                            "spec": item["spec"],
                        },
                    },
                )
                assert created.status_code == 201, created.text
                model = created.json()["data"]
                assert model["capabilities"]["input"] == ["text", "image"]
                model_ids[item["name"]] = model["id"]
                before = len(calls)
                debug = await client.post(
                    f"/api/v1/models/{model['id']}/debug",
                    headers=headers,
                    data={"input": "", "documents": "[]", "options": "{}"},
                    files={"file": ("test.png", png, "image/png")},
                )
                assert debug.status_code == 200, debug.text
                result = debug.json()["data"]
                assert result["ok"] is True, result
                assert result["raw_response"] == [1.0, 2.0, 3.0]
                assert result["observations"] == {"result_count": 1, "dimension": 3}
                assert len(calls) == before + 1
                assert calls[-1]["url"] == item["url"]
                assert calls[-1]["body"] == item["body"]

            text_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "text-embedding-" + suffix,
                    "type": "Embedding",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://text-embedding.test/v1",
                        "provider": "openai-compatible",
                        "spec": {"input": ["text"]},
                    },
                },
            )
            assert text_model.status_code == 201, text_model.text
            text_model_data = text_model.json()["data"]
            assert text_model_data["capabilities"]["input"] == ["text"]
            before = len(calls)
            unsupported = await client.post(
                f"/api/v1/models/{text_model_data['id']}/debug",
                headers=headers,
                data={"input": "", "documents": "[]", "options": "{}"},
                files={"file": ("test.png", png, "image/png")},
            )
            assert unsupported.json()["data"]["ok"] is False
            assert "does not accept images" in unsupported.json()["data"]["error"]
            assert len(calls) == before

            image_model_id = model_ids["jina-embeddings-v5-omni-small"]
            mixed = await client.post(
                f"/api/v1/models/{image_model_id}/debug",
                headers=headers,
                data={"input": "hello", "documents": "[]", "options": "{}"},
                files={"file": ("test.png", png, "image/png")},
            )
            assert "either text or an image" in mixed.json()["data"]["error"]
            invalid = await client.post(
                f"/api/v1/models/{image_model_id}/debug",
                headers=headers,
                data={"input": "", "documents": "[]", "options": "{}"},
                files={"file": ("test.png", b"not an image", "image/png")},
            )
            assert "not an image" in invalid.json()["data"]["error"]
            empty = await client.post(
                f"/api/v1/models/{image_model_id}/debug",
                headers=headers,
                data={"input": "", "documents": "[]", "options": "{}"},
            )
            assert "input cannot be empty" in empty.json()["data"]["error"]
            assert len(calls) == before

    _run(case)


def test_model_debug_image_embedding_similarity(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []
    png = b"\x89PNG\r\n\x1a\n" + b"similarity-image"

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else {}
        calls.append({"url": url, "body": payload})
        value = payload.get("input")
        if isinstance(value, list) and value and isinstance(value[0], dict):
            vector = [0.6, 0.8, 0.0]
        elif value == ["dog"]:
            vector = [1.0, 0.0, 0.0]
        elif value == ["car"]:
            vector = [0.0, 1.0, 0.0]
        else:
            raise AssertionError(payload)
        return 200, {}, json.dumps({"data": [{"embedding": vector}]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "jina-embeddings-v5-omni-small",
                    "display_name": "similarity " + uuid.uuid4().hex[:8],
                    "type": "Embedding",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://jina.test/v1",
                        "provider": "jina",
                        "spec": {"input": ["text", "image"]},
                    },
                },
            )
            assert created.status_code == 201, created.text
            model_id = created.json()["data"]["id"]
            debug = await client.post(
                f"/api/v1/models/{model_id}/debug",
                headers=headers,
                data={
                    "input": "dog",
                    "documents": '["car"]',
                    "options": '{"similarity": true}',
                },
                files={"file": ("test.png", png, "image/png")},
            )
            assert debug.status_code == 200, debug.text
            result = debug.json()["data"]
            assert result["ok"] is True, result
            assert result["observations"] == {"dimension": 3, "result_count": 2}
            assert len(calls) == 3
            assert calls[0]["body"]["input"] == ["dog"]
            assert calls[1]["body"]["input"] == ["car"]
            assert calls[2]["body"]["input"][0]["image"].startswith("data:image/png;base64,")
            results = result["raw_response"]["results"]
            assert results[0] == {"index": 1, "kind": "image", "similarity": 0.6}
            assert results[1] == {"index": 0, "kind": "text", "content": "car", "similarity": 0.0}

            text_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "text-embedding-similarity-" + uuid.uuid4().hex[:8],
                    "type": "Embedding",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://text-embedding.test/v1",
                        "provider": "openai-compatible",
                        "spec": {"input": ["text"]},
                    },
                },
            )
            assert text_model.status_code == 201, text_model.text
            refused = await client.post(
                f"/api/v1/models/{text_model.json()['data']['id']}/debug",
                headers=headers,
                data={"input": "dog", "documents": "[]", "options": '{"similarity": true}'},
                files={"file": ("test.png", png, "image/png")},
            )
            assert "does not accept images" in refused.json()["data"]["error"]
            assert len(calls) == 3

    _run(case)


def test_image_vector_ingestion_and_retrieval(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []
    png = b"\x89PNG\r\n\x1a\nimage-vector"
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode()

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else {}
        calls.append(payload)
        if url.endswith("/chat/completions"):
            return 200, {}, json.dumps({
                "choices": [{"message": {"content": "sales"}, "finish_reason": "stop"}]
            }).encode()
        value = payload.get("input")
        vector = [0.6, 0.8, 0.0] if isinstance(value, list) and value and isinstance(value[0], dict) else [1.0, 0.0, 0.0]
        return 200, {}, json.dumps({"data": [{"embedding": vector}]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]
            created_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "jina-embeddings-v5-omni-small",
                    "display_name": "image vectors " + suffix,
                    "type": "Embedding",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://jina-images.test/v1",
                        "provider": "jina",
                        "spec": {"input": ["text", "image"]},
                    },
                },
            )
            assert created_model.status_code == 201, created_model.text
            model_id = created_model.json()["data"]["id"]
            created_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "Image vector KB " + suffix,
                    "embedding_model_id": model_id,
                    "image_processing_config": {"image_vector_enabled": True},
                },
            )
            assert created_kb.status_code == 201, created_kb.text
            kb_id = created_kb.json()["data"]["id"]
            created = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={
                    "title": "Chart document",
                    "content": f"A drawing is attached.\n\n![a bar chart]({data_uri})",
                },
            )
            assert created.status_code == 201, created.text
            knowledge_id = created.json()["data"]["id"]

            image_chunk = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image' AND deleted_at IS NULL",
                (knowledge_id,),
            )
            assert image_chunk is not None
            vector_chunk = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image_vector' AND deleted_at IS NULL",
                (knowledge_id,),
            )
            assert vector_chunk is not None
            assert vector_chunk["parent_chunk_id"] == image_chunk["parent_chunk_id"]
            assert vector_chunk["content"] == "a bar chart"
            assert json.loads(vector_chunk["embedding"]) == [0.6, 0.8, 0.0]
            assert json.loads(image_chunk["image_info"])[0]["start_pos"] >= 0

            found = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/hybrid-search",
                headers=headers,
                json={"query": "bicycle"},
            )
            assert found.status_code == 200, found.text
            results = found.json()["data"]
            assert results[0]["chunk_id"] == vector_chunk["id"]
            assert results[0]["chunk_type"] == "image_vector"
            assert results[0]["source_type"] == "image"
            assert results[0]["content"] == "a bar chart"
            assert any(isinstance(payload.get("input"), list) and payload["input"] and isinstance(payload["input"][0], dict) for payload in calls)

            upload_vlm = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "test-vlm-" + suffix,
                    "type": "VLLM",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://vlm-images.test/v1",
                        "provider": "openai-compatible",
                    },
                },
            )
            assert upload_vlm.status_code == 201, upload_vlm.text
            upload_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "Direct image vector KB " + suffix,
                    "embedding_model_id": model_id,
                    "image_processing_config": {"image_vector_enabled": True},
                    "vlm_config": {
                        "enabled": True,
                        "model_id": upload_vlm.json()["data"]["id"],
                    },
                },
            )
            assert upload_kb.status_code == 201, upload_kb.text
            upload_kb_id = upload_kb.json()["data"]["id"]

            uploaded_image = await client.post(
                f"/api/v1/knowledge-bases/{upload_kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("sales.png", png, "image/png")},
            )
            assert uploaded_image.status_code == 200, uploaded_image.text
            image_knowledge = db.fetch_one(
                "SELECT * FROM knowledges WHERE id=?", (uploaded_image.json()["data"]["id"],)
            )
            assert image_knowledge["parse_status"] == "completed"
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (image_knowledge["id"],),
            ) is None
            uploaded_vector = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image_vector'",
                (image_knowledge["id"],),
            )
            assert uploaded_vector is not None
            assert uploaded_vector["content"] == "sales"

            from PIL import Image
            from app.services import file_proxy

            image_buffer = io.BytesIO()
            Image.new("RGB", (12, 12), (10, 20, 30)).save(image_buffer, format="PNG")
            embedded_png = image_buffer.getvalue()
            uploaded_docx = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={
                    "file": (
                        "embedded.docx",
                        _docx_with_image("Embedded figure", "Architecture chart", embedded_png),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert uploaded_docx.status_code == 200, uploaded_docx.text
            docx_knowledge = db.fetch_one(
                "SELECT * FROM knowledges WHERE id=?", (uploaded_docx.json()["data"]["id"],)
            )
            assert docx_knowledge["parse_status"] == "completed"
            docx_image = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image'",
                (docx_knowledge["id"],),
            )
            assert docx_image is not None
            image_info = json.loads(docx_image["image_info"])[0]
            assert image_info["caption"] == "Architecture chart"
            assert image_info["extracted"] is True
            assert image_info["url"].startswith(f"local://1/{docx_knowledge['id']}/embedded/")
            saved_path, _name = file_proxy.resolve_file(image_info["url"], 1)
            assert saved_path.read_bytes() == embedded_png
            docx_vector = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image_vector'",
                (docx_knowledge["id"],),
            )
            assert docx_vector is not None
            assert docx_vector["content"] == "Architecture chart"

            reparsed = await client.post(
                f"/api/v1/knowledge/{docx_knowledge['id']}/reparse", headers=headers
            )
            assert reparsed.status_code == 200, reparsed.text
            reparsed_image = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image'",
                (docx_knowledge["id"],),
            )
            reparsed_info = json.loads(reparsed_image["image_info"])[0]
            reparsed_path, _name = file_proxy.resolve_file(reparsed_info["url"], 1)
            assert reparsed_path.read_bytes() == embedded_png
            assert not saved_path.exists()

    _run(case)


def test_image_vector_indexing_respects_switch_and_validation(monkeypatch) -> None:
    from app.services import image_vectors, model_runtime

    png = b"\x89PNG\r\n\x1a\nimage-vector"
    pdf = b"%PDF-1.7"
    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else {}
        calls.append(payload)
        return 200, {}, json.dumps({"data": [{"embedding": [0.6, 0.8, 0.0]}]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]
            created_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "jina-embeddings-v5-omni-small",
                    "display_name": "image vector limits " + suffix,
                    "type": "Embedding",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://jina-limits.test/v1",
                        "provider": "jina",
                        "spec": {"input": ["text", "image"], "dimension": 1024},
                    },
                },
            )
            assert created_model.status_code == 201, created_model.text
            model_id = created_model.json()["data"]["id"]
            created_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Image limits KB " + suffix, "embedding_model_id": model_id},
            )
            assert created_kb.status_code == 201, created_kb.text
            kb_id = created_kb.json()["data"]["id"]
            created = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={
                    "title": "Disabled image vectors",
                    "content": "![chart](data:image/png;base64," + base64.b64encode(png).decode() + ")",
                },
            )
            assert created.status_code == 201, created.text
            knowledge = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (created.json()["data"]["id"],))
            kb = db.fetch_one("SELECT * FROM knowledge_bases WHERE id=?", (kb_id,))
            disabled = image_vectors.index_knowledge_images(1, kb, knowledge)
            assert disabled["enabled"] is False
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND chunk_type='image_vector'",
                (knowledge["id"],),
            ) is None

            kb["image_processing_config"] = json.dumps({"image_vector_enabled": True})
            mismatch = image_vectors.index_knowledge_images(1, kb, knowledge)
            assert mismatch["failed"] == 1
            assert "1024" in mismatch["items"][0]["status"]
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND chunk_type='image_vector'",
                (knowledge["id"],),
            ) is None

            kb["image_processing_config"] = json.dumps({"image_vector_enabled": True})
            model_row = db.fetch_one("SELECT * FROM models WHERE id=?", (model_id,))
            parameters = json.loads(model_row["parameters"])
            parameters["spec"].pop("dimension", None)
            with db.transaction() as conn:
                conn.execute("UPDATE models SET parameters=? WHERE id=?", (json.dumps(parameters), model_id))
                conn.execute(
                    "UPDATE chunks SET image_info=? WHERE knowledge_id=? AND chunk_type='image'",
                    (json.dumps([{"url": "data:application/pdf;base64," + base64.b64encode(pdf).decode(), "caption": "not an image"}]), knowledge["id"]),
                )
            invalid = image_vectors.index_knowledge_images(1, kb, knowledge)
            assert invalid["failed"] == 1
            assert "not an image (application/pdf)" in invalid["items"][0]["status"]

            knowledge["metadata"] = json.dumps({"image_source_type": "scanned_pdf"})
            scanned = image_vectors.index_knowledge_images(1, kb, knowledge)
            assert scanned["skipped"] == 1
            assert scanned["items"][0]["status"] == "skipped: scanned page"

            knowledge["metadata"] = "{}"
            parameters["provider"] = "openai-compatible"
            parameters["spec"] = {"input": ["text"]}
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE models SET name=?, parameters=? WHERE id=?",
                    ("text-embedding-3-small", json.dumps(parameters), model_id),
                )
            text_only = image_vectors.index_knowledge_images(1, kb, knowledge)
            assert text_only["skipped"] == 1
            assert "does not accept images" in text_only["items"][0]["status"]
            assert calls

    _run(case)


def test_image_multimodal_caption_ocr_and_failure_boundaries(monkeypatch) -> None:
    from app.services import model_runtime

    state = {
        "caption": "A bar chart showing quarterly revenue",
        "ocr": "Quarterly revenue 42",
        "fail": False,
    }
    chat_calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else {}
        if not url.endswith("/chat/completions"):
            raise AssertionError(f"unexpected model call: {url}")
        chat_calls.append(payload)
        if state["fail"]:
            raise RuntimeError("vlm unavailable")
        prompt = payload["messages"][0]["content"][0]["text"]
        content = state["ocr"] if "OCR assistant" in prompt else state["caption"]
        return 200, {}, json.dumps({
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}]
        }).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        from PIL import Image

        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]
            created_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "test-vlm-" + suffix,
                    "display_name": "VLM " + suffix,
                    "type": "VLLM",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://vlm.test/v1",
                        "provider": "openai-compatible",
                    },
                },
            )
            assert created_model.status_code == 201, created_model.text
            vlm_id = created_model.json()["data"]["id"]

            no_vlm_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "No VLM images " + suffix,
                    "image_processing_config": {"enable_multimodal": True},
                },
            )
            assert no_vlm_kb.status_code == 201, no_vlm_kb.text
            no_vlm_kb_id = no_vlm_kb.json()["data"]["id"]

            image_buffer = io.BytesIO()
            Image.new("RGB", (12, 12), (10, 20, 30)).save(image_buffer, format="PNG")
            png = image_buffer.getvalue()
            calls_before = len(chat_calls)
            uploaded_without_vlm = await client.post(
                f"/api/v1/knowledge-bases/{no_vlm_kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("plain.png", png, "image/png")},
            )
            assert uploaded_without_vlm.status_code == 400, uploaded_without_vlm.text
            assert uploaded_without_vlm.json()["error"]["message"] == "上传图片文件需要设置VLM模型"
            assert len(chat_calls) == calls_before
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=?",
                (no_vlm_kb_id,),
            )["n"] == 0

            created_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "VLM images " + suffix,
                    "vlm_config": {
                        "enabled": True,
                        "model_id": vlm_id,
                        "description_language": "Chinese",
                        "custom_instructions": "Focus on labels.",
                    },
                },
            )
            assert created_kb.status_code == 201, created_kb.text
            kb_id = created_kb.json()["data"]["id"]

            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("sales.png", png, "image/png")},
            )
            assert uploaded.status_code == 200, uploaded.text
            knowledge_id = uploaded.json()["data"]["id"]
            image_chunk = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image' AND deleted_at IS NULL",
                (knowledge_id,),
            )
            assert image_chunk is not None
            image_info = json.loads(image_chunk["image_info"])[0]
            assert image_info["caption"] == state["caption"]
            assert image_info["ocr_text"] == state["ocr"]

            caption_chunk = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image_caption' AND deleted_at IS NULL",
                (knowledge_id,),
            )
            ocr_chunk = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image_ocr' AND deleted_at IS NULL",
                (knowledge_id,),
            )
            assert caption_chunk["content"] == state["caption"]
            assert caption_chunk["parent_chunk_id"] == image_chunk["id"]
            assert ocr_chunk["content"] == state["ocr"]
            assert ocr_chunk["parent_chunk_id"] == image_chunk["id"]
            assert len(chat_calls) == calls_before + 2
            caption_request, ocr_request = chat_calls[-2:]
            assert "Chinese" in caption_request["messages"][0]["content"][0]["text"]
            assert "Focus on labels." in caption_request["messages"][0]["content"][0]["text"]
            assert "OCR assistant" in ocr_request["messages"][0]["content"][0]["text"]
            assert "Focus on labels." not in ocr_request["messages"][0]["content"][0]["text"]
            assert caption_request["messages"][0]["content"][1]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )

            uploaded_docx = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={
                    "file": (
                        "vlm.docx",
                        _docx_with_image("Embedded", "Architecture chart", png),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
            )
            assert uploaded_docx.status_code == 200, uploaded_docx.text
            docx_knowledge_id = uploaded_docx.json()["data"]["id"]
            docx_image = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image' AND deleted_at IS NULL",
                (docx_knowledge_id,),
            )
            assert json.loads(docx_image["image_info"])[0]["caption"] == state["caption"]
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND chunk_type='image_caption' AND deleted_at IS NULL",
                (docx_knowledge_id,),
            ) is not None

            state["caption"] = "A blank sign"
            state["ocr"] = "No text content."
            uploaded_blank = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("blank.png", png, "image/png")},
            )
            assert uploaded_blank.status_code == 200, uploaded_blank.text
            blank_id = uploaded_blank.json()["data"]["id"]
            blank_info = json.loads(db.fetch_one(
                "SELECT image_info FROM chunks WHERE knowledge_id=? AND chunk_type='image'",
                (blank_id,),
            )["image_info"])[0]
            assert blank_info["caption"] == "A blank sign"
            assert "ocr_text" not in blank_info
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND chunk_type='image_caption'",
                (blank_id,),
            ) is not None
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND chunk_type='image_ocr'",
                (blank_id,),
            ) is None

            state["fail"] = True
            uploaded_failed = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("failed.png", png, "image/png")},
            )
            assert uploaded_failed.status_code == 200, uploaded_failed.text
            failed_id = uploaded_failed.json()["data"]["id"]
            failed_knowledge = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (failed_id,))
            assert failed_knowledge["parse_status"] == "completed"
            failed_info = json.loads(db.fetch_one(
                "SELECT image_info FROM chunks WHERE knowledge_id=? AND chunk_type='image'",
                (failed_id,),
            )["image_info"])[0]
            assert failed_info["caption"] == "failed"
            assert db.fetch_one(
                "SELECT 1 FROM chunks WHERE knowledge_id=? AND chunk_type IN ('image_caption', 'image_ocr')",
                (failed_id,),
            ) is None

            state["fail"] = False
            state["caption"] = "A bar chart showing quarterly revenue"
            state["ocr"] = "Quarterly revenue 42"
            reparsed = await client.post(
                f"/api/v1/knowledge/{knowledge_id}/reparse", headers=headers
            )
            assert reparsed.status_code == 200, reparsed.text
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND chunk_type='image_caption' AND deleted_at IS NULL",
                (knowledge_id,),
            )["n"] == 1
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND chunk_type='image_ocr' AND deleted_at IS NULL",
                (knowledge_id,),
            )["n"] == 1

    _run(case)


def test_pdf_embedded_image_ingestion_and_reparse() -> None:
    async def case() -> None:
        from app.services import file_proxy

        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]
            created_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "PDF images " + suffix},
            )
            assert created_kb.status_code == 201, created_kb.text
            kb_id = created_kb.json()["data"]["id"]

            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("report.pdf", _pdf_with_image(), "application/pdf")},
            )
            assert uploaded.status_code == 200, uploaded.text
            knowledge_id = uploaded.json()["data"]["id"]
            knowledge = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
            assert knowledge["parse_status"] == "completed"
            image_chunk = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image' AND deleted_at IS NULL",
                (knowledge_id,),
            )
            assert image_chunk is not None
            image_info = json.loads(image_chunk["image_info"])[0]
            assert image_info["caption"] == "Page 1 image 1"
            assert image_info["extracted"] is True
            first_path, _name = file_proxy.resolve_file(image_info["url"], 1)
            assert first_path.read_bytes().startswith(b"\xff\xd8")

            reparsed = await client.post(
                f"/api/v1/knowledge/{knowledge_id}/reparse", headers=headers
            )
            assert reparsed.status_code == 200, reparsed.text
            reparsed_image = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='image' AND deleted_at IS NULL",
                (knowledge_id,),
            )
            reparsed_info = json.loads(reparsed_image["image_info"])[0]
            second_path, _name = file_proxy.resolve_file(reparsed_info["url"], 1)
            assert second_path.read_bytes().startswith(b"\xff\xd8")
            assert not first_path.exists()

    _run(case)


def test_audio_asr_ingestion_and_video_rejection(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[tuple[str, str]] = []
    state = {"fail": False}

    def fake_transcribe(runtime, data, filename, *, language=""):
        calls.append((filename, language))
        assert data.startswith(b"RIFF")
        if state["fail"]:
            raise ValueError("upstream returned invalid JSON")
        return {"text": "会议记录：确认交付时间。"}

    monkeypatch.setattr(model_runtime, "transcribe", fake_transcribe)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]

            disabled_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "ASR disabled " + suffix},
            )
            assert disabled_kb.status_code == 201, disabled_kb.text
            disabled = await client.post(
                f"/api/v1/knowledge-bases/{disabled_kb.json()['data']['id']}/knowledge/file",
                headers=headers,
                files={"file": ("meeting.wav", _wav(), "audio/wav")},
            )
            assert disabled.status_code == 400, disabled.text
            assert disabled.json()["error"]["message"] == "上传音频文件需要设置ASR语音识别模型"
            assert not calls
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=?",
                (disabled_kb.json()["data"]["id"],),
            )["n"] == 0

            model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "test-asr-" + suffix,
                    "type": "ASR",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://asr.test/v1",
                        "provider": "openai-compatible",
                    },
                },
            )
            assert model.status_code == 201, model.text
            asr_id = model.json()["data"]["id"]
            enabled_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "ASR enabled " + suffix,
                    "asr_config": {"enabled": True, "model_id": asr_id, "language": "zh"},
                },
            )
            assert enabled_kb.status_code == 201, enabled_kb.text
            kb_id = enabled_kb.json()["data"]["id"]
            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("meeting.wav", _wav(), "audio/wav")},
            )
            assert uploaded.status_code == 200, uploaded.text
            knowledge_id = uploaded.json()["data"]["id"]
            knowledge = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (knowledge_id,))
            assert knowledge["parse_status"] == "completed"
            text_chunk = db.fetch_one(
                "SELECT * FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (knowledge_id,),
            )
            assert text_chunk["content"] == "会议记录：确认交付时间。"
            assert calls == [("meeting.wav", "zh")]

            reparsed = await client.post(
                f"/api/v1/knowledge/{knowledge_id}/reparse", headers=headers
            )
            assert reparsed.status_code == 200, reparsed.text
            assert calls == [("meeting.wav", "zh"), ("meeting.wav", "zh")]
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (knowledge_id,),
            )["n"] == 1

            video = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
            )
            assert video.status_code == 400, video.text
            assert video.json()["error"]["message"] == "暂不支持上传视频文件"
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=? AND file_name='clip.mp4'",
                (kb_id,),
            )["n"] == 0

            state["fail"] = True
            failed = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("broken.wav", _wav(), "audio/wav")},
            )
            assert failed.status_code == 200, failed.text
            failed_data = failed.json()["data"]
            assert failed_data["parse_status"] == "failed"
            assert failed_data["error_message"].startswith("audio transcription failed:")
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=?",
                (failed_data["id"],),
            )["n"] == 0

    _run(case)


def test_asr_transcribe_wire_includes_language(monkeypatch) -> None:
    from app.services import model_runtime

    captured: dict = {}

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        captured.update({"method": method, "url": url, "body": body or b""})
        return 200, {}, json.dumps({"text": "transcribed"}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)
    result = model_runtime.transcribe(
        {"name": "whisper-1", "base_url": "https://asr.test/v1", "headers": {}},
        b"RIFF-audio",
        "meeting.wav",
        language="zh\nCN",
    )
    assert result == {"text": "transcribed"}
    assert captured["url"] == "https://asr.test/v1/audio/transcriptions"
    assert b'name="model"\r\n\r\nwhisper-1' in captured["body"]
    assert b'name="language"\r\n\r\nzh CN' in captured["body"]
    assert b'name="file"; filename="meeting.wav"' in captured["body"]


def test_knowledge_file_upload_admission_matches_go(monkeypatch) -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "Upload admission " + suffix},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            unsupported = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
            )
            assert unsupported.status_code == 400, unsupported.text
            assert unsupported.json()["error"]["message"] == "不支持的文件类型: exe"

            no_extension = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("README", b"text", "text/plain")},
            )
            assert no_extension.status_code == 400, no_extension.text
            assert no_extension.json()["error"]["message"] == "无法确定文件类型"

            empty_json = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("empty.json", b"", "application/json")},
            )
            assert empty_json.status_code == 400, empty_json.text
            assert empty_json.json()["error"]["message"] == "JSON 文件内容为空"

            invalid_json = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("invalid.json", b'{"a":', "application/json")},
            )
            assert invalid_json.status_code == 400, invalid_json.text
            assert invalid_json.json()["error"]["message"] == "JSON 文件内容无效，请检查格式"

            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={
                    "fileName": "docs/spec/design.md",
                    "metadata": json.dumps({"origin": "folder-upload"}),
                    "channel": "browser_extension",
                },
                files={"file": ("source.md", b"# Design\n\nUploaded from a folder.", "text/markdown")},
            )
            assert uploaded.status_code == 200, uploaded.text
            knowledge = uploaded.json()["data"]
            assert knowledge["title"] == "design.md"
            assert knowledge["file_name"] == "design.md"
            assert knowledge["folder_path"] == "docs/spec"
            assert knowledge["metadata"] == {"origin": "folder-upload"}
            assert knowledge["channel"] == "browser_extension"

            duplicate = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={"fileName": "docs/spec/design.md"},
                files={"file": ("source.md", b"# Design\n\nUploaded from a folder.", "text/markdown")},
            )
            assert duplicate.status_code == 409, duplicate.text
            duplicate_body = duplicate.json()
            assert duplicate_body["code"] == "duplicate_file"
            assert duplicate_body["data"]["id"] == knowledge["id"]
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM knowledges WHERE knowledge_base_id=?",
                (kb_id,),
            )["n"] == 1

            monkeypatch.setenv("MAX_FILE_SIZE_MB", "1")
            oversized = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("large.txt", b"x" * (1024 * 1024 + 1), "text/plain")},
            )
            assert oversized.status_code == 400, oversized.text
            assert oversized.json()["error"]["message"] == "文件大小不能超过1MB"

            monkeypatch.delenv("MAX_FILE_SIZE_MB")
            faq = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": "FAQ upload " + suffix, "type": "faq"},
            )
            assert faq.status_code == 201, faq.text
            faq_upload = await client.post(
                f"/api/v1/knowledge-bases/{faq.json()['data']['id']}/knowledge/file",
                headers=headers,
                files={"file": ("faq.txt", b"question", "text/plain")},
            )
            assert faq_upload.status_code == 400, faq_upload.text
            assert faq_upload.json()["error"]["message"] == "FAQ 知识库不支持文件上传，请使用 FAQ 导入功能"

    _run(case)


def test_knowledge_upload_process_overrides_match_go(monkeypatch) -> None:
    from PIL import Image
    from app.services import image_multimodal, model_runtime

    transcribe_calls: list[str] = []

    def fake_transcribe(_runtime, data, _filename, *, language=""):
        assert data.startswith(b"RIFF")
        transcribe_calls.append(language)
        return {"text": "process override audio"}

    monkeypatch.setattr(model_runtime, "transcribe", fake_transcribe)
    monkeypatch.setattr(
        image_multimodal,
        "process_knowledge_images",
        lambda *_args, **_kwargs: {"enabled": True},
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]

            vlm = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "override-vlm-" + suffix,
                    "type": "VLLM",
                    "source": "remote",
                    "parameters": {"base_url": "https://vlm.test/v1", "provider": "openai-compatible"},
                },
            )
            assert vlm.status_code == 201, vlm.text
            asr = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "override-asr-" + suffix,
                    "type": "ASR",
                    "source": "remote",
                    "parameters": {"base_url": "https://asr.test/v1", "provider": "openai-compatible"},
                },
            )
            assert asr.status_code == 201, asr.text

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "Process overrides " + suffix,
                    "chunking_config": {"chunk_size": 1000, "chunk_overlap": 0},
                },
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            invalid_object = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={"process_config": "[]"},
                files={"file": ("invalid.txt", b"text", "text/plain")},
            )
            assert invalid_object.status_code == 400, invalid_object.text
            assert invalid_object.json()["error"]["message"] == "Invalid process_config format"

            invalid_chunk_size = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={"process_config": json.dumps({"chunking_config": {"chunk_size": "40"}})},
                files={"file": ("invalid.txt", b"text", "text/plain")},
            )
            assert invalid_chunk_size.status_code == 400, invalid_chunk_size.text
            assert invalid_chunk_size.json()["error"]["message"] == "Invalid process_config format"

            overrides = {
                "chunking_config": {"chunk_size": 40, "chunk_overlap": 5},
                "enable_multimodel": False,
                "image_vector_enabled": True,
            }
            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={
                    "process_config": json.dumps(overrides),
                    "enable_multimodel": "true",
                },
                files={"file": ("chunked.txt", b"process override text " * 30, "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            knowledge = uploaded.json()["data"]
            assert knowledge["metadata"]["process_overrides"] == overrides
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (knowledge["id"],),
            )["n"] > 1

            reparsed = await client.post(
                f"/api/v1/knowledge/{knowledge['id']}/reparse", headers=headers
            )
            assert reparsed.status_code == 200, reparsed.text
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (knowledge["id"],),
            )["n"] > 1

            reparse_overrides = {
                "chunking_config": {"chunk_size": 1000, "chunk_overlap": 0},
            }
            reparsed_with_config = await client.post(
                f"/api/v1/knowledge/{knowledge['id']}/reparse",
                headers=headers,
                json={"process_config": reparse_overrides},
            )
            assert reparsed_with_config.status_code == 200, reparsed_with_config.text
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (knowledge["id"],),
            )["n"] == 1
            assert json.loads(db.fetch_one(
                "SELECT metadata FROM knowledges WHERE id=?", (knowledge["id"],)
            )["metadata"])["process_overrides"] == reparse_overrides

            form_override = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={"enable_multimodel": "true"},
                files={"file": ("form.txt", b"form fallback", "text/plain")},
            )
            assert form_override.status_code == 200, form_override.text
            assert form_override.json()["data"]["metadata"]["process_overrides"] == {
                "enable_multimodel": True,
            }

            image_buffer = io.BytesIO()
            Image.new("RGB", (12, 12), (10, 20, 30)).save(image_buffer, format="PNG")
            image_override = {
                "vlm_config": {
                    "enabled": True,
                    "model_id": vlm.json()["data"]["id"],
                    "description_language": "Chinese",
                },
            }
            image = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={"process_config": json.dumps(image_override)},
                files={"file": ("override.png", image_buffer.getvalue(), "image/png")},
            )
            assert image.status_code == 200, image.text
            assert image.json()["data"]["metadata"]["process_overrides"] == image_override

            audio_override = {
                "asr_config": {
                    "enabled": True,
                    "model_id": asr.json()["data"]["id"],
                    "language": "zh",
                },
            }
            audio = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                data={"process_config": json.dumps(audio_override)},
                files={"file": ("override.wav", _wav(), "audio/wav")},
            )
            assert audio.status_code == 200, audio.text
            assert audio.json()["data"]["metadata"]["process_overrides"] == audio_override
            audio_chunk = db.fetch_one(
                "SELECT content FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (audio.json()["data"]["id"],),
            )
            assert audio_chunk["content"] == "process override audio"
            assert transcribe_calls == ["zh"]

            rejected_audio_reparse = await client.post(
                f"/api/v1/knowledge/{audio.json()['data']['id']}/reparse",
                headers=headers,
                json={"process_config": {"asr_config": {"enabled": False}}},
            )
            assert rejected_audio_reparse.status_code == 400, rejected_audio_reparse.text
            assert rejected_audio_reparse.json()["error"]["message"] == "上传音频文件需要设置ASR语音识别模型"
            assert db.fetch_one(
                "SELECT COUNT(*) AS n FROM chunks WHERE knowledge_id=? AND chunk_type='text'",
                (audio.json()["data"]["id"],),
            )["n"] == 1
            assert json.loads(db.fetch_one(
                "SELECT metadata FROM knowledges WHERE id=?",
                (audio.json()["data"]["id"],),
            )["metadata"])["process_overrides"] == audio_override

    _run(case)


def test_model_debug_image_rerank_wire_formats_and_validation(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []
    png = b"\x89PNG\r\n\x1a\n" + b"rerank-image"
    data_uri = "data:image/png;base64," + base64.b64encode(png).decode()

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else None
        calls.append({"method": method, "url": url, "headers": dict(headers or {}), "body": payload})
        if url.endswith("/rerank"):
            return 200, {}, json.dumps({"results": [{"index": 0, "relevance_score": 0.9}]}).encode()
        return 200, {}, json.dumps({"rankings": [{"index": 0, "logit": 0.5}]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]
            cases = [
                {
                    "name": "jina-reranker-m0",
                    "provider": "jina",
                    "base_url": "https://jina.test/v1",
                    "spec": {"input": ["text", "image"]},
                    "url": "https://jina.test/v1/rerank",
                    "body": {
                        "model": "jina-reranker-m0",
                        "query": "which chart",
                        "return_documents": False,
                        "documents": [{"image": data_uri}],
                    },
                },
                {
                    "name": "nvidia/llama-nemotron-rerank-vl-1b-v2",
                    "provider": "nvidia",
                    "base_url": "https://nvidia.test/v1/retrieval/nvidia/reranking",
                    "spec": {"input": ["text", "image"]},
                    "url": "https://nvidia.test/v1/retrieval/nvidia/reranking",
                    "body": {
                        "model": "nvidia/llama-nemotron-rerank-vl-1b-v2",
                        "query": {"text": "which chart"},
                        "truncate": "END",
                        "passages": [{"image": data_uri}],
                    },
                },
            ]
            model_ids: dict[str, str] = {}
            for item in cases:
                created = await client.post(
                    "/api/v1/models",
                    headers=headers,
                    json={
                        "name": item["name"],
                        "display_name": item["name"] + " " + suffix,
                        "type": "Rerank",
                        "source": "remote",
                        "parameters": {
                            "base_url": item["base_url"],
                            "api_key": "rerank-key",
                            "provider": item["provider"],
                            "spec": item["spec"],
                        },
                    },
                )
                assert created.status_code == 201, created.text
                model = created.json()["data"]
                assert model["capabilities"]["input"] == ["text", "image"]
                model_ids[item["name"]] = model["id"]
                before = len(calls)
                debug = await client.post(
                    f"/api/v1/models/{model['id']}/debug",
                    headers=headers,
                    data={"input": "which chart", "documents": "[]", "options": "{}"},
                    files={"file": ("test.png", png, "image/png")},
                )
                assert debug.status_code == 200, debug.text
                result = debug.json()["data"]
                assert result["ok"] is True, result
                assert result["observations"]["result_count"] == 1
                assert len(calls) == before + 1
                assert calls[-1]["url"] == item["url"]
                assert calls[-1]["body"] == item["body"]
                if item["provider"] == "nvidia":
                    assert abs(result["raw_response"][0]["relevance_score"] - 0.622459) < 0.0001
                else:
                    assert result["raw_response"][0]["relevance_score"] == 0.9

            jina_id = model_ids["jina-reranker-m0"]
            mixed = await client.post(
                f"/api/v1/models/{jina_id}/debug",
                headers=headers,
                data={"input": "which chart", "documents": '["text"]', "options": "{}"},
                files={"file": ("test.png", png, "image/png")},
            )
            assert "either text documents or an image" in mixed.json()["data"]["error"]
            invalid = await client.post(
                f"/api/v1/models/{jina_id}/debug",
                headers=headers,
                data={"input": "which chart", "documents": "[]", "options": "{}"},
                files={"file": ("test.png", b"not an image", "image/png")},
            )
            assert "not an image" in invalid.json()["data"]["error"]
            missing_query = await client.post(
                f"/api/v1/models/{jina_id}/debug",
                headers=headers,
                data={"input": "", "documents": "[]", "options": "{}"},
                files={"file": ("test.png", png, "image/png")},
            )
            assert "query and documents cannot be empty" in missing_query.json()["data"]["error"]

            text_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "text-rerank-" + suffix,
                    "type": "Rerank",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://text-rerank.test/v1",
                        "provider": "openai-compatible",
                        "spec": {"input": ["text"]},
                    },
                },
            )
            assert text_model.status_code == 201, text_model.text
            text_model_data = text_model.json()["data"]
            assert text_model_data["capabilities"]["input"] == ["text"]
            before = len(calls)
            unsupported = await client.post(
                f"/api/v1/models/{text_model_data['id']}/debug",
                headers=headers,
                data={"input": "which chart", "documents": "[]", "options": "{}"},
                files={"file": ("test.png", png, "image/png")},
            )
            assert unsupported.json()["data"]["ok"] is False
            assert "does not accept images" in unsupported.json()["data"]["error"]
            assert len(calls) == before

    _run(case)


def test_tei_and_pinecone_rerank_wire_formats(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        payload = json.loads(body.decode()) if body else {}
        calls.append({"url": url, "headers": dict(headers or {}), "body": payload})
        if "texts" in payload:
            batch = payload["texts"]
            results = [{"index": index, "score": float(index)} for index in reversed(range(len(batch)))]
            return 200, {}, json.dumps(results).encode()
        batch = payload["documents"]
        results = [{"index": index, "score": float(index)} for index in reversed(range(len(batch)))]
        return 200, {}, json.dumps({"data": results}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)

    tei_runtime = {
        "name": "BAAI/bge-reranker-large",
        "provider": "huggingface_tei",
        "base_url": "https://tei.test",
        "headers": {"Authorization": "Bearer tei-key"},
        "compat": {
            "extra_body": {
                "query": "override",
                "texts": [],
                "raw_scores": True,
                "truncate": False,
                "truncation_direction": "Left",
            }
        },
    }
    tei_documents = [f"doc-{index}" for index in range(33)]
    tei_results = model_runtime.rerank(tei_runtime, "query", tei_documents)
    assert len(calls) == 2
    assert calls[0]["url"] == "https://tei.test/rerank"
    assert calls[0]["headers"]["Authorization"] == "Bearer tei-key"
    assert calls[0]["body"] == {
        "query": "query",
        "texts": tei_documents[:32],
        "raw_scores": False,
        "truncate": False,
        "truncation_direction": "Left",
    }
    assert calls[1]["body"]["texts"] == tei_documents[32:]
    assert {item["index"] for item in tei_results} == set(range(33))

    calls.clear()
    pinecone_runtime = {
        "name": "bge-reranker-v2-m3",
        "provider": "pinecone",
        "base_url": "https://api.pinecone.test",
        "headers": {"api-key": "pinecone-key"},
        "compat": {
            "max_documents": 100,
            "truncate": "END",
            "extra_body": {
                "model": "override",
                "query": "override",
                "top_n": 1,
                "parameters": {"truncate": "NONE"},
            },
        },
    }
    pinecone_documents = [f"doc-{index}" for index in range(101)]
    pinecone_results = model_runtime.rerank(pinecone_runtime, "query", pinecone_documents)
    assert len(calls) == 2
    assert calls[0]["url"] == "https://api.pinecone.test/rerank"
    assert calls[0]["headers"]["X-Pinecone-Api-Version"] == "2026-07"
    assert calls[0]["headers"]["api-key"] == "pinecone-key"
    assert calls[0]["body"] == {
        "model": "bge-reranker-v2-m3",
        "query": "query",
        "documents": [{"text": document} for document in pinecone_documents[:100]],
        "top_n": 100,
        "rank_fields": ["text"],
        "return_documents": False,
        "parameters": {"truncate": "END"},
    }
    assert calls[1]["body"]["documents"] == [{"text": pinecone_documents[100]}]
    assert calls[1]["body"]["top_n"] == 1
    assert {item["index"] for item in pinecone_results} == set(range(101))


def test_openai_tool_call_history_emits_empty_content(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        calls.append(json.loads(body.decode()))
        return 200, {}, json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}]
        }).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)
    assistant = {
        "role": "assistant",
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }],
    }
    message = {"role": "assistant", "content": "reasoning", "tool_calls": assistant["tool_calls"]}
    plain = {"role": "assistant"}

    model_runtime.chat_completion(
        {"name": "qwen-local", "base_url": "https://llm.test/v1"},
        [assistant, message, plain, {"role": "tool", "tool_call_id": "call-1", "content": "result"}],
    )

    wire = calls[0]["messages"]
    assert wire[0]["content"] == ""
    assert "content" not in assistant
    assert wire[1]["content"] == "reasoning"
    assert "content" not in wire[2]
    assert wire[3]["content"] == "result"


def test_update_knowledge_base_vlm_config_uses_managed_model_only() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            suffix = uuid.uuid4().hex[:8]

            chat_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "chat-" + suffix,
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {"base_url": "https://chat.test/v1"},
                },
            )
            assert chat_model.status_code == 201, chat_model.text
            vlm_model = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "vlm-" + suffix,
                    "type": "VLLM",
                    "source": "remote",
                    "parameters": {"base_url": "https://vlm.test/v1"},
                },
            )
            assert vlm_model.status_code == 201, vlm_model.text
            vlm_id = vlm_model.json()["data"]["id"]

            created = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "vlm-update-" + suffix,
                    "vlm_config": {
                        "enabled": True,
                        "model_name": "stored",
                        "base_url": "https://stored.test/v1",
                        "api_key": "stored-key",
                        "interface_type": "openai",
                    },
                },
            )
            assert created.status_code == 201, created.text
            kb_id = created.json()["data"]["id"]

            updated = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=headers,
                json={
                    "vlm_config": {
                        "enabled": True,
                        "model_id": vlm_id,
                        "description_language": "Chinese",
                        "custom_instructions": "Keep chart labels.",
                        "model_name": "evil",
                        "base_url": "http://169.254.169.254",
                        "api_key": "evil-key",
                        "interface_type": "ollama",
                    }
                },
            )
            assert updated.status_code == 200, updated.text
            config = updated.json()["data"]["vlm_config"]
            assert config["enabled"] is True
            assert config["model_id"] == vlm_id
            assert config["description_language"] == "Chinese"
            assert config["custom_instructions"] == "Keep chart labels."
            assert config["model_name"] == ""
            assert config["base_url"] == ""
            assert config["api_key"] == ""
            assert config["interface_type"] == ""
            stored = json.loads(db.fetch_one(
                "SELECT vlm_config FROM knowledge_bases WHERE id=?", (kb_id,)
            )["vlm_config"])
            assert "evil" not in json.dumps(stored)

            missing = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=headers,
                json={"vlm_config": {"enabled": True, "model_id": "missing-vlm"}},
            )
            assert missing.status_code == 400, missing.text
            assert missing.json()["error"]["message"] == "vlm_config.model_id: model not found"

            wrong_type = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=headers,
                json={"vlm_config": {"enabled": True, "model_id": chat_model.json()["data"]["id"]}},
            )
            assert wrong_type.status_code == 400, wrong_type.text
            assert "want VLLM" in wrong_type.json()["error"]["message"]

            no_model = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=headers,
                json={"vlm_config": {"enabled": True}},
            )
            assert no_model.status_code == 400, no_model.text
            assert no_model.json()["error"]["message"] == "vlm_config.model_id is required when enabled"

            too_long = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=headers,
                json={
                    "vlm_config": {
                        "enabled": True,
                        "model_id": vlm_id,
                        "custom_instructions": "字" * 4001,
                    }
                },
            )
            assert too_long.status_code == 400, too_long.text
            unchanged = json.loads(db.fetch_one(
                "SELECT vlm_config FROM knowledge_bases WHERE id=?", (kb_id,)
            )["vlm_config"])
            assert unchanged["custom_instructions"] == "Keep chart labels."

            disabled = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}",
                headers=headers,
                json={"vlm_config": {"enabled": False}},
            )
            assert disabled.status_code == 200, disabled.text
            disabled_config = disabled.json()["data"]["vlm_config"]
            assert disabled_config["enabled"] is False
            assert disabled_config["model_id"] == ""
            assert disabled_config["model_name"] == ""
            assert disabled_config["base_url"] == ""

            legacy = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": "vlm-legacy-" + suffix,
                    "vlm_config": {
                        "enabled": False,
                        "model_name": "stored",
                        "base_url": "https://stored.test/v1",
                        "api_key": "stored-key",
                        "interface_type": "openai",
                    },
                },
            )
            assert legacy.status_code == 201, legacy.text
            legacy_id = legacy.json()["data"]["id"]

            kept = await client.put(
                f"/api/v1/knowledge-bases/{legacy_id}",
                headers=headers,
                json={"vlm_config": {"enabled": True}},
            )
            assert kept.status_code == 200, kept.text
            assert kept.json()["data"]["vlm_config"]["base_url"] == "https://stored.test/v1"

            echoed = await client.put(
                f"/api/v1/knowledge-bases/{legacy_id}",
                headers=headers,
                json={
                    "vlm_config": {
                        "enabled": False,
                        "model_name": "stored",
                        "base_url": "https://stored.test/v1",
                    }
                },
            )
            assert echoed.status_code == 200, echoed.text
            assert echoed.json()["data"]["vlm_config"]["base_url"] == "https://stored.test/v1"

            cleared = await client.put(
                f"/api/v1/knowledge-bases/{legacy_id}",
                headers=headers,
                json={"vlm_config": {"enabled": False}},
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["data"]["vlm_config"]["base_url"] == ""

    _run(case)


def test_context_images_deduplicates_and_limits_image_vector_hits(monkeypatch) -> None:
    from app.services import retrieval

    reads: list[str] = []

    def fake_read(url, tenant_id):
        reads.append(url)
        return url.encode()

    monkeypatch.setattr(retrieval.image_vectors, "_read_image", fake_read)
    monkeypatch.setattr(
        retrieval.image_prep,
        "prepare",
        lambda data, **_kwargs: (data, "image/png"),
    )
    rows = [
        {"chunk_type": "image_vector", "knowledge_base_id": "kb-1", "image_info": json.dumps([{"url": "a"}])},
        {"chunk_type": "text", "knowledge_base_id": "kb-1", "image_info": json.dumps([{"url": "ignored"}])},
        {"chunk_type": "image_vector", "knowledge_base_id": "kb-1", "image_info": json.dumps([{"url": "a"}])},
        {"chunk_type": "image_vector", "knowledge_base_id": "kb-1", "image_info": json.dumps([{"url": "b"}])},
        {"chunk_type": "image_vector", "knowledge_base_id": "kb-2", "image_info": json.dumps([{"url": "b"}])},
        {"chunk_type": "image_vector", "knowledge_base_id": "kb-1", "image_info": json.dumps([{"url": "c"}])},
        {"chunk_type": "image_vector", "knowledge_base_id": "kb-1", "image_info": json.dumps([{"url": "d"}])},
    ]

    images = retrieval.context_images(rows, 7)

    assert reads == ["a", "b", "b"]
    assert [base64.b64decode(image.split(",", 1)[1]).decode() for image in images] == ["a", "b", "b"]
    assert all(image.startswith("data:image/png;base64,") for image in images)


def test_chat_attaches_image_vector_hits_for_vision_model(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[list[dict]] = []

    def fake_chat_completion(runtime, messages, **kwargs):
        calls.append(json.loads(json.dumps(messages)))
        return {"choices": [{"message": {"role": "assistant", "content": "chart answer"}}]}

    def fake_retrieve(*_args, **_kwargs):
        return [{
            "id": "image-hit",
            "knowledge_id": "knowledge-1",
            "knowledge_base_id": "kb-1",
            "title": "Chart",
            "file_name": "chart.png",
            "content": "a quarterly bar chart",
            "chunk_type": "image_vector",
            "chunk_index": 0,
            "score": 0.8,
            "start_at": 0,
            "end_at": 0,
        }]

    monkeypatch.setattr(chat_router, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_router, "context_images", lambda _rows, _tenant_id: ["data:image/png;base64,cmV0cmlldmVk"])
    monkeypatch.setattr(model_runtime, "chat_completion", fake_chat_completion)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "vision-chat",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://vision.test/v1",
                        "supports_vision": True,
                    },
                },
            )
            assert created.status_code == 201, created.text
            model_id = created.json()["data"]["id"]
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "vision", "agent_config": {"model_id": model_id}},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            response = await client.post(
                f"/api/v1/knowledge-chat/{session_id}",
                headers=headers,
                json={"query": "which quarter peaked"},
            )
            assert response.status_code == 200, response.text

    _run(case)
    last_content = calls[-1][-1]["content"]
    assert last_content[0]["type"] == "text"
    assert "retrieved_images" in last_content[0]["text"]
    assert last_content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,cmV0cmlldmVk"},
    }


def test_chat_uses_model_selected_in_session_config(monkeypatch) -> None:
    from app.services import llm as llm_service
    from app.services import model_runtime

    seen: list[dict] = []

    def fake_completion(runtime, messages, **kwargs):
        seen.append({"runtime": runtime, "messages": messages, "kwargs": kwargs})
        return {
            "choices": [{"message": {"role": "assistant", "content": "数据库模型回答"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        }

    monkeypatch.setattr(model_runtime, "chat_completion", fake_completion)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "session-model",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://session-model.test/v1",
                        "api_key": "session-key",
                        "provider": "openai-compatible",
                    },
                },
            )
            assert created.status_code == 201, created.text
            model_id = created.json()["data"]["id"]

            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={"title": "新会话", "agent_config": {"model_id": model_id}},
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            response = await client.post(
                f"/api/v1/knowledge-chat/{session_id}",
                headers=headers,
                json={"query": "介绍知识库"},
            )
            assert response.status_code == 200, response.text
            events = _events(response)
            complete = next(event for event in events if event.get("response_type") == "complete")
            assert complete["data"]["final_content"] == "数据库模型回答"
            assert complete["data"]["total_steps"] == 1
            assert complete["data"]["total_duration_ms"] >= 0
            assert complete["data"]["usage"] == {
                "prompt_tokens": 12,
                "completion_tokens": 5,
                "total_tokens": 17,
            }
            assert complete["usage"] == complete["data"]["usage"]
            title = next(event for event in events if event.get("response_type") == "session_title")
            assert title["id"]
            assert title["content"] == "介绍知识库"
            assert title["done"] is True
            assert title["data"] == {"session_id": session_id, "title": "介绍知识库"}
            assert seen[-1]["runtime"]["id"] == model_id
            assert seen[-1]["runtime"]["base_url"] == "https://session-model.test/v1"

    _run(case)


def test_chat_accepts_upload_only_turns_and_replays_upload_question(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[list[dict]] = []

    def fake_completion(runtime, messages, **kwargs):
        calls.append(json.loads(json.dumps(messages)))
        return {"choices": [{"message": {"role": "assistant", "content": "upload answer"}}]}

    monkeypatch.setattr(model_runtime, "chat_completion", fake_completion)
    monkeypatch.setattr(chat_router, "retrieve", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        chat_router.query_understanding,
        "understand_query",
        lambda *_args, **_kwargs: SimpleNamespace(intent="", rewrite_query=""),
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "upload-only-model",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {
                        "base_url": "https://upload-only.test/v1",
                        "supports_vision": True,
                    },
                },
            )
            assert created.status_code == 201, created.text
            model_id = created.json()["data"]["id"]
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "title": "upload only",
                    "agent_config": {"model_id": model_id, "enable_rewrite": False},
                },
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]
            image = "data:image/png;base64,aGVsbG8="

            response = await client.post(
                f"/api/v1/knowledge-chat/{session_id}",
                headers=headers,
                json={"query": "   ", "images": [{"data": image}]},
            )
            assert response.status_code == 200, response.text
            assert any(event.get("response_type") == "complete" for event in _events(response))
            first_content = calls[-1][-1]["content"]
            assert "请根据我上传的内容回答。" in first_content[0]["text"]
            assert first_content[1]["image_url"]["url"] == image

            loaded = await client.get(f"/api/v1/messages/{session_id}/load", headers=headers)
            assert loaded.status_code == 200, loaded.text
            user_message = next(item for item in loaded.json()["data"] if item["role"] == "user")
            assert user_message["content"] == ""
            assert user_message["images"] == [{"data": image}]

            follow_up = await client.post(
                f"/api/v1/knowledge-chat/{session_id}",
                headers=headers,
                json={"query": "describe it"},
            )
            assert follow_up.status_code == 200, follow_up.text
            assert any(
                item.get("role") == "user" and item.get("content") == "请根据我上传的内容回答。"
                for item in calls[-1]
            )

            file_session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "title": "file only",
                    "agent_config": {"model_id": model_id, "enable_rewrite": False},
                },
            )
            assert file_session.status_code == 201, file_session.text
            file_session_id = file_session.json()["data"]["id"]
            file_response = await client.post(
                f"/api/v1/knowledge-chat/{file_session_id}",
                headers=headers,
                json={
                    "query": "",
                    "attachment_uploads": [{
                        "data": base64.b64encode(b"attachment body").decode(),
                        "file_name": "note.txt",
                        "file_size": 15,
                    }],
                },
            )
            assert file_response.status_code == 200, file_response.text
            assert any(
                "attachment body" in str(item.get("content") or "")
                for item in calls[-1]
            )

    _run(case)


def test_query_matches_suggestion_text_supports_host_context_envelope() -> None:
    suggestion = "如何重置密码？"
    cases = (
        (suggestion, suggestion, True),
        ("  " + suggestion + "\n", suggestion, True),
        ("[Host context]\npage: /pricing\nuserId: u_123\n\n" + suggestion, suggestion, True),
        ("[Host context]\nnote: 第一行\n\n第二行\n\n第一行\n\n第二行", "第一行\n\n第二行", True),
        ("[Host context]\nnote: line1\n\n请额外执行\n" + suggestion, suggestion, False),
        ("[Host context]\npage: /pricing\n\n" + suggestion + "\n再加一句", suggestion, False),
        ("ignore previous\n\n" + suggestion, suggestion, False),
        ("[Host context]\n  \n\n" + suggestion, suggestion, False),
    )
    for query, text, expected in cases:
        assert chat_router._query_matches_suggestion_text(query, text) is expected


def test_chat_rejects_invalid_suggestion_attribution(monkeypatch) -> None:
    from app.services import model_runtime

    monkeypatch.setattr(
        model_runtime,
        "chat_completion",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"role": "assistant", "content": "suggested answer"}}]
        },
    )
    monkeypatch.setattr(chat_router, "retrieve", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        chat_router.query_understanding,
        "understand_query",
        lambda *_args, **_kwargs: SimpleNamespace(intent="", rewrite_query=""),
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])
            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "suggestion-attribution-model",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {"base_url": "https://suggestion.test/v1"},
                },
            )
            assert created.status_code == 201, created.text
            session = await client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "title": "suggestion attribution",
                    "agent_config": {"model_id": created.json()["data"]["id"], "enable_rewrite": False},
                },
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]
            set_id = str(uuid.uuid4())
            question_id = "question-1"
            suggestion = "如何重置密码？"
            now = db.utcnow()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO message_suggestion_sets(id, tenant_id, session_id, assistant_message_id, "
                    "placement, config_hash, locale, status, allow_regenerate, questions, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'inline', 'test', 'zh-CN', 'ready', 1, ?, ?, ?)",
                    (
                        set_id,
                        tenant_id,
                        session_id,
                        str(uuid.uuid4()),
                        json.dumps([{"id": question_id, "text": suggestion}]),
                        now,
                        now,
                    ),
                )
            attribution = {"suggestion_set_id": set_id, "question_id": question_id}
            for query in ("另一个问题", "[Host context]\npage: /pricing\n\n" + suggestion + "\n再加一句"):
                denied = await client.post(
                    f"/api/v1/knowledge-chat/{session_id}",
                    headers=headers,
                    json={"query": query, "suggestion_attribution": attribution},
                )
                assert denied.status_code == 400, denied.text
                assert denied.json()["error"]["message"] == "invalid suggestion attribution"

            accepted = await client.post(
                f"/api/v1/knowledge-chat/{session_id}",
                headers=headers,
                json={
                    "query": "[Host context]\npage: /pricing\nnote: 第一行\n\n第二行\n\n" + suggestion,
                    "suggestion_attribution": attribution,
                },
            )
            assert accepted.status_code == 200, accepted.text
            assert any(event.get("response_type") == "complete" for event in _events(accepted))

    _run(case)


def test_storage_and_vector_store_connectivity_roundtrip(monkeypatch) -> None:
    from app.services import connectivity

    storage_calls: list[tuple[str, dict]] = []
    vector_calls: list[tuple[str, dict]] = []
    ssrf_calls: list[str] = []

    def fake_storage(provider, config_data):
        storage_calls.append((provider, dict(config_data)))
        return {"ok": True, "provider": provider}

    def fake_vector_store(engine_type, connection):
        vector_calls.append((engine_type, dict(connection)))
        return {"ok": True, "engine_type": engine_type, "version": "8.12.0"}

    monkeypatch.setattr(connectivity, "test_storage", fake_storage)
    monkeypatch.setattr(connectivity, "test_vector_store", fake_vector_store)
    monkeypatch.setattr(
        infra_router.url_guard,
        "validate_url_for_ssrf",
        lambda url: ssrf_calls.append(url),
    )

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            types = await client.get("/api/v1/storage-backends/types", headers=headers)
            assert types.status_code == 200, types.text
            assert types.json()["data"][:2] == ["local", "minio"]

            raw_test = await client.post(
                "/api/v1/storage-backends/test",
                headers=headers,
                json={
                    "provider": "s3",
                    "config": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key_id": "RAW-ACCESS",
                        "secret_access_key": "RAW-SECRET",
                    },
                },
            )
            assert raw_test.status_code == 200, raw_test.text
            assert raw_test.json() == {"success": True}
            assert storage_calls[-1][0] == "s3"
            assert storage_calls[-1][1]["secret_access_key"] == "RAW-SECRET"

            backend = await client.post(
                "/api/v1/storage-backends",
                headers=headers,
                json={
                    "name": f"storage-{suffix}",
                    "provider": "s3",
                    "config": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key_id": "STORED-ACCESS",
                        "secret_access_key": "STORED-SECRET",
                    },
                },
            )
            assert backend.status_code == 201, backend.text
            backend_data = backend.json()["data"]
            backend_id = backend_data["id"]
            assert backend_data["config"]["access_key_id"] == "***"
            assert backend_data["config"]["secret_access_key"] == "***"

            listed = await client.get("/api/v1/storage-backends", headers=headers)
            assert listed.status_code == 200, listed.text
            assert "default_storage_backend_id" in listed.json()
            assert "STORED-SECRET" not in listed.text

            by_id_test = await client.post(f"/api/v1/storage-backends/{backend_id}/test", headers=headers)
            assert by_id_test.status_code == 200, by_id_test.text
            assert by_id_test.json() == {"success": True}
            assert storage_calls[-1][1]["secret_access_key"] == "STORED-SECRET"

            updated = await client.put(
                f"/api/v1/storage-backends/{backend_id}",
                headers=headers,
                json={"config": {"access_key_id": "***", "secret_access_key": "***", "bucket_name": "docs-v2"}},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["config"]["bucket_name"] == "docs-v2"
            assert updated.json()["data"]["config"]["secret_access_key"] == "***"

            await client.post(f"/api/v1/storage-backends/{backend_id}/test", headers=headers)
            assert storage_calls[-1][1]["access_key_id"] == "STORED-ACCESS"
            assert storage_calls[-1][1]["secret_access_key"] == "STORED-SECRET"

            vector_store = await client.post(
                "/api/v1/vector-stores",
                headers=headers,
                json={
                    "name": f"vector-{suffix}",
                    "engine_type": "qdrant",
                    "connection_config": {
                        "host": "qdrant.example.test",
                        "password": "VECTOR-PASSWORD",
                        "api_key": "VECTOR-KEY",
                    },
                    "index_config": {"collection_prefix": "smoke"},
                },
            )
            assert vector_store.status_code == 201, vector_store.text
            vector_data = vector_store.json()["data"]
            vector_id = vector_data["id"]
            assert vector_data["connection_config"]["password"] == "***"
            assert vector_data["connection_config"]["api_key"] == "***"
            assert vector_data["connection_config"]["version"] == "8.12.0"
            assert vector_data["source"] == "user"
            assert vector_data["readonly"] is False
            assert "https://qdrant.example.test" in ssrf_calls

            duplicate = await client.post(
                "/api/v1/vector-stores",
                headers=headers,
                json={
                    "name": f"vector-duplicate-{suffix}",
                    "engine_type": "qdrant",
                    "connection_config": {
                        "host": "qdrant.example.test",
                        "password": "OTHER-PASSWORD",
                    },
                    "index_config": {"collection_prefix": "smoke"},
                },
            )
            assert duplicate.status_code == 409, duplicate.text

            raw_vector_test = await client.post(
                "/api/v1/vector-stores/test",
                headers=headers,
                json={"engine_type": "qdrant", "connection_config": {"host": "raw.example.test", "api_key": "RAW-KEY"}},
            )
            assert raw_vector_test.status_code == 200, raw_vector_test.text
            assert raw_vector_test.json() == {"success": True, "version": "8.12.0"}
            assert vector_calls[-1][1]["api_key"] == "RAW-KEY"

            vector_test = await client.post(f"/api/v1/vector-stores/{vector_id}/test", headers=headers)
            assert vector_test.status_code == 200, vector_test.text
            assert vector_test.json() == {"success": True, "version": "8.12.0"}
            assert vector_calls[-1][1]["api_key"] == "VECTOR-KEY"

            updated_vector = await client.put(
                f"/api/v1/vector-stores/{vector_id}",
                headers=headers,
                json={
                    "name": f"vector-renamed-{suffix}",
                    "engine_type": "milvus",
                    "connection_config": {"host": "qdrant-v2.example.test"},
                    "index_config": {"collection_prefix": "renamed"},
                },
            )
            assert updated_vector.status_code == 200, updated_vector.text
            updated_data = updated_vector.json()["data"]
            assert updated_data["name"] == f"vector-renamed-{suffix}"
            assert updated_data["engine_type"] == "qdrant"
            assert updated_data["connection_config"]["host"] == "qdrant.example.test"
            assert updated_data["index_config"]["collection_prefix"] == "smoke"

            missing_name = await client.put(
                f"/api/v1/vector-stores/{vector_id}",
                headers=headers,
                json={},
            )
            assert missing_name.status_code == 400, missing_name.text

            invalid_create = await client.post(
                "/api/v1/vector-stores",
                headers=headers,
                json={
                    "name": f"invalid-{suffix}",
                    "engine_type": "postgres",
                    "connection_config": {"use_default_connection": True},
                },
            )
            assert invalid_create.status_code == 400, invalid_create.text

            missing_connection = await client.post(
                "/api/v1/vector-stores",
                headers=headers,
                json={
                    "name": f"missing-{suffix}",
                    "engine_type": "elasticsearch",
                    "connection_config": {},
                },
            )
            assert missing_connection.status_code == 400, missing_connection.text

            raw_validation = await client.post(
                "/api/v1/vector-stores/test",
                headers=headers,
                json={"engine_type": "qdrant", "connection_config": {}},
            )
            assert raw_validation.status_code == 200, raw_validation.text
            assert raw_validation.json() == {
                "success": False,
                "error": "host is required for qdrant",
            }

            raw_unsupported = await client.post(
                "/api/v1/vector-stores/test",
                headers=headers,
                json={"engine_type": "sqlite", "connection_config": {}},
            )
            assert raw_unsupported.status_code == 200, raw_unsupported.text
            assert raw_unsupported.json()["success"] is False

            await client.post(f"/api/v1/vector-stores/{vector_id}/test", headers=headers)
            assert vector_calls[-1][1]["password"] == "VECTOR-PASSWORD"
            assert vector_calls[-1][1]["api_key"] == "VECTOR-KEY"

            def failed_vector_store(engine_type, connection):
                vector_calls.append((engine_type, dict(connection)))
                raise connectivity.ConnectivityError("connection refused")

            monkeypatch.setattr(connectivity, "test_vector_store", failed_vector_store)
            failed_create = await client.post(
                "/api/v1/vector-stores",
                headers=headers,
                json={
                    "name": f"vector-failed-{suffix}",
                    "engine_type": "qdrant",
                    "connection_config": {"host": "unreachable.example.test"},
                },
            )
            assert failed_create.status_code == 400, failed_create.text
            failed_raw = await client.post(
                "/api/v1/vector-stores/test",
                headers=headers,
                json={
                    "engine_type": "qdrant",
                    "connection_config": {"host": "unreachable.example.test"},
                },
            )
            assert failed_raw.status_code == 200, failed_raw.text
            assert failed_raw.json()["success"] is False
            monkeypatch.setattr(connectivity, "test_vector_store", fake_vector_store)

            def blocked_ssrf(url):
                raise infra_router.url_guard.URLGuardError("loopback address")

            monkeypatch.setattr(
                infra_router.url_guard, "validate_url_for_ssrf", blocked_ssrf
            )
            blocked_raw = await client.post(
                "/api/v1/vector-stores/test",
                headers=headers,
                json={
                    "engine_type": "qdrant",
                    "connection_config": {"host": "127.0.0.1"},
                },
            )
            assert blocked_raw.status_code == 200, blocked_raw.text
            assert blocked_raw.json()["success"] is False
            blocked_create = await client.post(
                "/api/v1/vector-stores",
                headers=headers,
                json={
                    "name": f"vector-blocked-{suffix}",
                    "engine_type": "qdrant",
                    "connection_config": {"host": "127.0.0.1"},
                },
            )
            assert blocked_create.status_code == 400, blocked_create.text
            monkeypatch.setattr(
                infra_router.url_guard,
                "validate_url_for_ssrf",
                lambda url: ssrf_calls.append(url),
            )

            bound_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": f"vector-bound-{suffix}",
                    "vector_store_id": vector_id,
                },
            )
            assert bound_kb.status_code == 201, bound_kb.text
            blocked_delete = await client.delete(
                f"/api/v1/vector-stores/{vector_id}", headers=headers
            )
            assert blocked_delete.status_code == 400, blocked_delete.text
            assert "still has 1 knowledge base(s) bound" in blocked_delete.text
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET vector_store_id=NULL WHERE id=?",
                    (bound_kb.json()["data"]["id"],),
                )
            deleted = await client.delete(
                f"/api/v1/vector-stores/{vector_id}", headers=headers
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json() == {"success": True}

            engine_check = await client.post(
                "/api/v1/system/storage-engine-check",
                headers=headers,
                json={
                    "provider": "s3",
                    "s3": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key": "CHECK-ACCESS",
                        "secret_key": "CHECK-SECRET",
                    },
                },
            )
            assert engine_check.status_code == 200, engine_check.text
            assert engine_check.json()["code"] == 0
            assert engine_check.json()["data"]["ok"] is True
            assert storage_calls[-1][0] == "s3"

            engine_status = await client.get("/api/v1/system/storage-engine-status", headers=headers)
            assert engine_status.status_code == 200, engine_status.text
            status_data = engine_status.json()
            assert status_data["code"] == 0
            assert len(status_data["data"]["engines"]) == 8
            assert "local" in status_data["data"]["allowed_providers"]
            assert status_data["data"]["engines"][0]["available"] is True

    _run(case)


def test_vector_store_types_exposes_registration_schema(monkeypatch) -> None:
    monkeypatch.setenv("TENCENT_VECTORDB_REPLICA_NUMBER", "0")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            response = await client.get("/api/v1/vector-stores/types", headers=_headers(login))
            assert response.status_code == 200, response.text

            entries = response.json()["data"]
            assert [entry["type"] for entry in entries] == [
                "elasticsearch",
                "qdrant",
                "milvus",
                "tencent_vectordb",
                "weaviate",
                "doris",
                "opensearch",
            ]
            assert all("engine_type" not in entry and "name" not in entry for entry in entries)

            for entry in entries:
                assert set(entry) == {"type", "display_name", "connection_fields", "index_fields"}
                assert entry["display_name"]
                assert entry["connection_fields"]
                assert entry["index_fields"]
                for field in entry["connection_fields"] + entry["index_fields"]:
                    assert {"name", "type", "required"} <= set(field)

            by_type = {entry["type"]: entry for entry in entries}
            qdrant_fields = {field["name"]: field for field in by_type["qdrant"]["connection_fields"]}
            assert qdrant_fields["host"]["required"] is True
            assert qdrant_fields["api_key"]["sensitive"] is True

            tencent_index = {
                field["name"]: field for field in by_type["tencent_vectordb"]["index_fields"]
            }
            assert tencent_index["replica_number"]["default"] == 0

            opensearch_index = {
                field["name"]: field for field in by_type["opensearch"]["index_fields"]
            }
            assert opensearch_index["hnsw_m"]["immutable"] is True
            assert opensearch_index["hnsw_m"]["min"] == 2
            assert opensearch_index["hnsw_m"]["max"] == 100
            assert opensearch_index["knn_engine"]["enum"] == ["lucene", "faiss"]

    _run(case)


def test_vector_store_environment_stores_are_readonly_and_testable(monkeypatch) -> None:
    from app.services import connectivity

    vector_calls: list[tuple[str, dict]] = []

    def fake_vector_store(engine_type, connection):
        vector_calls.append((engine_type, dict(connection)))
        return {"ok": True, "engine_type": engine_type, "version": "1.12.6"}

    monkeypatch.setenv("RETRIEVE_DRIVER", "sqlite,qdrant")
    monkeypatch.setenv("QDRANT_HOST", "localhost")
    monkeypatch.setenv("QDRANT_PORT", "6334")
    monkeypatch.setenv("QDRANT_API_KEY", "ENV-KEY")
    monkeypatch.setenv("QDRANT_COLLECTION", "env_collection")
    monkeypatch.setattr(connectivity, "test_vector_store", fake_vector_store)
    monkeypatch.setattr(
        infra_router.url_guard,
        "validate_url_for_ssrf",
        lambda url: None,
    )

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            listed = await client.get("/api/v1/vector-stores", headers=headers)
            assert listed.status_code == 200, listed.text
            stores = listed.json()["data"]
            assert [store["id"] for store in stores[:2]] == [
                "__env_sqlite__",
                "__env_qdrant__",
            ]
            assert all(store["source"] == "env" for store in stores[:2])
            assert all(store["readonly"] is True for store in stores[:2])
            assert stores[1]["connection_config"]["api_key"] == "***"
            assert "ENV-KEY" not in listed.text

            fetched = await client.get(
                "/api/v1/vector-stores/__env_qdrant__", headers=headers
            )
            assert fetched.status_code == 200, fetched.text
            fetched_data = fetched.json()["data"]
            assert fetched_data["engine_type"] == "qdrant"
            assert fetched_data["connection_config"]["api_key"] == "***"

            tested = await client.post(
                "/api/v1/vector-stores/__env_qdrant__/test", headers=headers
            )
            assert tested.status_code == 200, tested.text
            assert tested.json() == {"success": True, "version": "1.12.6"}
            assert vector_calls[-1] == (
                "qdrant",
                {
                    "host": "localhost",
                    "port": 6334,
                    "api_key": "ENV-KEY",
                },
            )

            updated = await client.put(
                "/api/v1/vector-stores/__env_qdrant__",
                headers=headers,
                json={"name": "changed"},
            )
            assert updated.status_code == 400, updated.text
            assert updated.json()["error"]["message"] == (
                "environment-configured vector stores cannot be modified via API"
            )

            deleted = await client.delete(
                "/api/v1/vector-stores/__env_qdrant__", headers=headers
            )
            assert deleted.status_code == 400, deleted.text
            assert deleted.json()["error"]["message"] == (
                "environment-configured vector stores cannot be modified via API"
            )

            duplicate = await client.post(
                "/api/v1/vector-stores",
                headers=headers,
                json={
                    "name": f"env-duplicate-{suffix}",
                    "engine_type": "qdrant",
                    "connection_config": {"host": "localhost", "port": 6334},
                    "index_config": {"collection_prefix": "env_collection"},
                },
            )
            assert duplicate.status_code == 409, duplicate.text

            missing = await client.get(
                "/api/v1/vector-stores/__env_milvus__", headers=headers
            )
            assert missing.status_code == 404, missing.text

    _run(case)


def test_model_limiter_gates_only_background_calls(monkeypatch) -> None:
    from app.services import model_limiter

    monkeypatch.setattr(model_limiter, "_configured_limit", lambda: 1)
    model_id = f"limiter-{uuid.uuid4()}"
    runtime = {"id": model_id, "name": "limiter-model", "display_name": "Limiter model"}

    release = model_limiter.acquire(runtime)
    release()
    assert not any(item["model_id"] == model_id for item in model_limiter.runtime_stats())
    with model_limiter.background():
        fallback = model_limiter.acquire({**runtime, "max_concurrency": -1})
        fallback_stat = next(
            item for item in model_limiter.runtime_stats() if item["model_id"] == model_id
        )
        assert fallback_stat["active"] == 1
        assert fallback_stat["limit"] == 1
        fallback()

    entered = threading.Event()
    with model_limiter.background():
        held = model_limiter.acquire(runtime)

        def wait_for_slot() -> None:
            with model_limiter.background():
                release_second = model_limiter.acquire(runtime)
                entered.set()
                release_second()

        worker = threading.Thread(target=wait_for_slot)
        worker.start()
        time.sleep(0.05)
        assert entered.is_set() is False
        stat = next(item for item in model_limiter.runtime_stats() if item["model_id"] == model_id)
        assert stat == {
            "model_id": model_id,
            "name": "Limiter model",
            "active": 1,
            "waiting": 1,
            "limit": 1,
        }
        held()
        worker.join(timeout=2)
        assert entered.is_set() is True

    final = next(item for item in model_limiter.runtime_stats() if item["model_id"] == model_id)
    assert final["active"] == 0
    assert final["waiting"] == 0


def test_runtime_queue_concurrency_reads_system_settings() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            try:
                for key, value in (
                    ("asynq.core_concurrency", "9"),
                    ("asynq.wiki_concurrency", "11"),
                ):
                    updated = await client.put(
                        f"/api/v1/system/admin/settings/{key}",
                        headers=headers,
                        json={"value": value},
                    )
                    assert updated.status_code == 200, updated.text

                response = await client.get(
                    "/api/v1/system/admin/runtime/queues", headers=headers
                )
                assert response.status_code == 200, response.text
                runtime = response.json()
                pools = {pool["name"]: pool for pool in runtime["pools"]}
                assert pools["core"]["concurrency"] == 9
                assert pools["wiki"]["concurrency"] == 11
                assert runtime["upstream_concurrency"] == 33
                assert runtime["wiki_concurrency"] == 11
            finally:
                for key in ("asynq.core_concurrency", "asynq.wiki_concurrency"):
                    await client.delete(f"/api/v1/system/admin/settings/{key}", headers=headers)

    _run(case)


def test_workspace_policy_persists() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = login["active_tenant"]["id"]

            updated = await client.put(
                "/api/v1/sandbox-configs/workspace-policy",
                headers=headers,
                json={"scripts_disabled": True},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["workspace_scripts_disabled"] is True

            fetched = await client.get("/api/v1/sandbox-configs/workspace-policy", headers=headers)
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["data"]["scripts_disabled"] is True

        row = db.fetch_one("SELECT scripts_disabled FROM py_workspace_policies WHERE tenant_id=?", (tenant_id,))
        assert row and row["scripts_disabled"] == 1

    _run(case)


def test_ollama_endpoints_use_runtime_service(monkeypatch) -> None:
    from app.services import ollama as ollama_service

    task = {
        "id": "task-1",
        "modelName": "qwen2.5:7b",
        "status": "downloading",
        "progress": 42.5,
        "message": "pulling manifest",
        "startTime": "2026-10-17T00:00:00Z",
    }
    monkeypatch.setattr(
        ollama_service,
        "status",
        lambda: {"available": True, "version": "0.12.0", "baseUrl": "http://ollama.test:11434"},
    )
    monkeypatch.setattr(
        ollama_service,
        "list_models",
        lambda: [{"name": "qwen2.5:7b", "model": "qwen2.5:7b", "size": 123, "digest": "sha", "modified_at": "now", "details": {}}],
    )
    monkeypatch.setattr(ollama_service, "check_models", lambda names: {name: name == "qwen2.5:7b" for name in names})
    monkeypatch.setattr(
        ollama_service,
        "download_model",
        lambda name: ({"taskId": "task-1", "modelName": name, "status": "pending", "progress": 0.0}, "模型下载任务已创建"),
    )
    monkeypatch.setattr(ollama_service, "get_task", lambda task_id: task if task_id == "task-1" else None)
    monkeypatch.setattr(ollama_service, "list_tasks", lambda: [task])

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            status = await client.get("/api/v1/initialization/ollama/status", headers=headers)
            assert status.status_code == 200, status.text
            assert status.json()["data"] == {
                "available": True,
                "version": "0.12.0",
                "baseUrl": "http://ollama.test:11434",
            }

            models = await client.get("/api/v1/initialization/ollama/models", headers=headers)
            assert models.status_code == 200, models.text
            assert models.json()["data"]["models"][0]["name"] == "qwen2.5:7b"

            checked = await client.post(
                "/api/v1/initialization/ollama/models/check",
                headers=headers,
                json={"models": ["qwen2.5:7b", "missing:latest"]},
            )
            assert checked.status_code == 200, checked.text
            assert checked.json()["data"]["models"] == {"qwen2.5:7b": True, "missing:latest": False}

            downloaded = await client.post(
                "/api/v1/initialization/ollama/models/download",
                headers=headers,
                json={"modelName": "qwen2.5:7b"},
            )
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.json()["data"]["taskId"] == "task-1"
            assert downloaded.json()["message"] == "模型下载任务已创建"

            tasks = await client.get("/api/v1/initialization/ollama/download/tasks", headers=headers)
            assert tasks.status_code == 200, tasks.text
            assert tasks.json()["data"] == [task]

            progress = await client.get("/api/v1/initialization/ollama/download/progress/task-1", headers=headers)
            assert progress.status_code == 200, progress.text
            assert progress.json()["data"]["progress"] == 42.5

            missing = await client.get("/api/v1/initialization/ollama/download/progress/missing", headers=headers)
            assert missing.status_code == 404

    _run(case)


def test_weknoracloud_credentials_are_saved_and_reported(monkeypatch) -> None:
    from app.services import weknoracloud as cloud_service

    verified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cloud_service,
        "verify_credentials",
        lambda app_id, app_secret: verified.append((app_id, app_secret)),
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            saved = await client.post(
                "/api/v1/weknoracloud/credentials",
                headers=headers,
                json={"app_id": "wx-app-id", "app_secret": "cloud-secret"},
            )
            assert saved.status_code == 200, saved.text
            assert saved.json() == {"success": True, "message": "凭证保存成功"}
            assert verified[-1] == ("wx-app-id", "cloud-secret")

            status = await client.get("/api/v1/models/weknoracloud/status", headers=headers)
            assert status.status_code == 200, status.text
            assert status.json() == {"has_models": True, "needs_reinit": False}
            assert "cloud-secret" not in status.text

            tenant = await client.get(f"/api/v1/tenants/{login['active_tenant']['id']}", headers=headers)
            assert tenant.status_code == 200, tenant.text
            assert "cloud-secret" not in tenant.text

            invalid = await client.post(
                "/api/v1/weknoracloud/credentials",
                headers=headers,
                json={"app_id": "wx-app-id"},
            )
            assert invalid.status_code == 400

    _run(case)


def test_model_catalog_preview_publish_and_runtime_topology() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            catalog = await client.get("/api/v1/system/admin/model-catalog", headers=headers)
            assert catalog.status_code == 200, catalog.text
            state = catalog.json()
            assert state["version"] >= 1
            assert state["baseline"]
            assert state["overlay"]["providers"] == {}
            assert any(provider["id"] == "openai" for provider in state["builtin"])
            assert any(provider["id"] == "openai" for provider in state["effective"])

            overlay = {
                "providers": {
                    "openai": {
                        "models": [
                            {
                                "id": "catalog-test-model",
                                "type": "KnowledgeQA",
                                "name": "Catalog test",
                                "context_window": 128000,
                            }
                        ]
                    }
                }
            }
            preview = await client.post(
                "/api/v1/system/admin/model-catalog/preview",
                headers=headers,
                json={"version": state["version"], "baseline": state["baseline"], "overlay": overlay},
            )
            assert preview.status_code == 200, preview.text
            preview_state = preview.json()
            openai = next(item for item in preview_state["effective"] if item["id"] == "openai")
            assert any(model["id"] == "catalog-test-model" for model in openai["models"])

            published = await client.put(
                "/api/v1/system/admin/model-catalog",
                headers=headers,
                json={"version": state["version"], "baseline": state["baseline"], "overlay": overlay},
            )
            assert published.status_code == 200, published.text
            published_state = published.json()
            assert published_state["version"] == state["version"] + 1
            assert published_state["history"][-1]["overlay"] == overlay

            stale = await client.put(
                "/api/v1/system/admin/model-catalog",
                headers=headers,
                json={"version": state["version"], "baseline": state["baseline"], "overlay": {"providers": {}}},
            )
            assert stale.status_code == 409

            queues = await client.get("/api/v1/system/admin/runtime/queues", headers=headers)
            assert queues.status_code == 200, queues.text
            runtime = queues.json()
            assert runtime["available"] is True
            assert runtime["upstream_concurrency"] == 32
            assert len(runtime["pools"]) == 6
            assert runtime["queues"][0]["name"] == "database"
            assert runtime["model_limiter_available"] is True
            assert isinstance(runtime["models"], list)

            now = db.utcnow()
            pending_id = f"runtime-pending-{uuid.uuid4()}"
            failed_id = f"runtime-failed-{uuid.uuid4()}"
            completed_id = f"runtime-completed-{uuid.uuid4()}"
            with db.transaction() as conn:
                for task_id, status in (
                    (pending_id, "pending"),
                    (failed_id, "failed"),
                    (completed_id, "completed"),
                ):
                    conn.execute(
                        "INSERT INTO py_task_results(id, tenant_id, kind, status, total, processed, payload, result, error, created_at, updated_at) "
                        "VALUES (?, 1, 'runtime_test', ?, 0, 0, '{}', '{}', '', ?, ?)",
                        (task_id, status, now, now),
                    )

            pending_tasks = await client.get(
                "/api/v1/system/admin/runtime/queues/database/tasks?state=pending&page_size=200",
                headers=headers,
            )
            assert pending_tasks.status_code == 200, pending_tasks.text
            assert any(task["task_id"] == pending_id for task in pending_tasks.json()["tasks"])

            action = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{pending_id}/actions/run_now",
                headers=headers,
            )
            assert action.status_code == 409

            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Runtime rerun"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            retry_id = f"runtime-retry-{uuid.uuid4()}"
            retry_payload = {
                "tenant_id": login["active_tenant"]["id"],
                "kb_id": kb_id,
                "entries": [],
                "mode": "append",
                "dry_run": True,
            }
            retry_after = time.time() + 60
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO py_task_results"
                    "(id, tenant_id, kind, status, total, processed, payload, result, error, "
                    "retry_count, max_retry, retry_after, created_at, updated_at) "
                    "VALUES (?, ?, 'faq_import', 'pending', 0, 0, ?, '{}', 'transient', 1, 3, ?, ?, ?)",
                    (
                        retry_id,
                        login["active_tenant"]["id"],
                        json.dumps(retry_payload),
                        f"{retry_after:.3f}",
                        now,
                        now,
                    ),
                )
            retry_tasks = await client.get(
                "/api/v1/system/admin/runtime/queues/database/tasks?state=retry&page_size=200",
                headers=headers,
            )
            retry_task = next(
                task for task in retry_tasks.json()["tasks"] if task["task_id"] == retry_id
            )
            assert retry_task["state"] == "retry"
            assert retry_task["retried"] == 1
            assert retry_task["max_retry"] == 3
            assert retry_task["next_process_at"]
            assert {"cancel", "run_now"} <= set(retry_task["allowed_actions"])
            pending_after_retry = await client.get(
                "/api/v1/system/admin/runtime/queues/database/tasks?state=pending&page_size=200",
                headers=headers,
            )
            assert all(
                task["task_id"] != retry_id
                for task in pending_after_retry.json()["tasks"]
            )
            queues_with_retry = await client.get(
                "/api/v1/system/admin/runtime/queues", headers=headers
            )
            assert queues_with_retry.json()["queues"][0]["retry"] >= 1
            retry_now = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{retry_id}/actions/run_now",
                headers=headers,
            )
            assert retry_now.status_code == 200, retry_now.text
            for _ in range(500):
                retry_row = db.fetch_one(
                    "SELECT status, retry_count, retry_after FROM py_task_results WHERE id=?",
                    (retry_id,),
                )
                if retry_row and retry_row["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert retry_row is not None
            assert retry_row["status"] == "completed"
            assert retry_row["retry_count"] == 1
            assert retry_row["retry_after"] == ""

            failed_faq_id = f"runtime-faq-{uuid.uuid4()}"
            faq_payload = {
                "tenant_id": login["active_tenant"]["id"],
                "kb_id": kb_id,
                "entries": [],
                "mode": "append",
                "dry_run": True,
            }
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO py_task_results(id, tenant_id, kind, status, total, processed, payload, result, error, created_at, updated_at) "
                    "VALUES (?, ?, 'faq_import', 'failed', 0, 0, ?, '{}', 'previous failure', ?, ?)",
                    (
                        failed_faq_id,
                        login["active_tenant"]["id"],
                        json.dumps(faq_payload),
                        now,
                        now,
                    ),
                )
            archived_faq = await client.get(
                "/api/v1/system/admin/runtime/queues/database/tasks?state=archived&page_size=200",
                headers=headers,
            )
            failed_task = next(
                task for task in archived_faq.json()["tasks"] if task["task_id"] == failed_faq_id
            )
            assert "run_now" in failed_task["allowed_actions"]
            rerun = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{failed_faq_id}/actions/run_now",
                headers=headers,
            )
            assert rerun.status_code == 200, rerun.text
            for _ in range(500):
                rerun_row = db.fetch_one("SELECT status FROM py_task_results WHERE id=?", (failed_faq_id,))
                if rerun_row and rerun_row["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert rerun_row is not None and rerun_row["status"] == "completed"

            cancelled = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{pending_id}/actions/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200, cancelled.text
            assert db.fetch_one("SELECT status FROM py_task_results WHERE id=?", (pending_id,))["status"] == "cancelled"

            archived = await client.get(
                "/api/v1/system/admin/runtime/queues/database/tasks?state=archived&page_size=200",
                headers=headers,
            )
            archived_ids = {task["task_id"] for task in archived.json()["tasks"]}
            assert {pending_id, failed_id} <= archived_ids

            deleted = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{failed_id}/actions/delete",
                headers=headers,
            )
            assert deleted.status_code == 200, deleted.text
            assert db.fetch_one("SELECT 1 FROM py_task_results WHERE id=?", (failed_id,)) is None

            purged = await client.delete(
                "/api/v1/system/admin/runtime/queues/database/archived",
                headers=headers,
            )
            assert purged.status_code == 200
            assert purged.json()["deleted"] >= 1
            assert db.fetch_one("SELECT 1 FROM py_task_results WHERE id=?", (pending_id,)) is None
            assert db.fetch_one("SELECT 1 FROM py_task_results WHERE id=?", (completed_id,)) is not None

    _run(case)


def test_initialization_chat_check_allows_non_auth_400(monkeypatch) -> None:
    from app.services import model_runtime

    failures = {
        "https://bad-parameter.test/v1": model_runtime.UpstreamHTTPError(
            400, '{"error":{"message":"max_tokens is not supported"}}'
        ),
        "https://bad-key.test/v1": model_runtime.UpstreamHTTPError(
            400, '{"error":{"message":"API_KEY_INVALID"}}'
        ),
        "https://unauthorized.test/v1": model_runtime.UpstreamHTTPError(
            401, '{"error":{"message":"invalid credentials"}}'
        ),
    }

    def fake_chat(runtime, _messages, **_kwargs):
        raise failures[str(runtime["base_url"])]

    monkeypatch.setattr(model_runtime, "chat_completion", fake_chat)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            async def check(base_url: str) -> dict:
                response = await client.post(
                    "/api/v1/initialization/remote/check",
                    headers=headers,
                    json={
                        "modelName": "test-model",
                        "baseUrl": base_url,
                        "apiKey": "test-key",
                        "provider": "openai-compatible",
                    },
                )
                assert response.status_code == 200, response.text
                return response.json()["data"]

            parameter_error = await check("https://bad-parameter.test/v1")
            assert parameter_error == {"available": True, "message": "连接正常，模型可用"}

            auth_error = await check("https://bad-key.test/v1")
            assert auth_error["available"] is False
            assert "HTTP 400" in auth_error["message"]

            unauthorized = await check("https://unauthorized.test/v1")
            assert unauthorized["available"] is False
            assert "HTTP 401" in unauthorized["message"]

    _run(case)


def test_initialization_model_checks_call_runtime(monkeypatch) -> None:
    from app.services import model_runtime

    calls: list[tuple[str, dict]] = []

    def fake_chat(runtime, messages, **kwargs):
        calls.append(("chat", runtime))
        return {"choices": [{"message": {"role": "assistant", "content": "图片里是一张测试图片"}}]}

    def fake_embedding(runtime, texts):
        calls.append(("embedding", runtime))
        return [[0.1, 0.2, 0.3]]

    def fake_rerank(runtime, query, documents):
        calls.append(("rerank", runtime))
        return [{"index": 0, "relevance_score": 0.9, "document": documents[0]}]

    def fake_transcribe(runtime, data, filename):
        calls.append(("asr", runtime))
        assert data.startswith(b"RIFF")
        assert filename.endswith(".wav")
        return {"text": "hi"}

    monkeypatch.setattr(model_runtime, "chat_completion", fake_chat)
    monkeypatch.setattr(model_runtime, "embedding", fake_embedding)
    monkeypatch.setattr(model_runtime, "rerank", fake_rerank)
    monkeypatch.setattr(model_runtime, "transcribe", fake_transcribe)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            model = {
                "modelName": "test-model",
                "baseUrl": "https://models.test/v1",
                "apiKey": "test-key",
                "provider": "openai-compatible",
            }

            remote = await client.post("/api/v1/initialization/remote/check", headers=headers, json=model)
            assert remote.status_code == 200, remote.text
            assert remote.json()["data"] == {"available": True, "message": "连接成功"}
            assert calls[-1][1]["base_url"] == "https://models.test/v1"
            assert calls[-1][1]["api_key"] == "test-key"

            embedding = await client.post("/api/v1/initialization/embedding/test", headers=headers, json=model)
            assert embedding.status_code == 200, embedding.text
            assert embedding.json()["data"]["available"] is True
            assert embedding.json()["data"]["dimension"] == 3

            rerank = await client.post("/api/v1/initialization/rerank/check", headers=headers, json=model)
            assert rerank.status_code == 200, rerank.text
            assert rerank.json()["data"]["available"] is True
            assert "1个结果" in rerank.json()["data"]["message"]

            asr = await client.post("/api/v1/initialization/asr/check", headers=headers, json=model)
            assert asr.status_code == 200, asr.text
            assert asr.json()["data"]["available"] is True
            assert "hi" in asr.json()["data"]["message"]

            multimodal = await client.post(
                "/api/v1/initialization/multimodal/test",
                headers=headers,
                data={
                    "vlm_model": "vision-test",
                    "vlm_base_url": "https://models.test/v1",
                    "vlm_api_key": "vision-key",
                },
                files={"image": ("test.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            )
            assert multimodal.status_code == 200, multimodal.text
            payload = multimodal.json()["data"]
            assert payload["success"] is True
            assert payload["caption"] == "图片里是一张测试图片"
            assert payload["processing_time"] >= 0

    _run(case)


def test_initialization_config_roundtrip() -> None:
    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            llm = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": f"init-llm-{suffix}",
                    "display_name": "Init LLM",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {"base_url": "https://init-llm.test/v1", "api_key": "llm-secret"},
                },
            )
            assert llm.status_code == 201, llm.text
            embedding = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": f"init-embed-{suffix}",
                    "display_name": "Init Embedding",
                    "type": "Embedding",
                    "source": "remote",
                    "parameters": {"base_url": "https://init-embed.test/v1", "api_key": "embed-secret"},
                },
            )
            assert embedding.status_code == 201, embedding.text
            llm_id = llm.json()["data"]["id"]
            embedding_id = embedding.json()["data"]["id"]

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"init-kb-{suffix}", "embedding_model_id": embedding_id},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            updated = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={
                    "llmModelId": llm_id,
                    "embeddingModelId": embedding_id,
                    "documentSplitting": {
                        "chunkSize": 768,
                        "chunkOverlap": 64,
                        "separators": ["\n\n", "\n"],
                        "strategy": "auto",
                        "tokenLimit": 600,
                        "languages": ["zh"],
                    },
                    "vlm_config": {"enabled": True, "model_id": llm_id, "storage_type": "minio"},
                    "asr_config": {"enabled": True, "model_id": llm_id, "language": "zh"},
                    "nodeExtract": {"enabled": True, "text": "extract", "tags": ["a"], "nodes": [], "relations": []},
                    "questionGeneration": {"enabled": True, "questionCount": 3},
                    "multimodal": {"enabled": True},
                },
            )
            assert updated.status_code == 200, updated.text
            config = updated.json()["data"]
            assert config["llm"]["modelName"] == f"init-llm-{suffix}"
            assert config["llm"]["baseUrl"] == "https://init-llm.test/v1"
            assert config["llm"]["credentials"]["apiKey"] is True
            assert config["embedding"]["modelName"] == f"init-embed-{suffix}"
            assert config["documentSplitting"]["chunkSize"] == 768
            assert config["documentSplitting"]["chunkOverlap"] == 64
            assert config["documentSplitting"]["strategy"] == "auto"
            assert config["nodeExtract"]["enabled"] is True
            assert config["questionGeneration"]["enabled"] is True
            assert config["questionGeneration"]["questionCount"] == 3
            assert config["multimodal"]["enabled"] is True
            assert config["asr"]["enabled"] is True
            assert config["hasFiles"] is False
            assert "llm-secret" not in updated.text

            fetched = await client.get(f"/api/v1/initialization/config/{kb_id}", headers=headers)
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["data"]["documentSplitting"]["chunkSize"] == 768

            viewer_headers = await _login_tenant_member(
                client, int(login["active_tenant"]["id"]), "viewer"
            )
            viewer_config = await client.get(
                f"/api/v1/initialization/config/{kb_id}", headers=viewer_headers
            )
            assert viewer_config.status_code == 200, viewer_config.text
            assert viewer_config.json()["data"]["llm"]["baseUrl"] == ""
            assert viewer_config.json()["data"]["llm"]["credentials"]["apiKey"] is True

            missing_llm = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={"llmModelId": "missing-llm"},
            )
            assert missing_llm.status_code == 400, missing_llm.text
            assert missing_llm.json()["error"]["message"] == "LLM模型不存在"
            missing_embedding = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={"embeddingModelId": "missing-embedding"},
            )
            assert missing_embedding.status_code == 400, missing_embedding.text
            assert missing_embedding.json()["error"]["message"] == "Embedding模型不存在"

            disabled = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={
                    "multimodal": {"enabled": False},
                    "vlm_config": {
                        "enabled": False,
                        "model_id": llm_id,
                        "description_language": "Chinese",
                        "custom_instructions": "Keep labels.",
                    },
                    "asr_config": {"enabled": False, "model_id": llm_id, "language": "zh"},
                },
            )
            assert disabled.status_code == 200, disabled.text
            disabled_config = disabled.json()["data"]
            assert disabled_config["multimodal"]["enabled"] is False
            assert disabled_config["multimodal"]["descriptionLanguage"] == "Chinese"
            assert disabled_config["multimodal"]["customInstructions"] == "Keep labels."
            assert disabled_config["asr"]["enabled"] is False
            stored_vlm = db.fetch_one("SELECT vlm_config FROM knowledge_bases WHERE id=?", (kb_id,))
            assert json.loads(stored_vlm["vlm_config"]) == {
                "enabled": False,
                "model_id": "",
                "description_language": "Chinese",
                "custom_instructions": "Keep labels.",
            }

            unavailable_models = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={
                    "multimodal": {"enabled": True},
                    "vlm_config": {"enabled": True, "model_id": "missing-vlm"},
                    "asr_config": {"enabled": True, "model_id": "missing-asr", "language": "zh"},
                },
            )
            assert unavailable_models.status_code == 200, unavailable_models.text
            unavailable_config = unavailable_models.json()["data"]
            assert unavailable_config["multimodal"]["enabled"] is False
            assert unavailable_config["asr"]["enabled"] is False

            initialized = await client.post(
                f"/api/v1/initialization/initialize/{kb_id}",
                headers=headers,
                json={"documentSplitting": {"chunkSize": 1024, "chunkOverlap": 80, "separators": ["\n"]}},
            )
            assert initialized.status_code == 200, initialized.text
            assert initialized.json()["data"] == {"initialized": True}
            fetched_again = await client.get(f"/api/v1/initialization/config/{kb_id}", headers=headers)
            assert fetched_again.json()["data"]["documentSplitting"]["chunkSize"] == 1024

            other_embedding = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": f"init-embed-other-{suffix}",
                    "type": "Embedding",
                    "source": "remote",
                    "parameters": {"base_url": "https://init-embed-other.test/v1"},
                },
            )
            assert other_embedding.status_code == 201, other_embedding.text
            switched_embedding = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={"embeddingModelId": other_embedding.json()["data"]["id"]},
            )
            assert switched_embedding.status_code == 200, switched_embedding.text
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "embedding guard", "content": "cannot switch embedding now"},
            )
            assert manual.status_code == 201, manual.text
            blocked_embedding = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={"embeddingModelId": embedding_id},
            )
            assert blocked_embedding.status_code == 400, blocked_embedding.text
            assert blocked_embedding.json()["error"]["message"] == "知识库中已有文件，无法修改Embedding模型"

            with db.transaction() as conn:
                conn.execute("UPDATE models SET is_builtin=1 WHERE id=?", (llm_id,))
            builtin_config = await client.get(
                f"/api/v1/initialization/config/{kb_id}", headers=headers
            )
            assert builtin_config.status_code == 200, builtin_config.text
            assert builtin_config.json()["data"]["llm"]["baseUrl"] == ""
            assert builtin_config.json()["data"]["llm"]["credentials"]["apiKey"] is False

            clamped_generation = await client.put(
                f"/api/v1/initialization/config/{kb_id}",
                headers=headers,
                json={
                    "questionGeneration": {
                        "enabled": True,
                        "questionCount": 99,
                        "customInstructions": "  concise  ",
                    }
                },
            )
            assert clamped_generation.status_code == 200, clamped_generation.text
            assert clamped_generation.json()["data"]["questionGeneration"] == {
                "enabled": True,
                "questionCount": 10,
                "customInstructions": "concise",
            }

    _run(case)


def test_initialization_multimodal_enabled_matches_go_semantics() -> None:
    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            legacy = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": f"legacy-multimodal-{suffix}",
                    "image_processing_config": {"enable_multimodal": True},
                },
            )
            assert legacy.status_code == 201, legacy.text
            legacy_id = legacy.json()["data"]["id"]
            legacy_config = await client.get(
                f"/api/v1/initialization/config/{legacy_id}", headers=headers
            )
            assert legacy_config.status_code == 200, legacy_config.text
            assert legacy_config.json()["data"]["multimodal"]["enabled"] is False

            writer = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": f"multimodal-writer-{suffix}",
                    "image_processing_config": {"image_vector_enabled": True},
                },
            )
            assert writer.status_code == 201, writer.text
            writer_id = writer.json()["data"]["id"]
            updated = await client.put(
                f"/api/v1/initialization/config/{writer_id}",
                headers=headers,
                json={"multimodal": {"enabled": True}},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["multimodal"]["enabled"] is False
            assert "storageType" not in updated.json()["data"]["multimodal"]
            stored = db.fetch_one(
                "SELECT image_processing_config, storage_provider_config FROM knowledge_bases WHERE id=?",
                (writer_id,),
            )
            assert "enable_multimodal" not in json.loads(stored["image_processing_config"])
            assert json.loads(stored["storage_provider_config"]) == {"provider": "local"}

            vlm = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": f"vlm-multimodal-{suffix}",
                    "vlm_config": {"enabled": True, "model_id": "vlm-model"},
                },
            )
            assert vlm.status_code == 201, vlm.text
            vlm_config = await client.get(
                f"/api/v1/initialization/config/{vlm.json()['data']['id']}", headers=headers
            )
            assert vlm_config.status_code == 200, vlm_config.text
            assert vlm_config.json()["data"]["multimodal"]["enabled"] is True

            storage = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={
                    "name": f"storage-multimodal-{suffix}",
                    "storage_provider_config": {"provider": "minio"},
                },
            )
            assert storage.status_code == 201, storage.text
            storage_config = await client.get(
                f"/api/v1/initialization/config/{storage.json()['data']['id']}", headers=headers
            )
            assert storage_config.status_code == 200, storage_config.text
            assert storage_config.json()["data"]["multimodal"]["enabled"] is True
            assert storage_config.json()["data"]["multimodal"]["storageType"] == "minio"
            assert storage_config.json()["data"]["multimodal"]["minio"] == {
                "bucketName": "",
                "pathPrefix": "",
            }

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET cos_config=? WHERE id=?",
                    (json.dumps({"bucket_name": "legacy-bucket"}), legacy_id),
                )
            legacy_storage = await client.get(
                f"/api/v1/initialization/config/{legacy_id}", headers=headers
            )
            assert legacy_storage.status_code == 200, legacy_storage.text
            assert legacy_storage.json()["data"]["multimodal"]["enabled"] is True
            assert "storageType" not in legacy_storage.json()["data"]["multimodal"]

            provider_update = await client.put(
                f"/api/v1/initialization/config/{writer_id}",
                headers=headers,
                json={"storageProvider": "cos"},
            )
            assert provider_update.status_code == 200, provider_update.text
            assert provider_update.json()["data"]["multimodal"]["storageType"] == "cos"
            assert provider_update.json()["data"]["multimodal"]["cos"]["credentials"] == {
                "secretId": False,
                "secretKey": False,
            }
            stored = db.fetch_one(
                "SELECT storage_provider_config FROM knowledge_bases WHERE id=?", (writer_id,)
            )
            assert json.loads(stored["storage_provider_config"]) == {"provider": "cos"}

            denied_provider = await client.put(
                f"/api/v1/initialization/config/{writer_id}",
                headers=headers,
                json={"storageProvider": "unsupported"},
            )
            assert denied_provider.status_code == 400, denied_provider.text
            assert denied_provider.json()["error"]["message"] == "Storage provider is not allowed by STORAGE_ALLOW_LIST"

            backend = await client.post(
                "/api/v1/storage-backends",
                headers=headers,
                json={
                    "name": f"init-storage-{suffix}",
                    "provider": "s3",
                    "config": {
                        "endpoint": "https://s3.example.test",
                        "region": "us-east-1",
                        "bucket_name": "docs",
                        "access_key_id": "ACCESS",
                        "secret_access_key": "SECRET",
                    },
                },
            )
            assert backend.status_code == 201, backend.text
            backend_id = backend.json()["data"]["id"]
            backend_update = await client.put(
                f"/api/v1/initialization/config/{writer_id}",
                headers=headers,
                json={"storageBackendId": backend_id},
            )
            assert backend_update.status_code == 200, backend_update.text
            assert backend_update.json()["data"]["multimodal"]["storageType"] == "s3"
            stored = db.fetch_one(
                "SELECT storage_backend_id, storage_provider_config FROM knowledge_bases WHERE id=?",
                (writer_id,),
            )
            assert stored["storage_backend_id"] == backend_id
            assert json.loads(stored["storage_provider_config"]) == {"provider": "s3"}

            manual = await client.post(
                f"/api/v1/knowledge-bases/{writer_id}/knowledge/manual",
                headers=headers,
                json={"title": "stored document", "content": "storage binding guard"},
            )
            assert manual.status_code == 201, manual.text
            second_backend = await client.post(
                "/api/v1/storage-backends",
                headers=headers,
                json={"name": f"init-storage-2-{suffix}", "provider": "local", "config": {}},
            )
            assert second_backend.status_code == 201, second_backend.text
            denied_backend_change = await client.put(
                f"/api/v1/initialization/config/{writer_id}",
                headers=headers,
                json={"storageBackendId": second_backend.json()["data"]["id"]},
            )
            assert denied_backend_change.status_code == 400, denied_backend_change.text
            assert (
                denied_backend_change.json()["error"]["message"]
                == "Storage backend cannot be changed while the knowledge base contains files; migrate storage first"
            )

    _run(case)


def test_initialization_extract_uses_chat_model_and_filters_relations(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_chat_completion(runtime, messages, **options):
        calls.append({"runtime": runtime, "messages": messages, "options": options})
        system = messages[0]["content"] if messages[0]["role"] == "system" else ""
        if "information extraction task" in system:
            content = """```json
[
  {"entity": "Romeo and Juliet", "entity_attributes": ["A tragedy", "A play"]},
  {"entity": "Romeo and Juliet", "entity_attributes": ["A tragedy", "Written by Shakespeare"]},
  {"entity": "William Shakespeare", "entity_attributes": ["Playwright"]},
  {"entity1": "Romeo and Juliet", "entity2": "William Shakespeare", "relation": "Author"},
  {"entity1": "Romeo and Juliet", "entity2": "Verona", "relation": "Location"},
  {"entity1": "Romeo and Juliet", "entity2": "Romeo and Juliet", "relation": "Person"},
  {"entity1": "Romeo and Juliet", "entity2": "William Shakespeare", "relation": "Unknown"}
]
```"""
        else:
            content = "这是一段与标签相关的示例文本，包含专业术语和典型场景。"
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    monkeypatch.setattr(infra_router.model_runtime, "chat_completion", fake_chat_completion)

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": f"extract-chat-{suffix}",
                    "type": "KnowledgeQA",
                    "source": "remote",
                    "parameters": {"base_url": "https://extract.test/v1", "api_key": "extract-secret"},
                },
            )
            assert created.status_code == 201, created.text
            model_id = created.json()["data"]["id"]

            tags = await client.post("/api/v1/initialization/extract/fabri-tag", headers=headers, json={})
            assert tags.status_code == 200, tags.text
            generated_tags = tags.json()["data"]["tags"]
            assert 1 <= len(generated_tags) <= 9
            assert set(generated_tags) <= {
                "Content", "Culture", "Person", "Event", "Time", "Location", "Work", "Author", "Relation", "Attribute"
            }

            fabricated = await client.post(
                "/api/v1/initialization/extract/fabri-text",
                headers=headers,
                json={"tags": generated_tags, "model_id": model_id},
            )
            assert fabricated.status_code == 200, fabricated.text
            assert fabricated.json()["data"]["text"].startswith("这是一段")
            assert generated_tags[0] in calls[-1]["messages"][0]["content"]
            assert calls[-1]["options"]["temperature"] == 0.3
            assert calls[-1]["options"]["max_tokens"] == 4096

            extracted = await client.post(
                "/api/v1/initialization/extract/text-relation",
                headers=headers,
                json={"text": "Romeo and Juliet was written by William Shakespeare in Verona.", "tags": ["Author", "Location"], "model_id": model_id},
            )
            assert extracted.status_code == 200, extracted.text
            graph = extracted.json()["data"]
            romeo = next(node for node in graph["nodes"] if node["name"] == "Romeo and Juliet")
            assert romeo["attributes"] == ["A tragedy", "A play", "Written by Shakespeare"]
            assert any(node["name"] == "Verona" for node in graph["nodes"])
            assert graph["relations"] == [
                {"node1": "Romeo and Juliet", "node2": "William Shakespeare", "type": "Author"},
                {"node1": "Romeo and Juliet", "node2": "Verona", "type": "Location"},
            ]
            assert calls[-1]["options"]["max_tokens"] == 8192
            assert "# Examples" in calls[-1]["messages"][0]["content"]

    _run(case)


def test_chunker_preview_adaptive_strategies_and_limits() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            empty = await client.post(
                "/api/v1/chunker/preview",
                headers=headers,
                json={"text": "  \n\t  "},
            )
            assert empty.status_code == 400, empty.text
            assert "empty" in empty.json()["error"]["message"]

            oversized = await client.post(
                "/api/v1/chunker/preview",
                headers=headers,
                json={"text": "a" * (64 * 1024 + 1)},
            )
            assert oversized.status_code == 413, oversized.text
            assert oversized.json()["limit"] == 64 * 1024

            text = "# Top\nintro paragraph here.\n\n## Section A\nbody A.\n\n## Section B\nbody B."
            response = await client.post(
                "/api/v1/chunker/preview",
                headers=headers,
                json={
                    "text": text,
                    "chunking_config": {
                        "chunk_size": 200,
                        "chunk_overlap": 20,
                        "separators": ["\n\n", "\n"],
                        "strategy": "auto",
                    },
                },
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["selected_tier"] in {"heading", "heuristic", "legacy"}
            assert data["tier_chain"][-1] == "legacy"
            assert data["profile"]["md_heading_total"] == 3
            assert data["stats"]["count"] == len(data["chunks"])
            assert data["stats"]["count"] > 0
            for chunk in data["chunks"]:
                assert text[chunk["start"] : chunk["end"]] == chunk["content"]
                assert chunk["size_chars"] == len(chunk["content"])

            legacy = await client.post(
                "/api/v1/chunker/preview",
                headers=headers,
                json={
                    "text": "para one.\n\npara two.\n\npara three.\n\npara four.",
                    "chunking_config": {
                        "chunk_size": 100,
                        "chunk_overlap": 10,
                        "separators": ["\n\n"],
                        "strategy": "legacy",
                    },
                },
            )
            assert legacy.status_code == 200, legacy.text
            assert legacy.json()["data"]["selected_tier"] == "legacy"
            assert legacy.json()["data"]["profile"] is not None

            many = await client.post(
                "/api/v1/chunker/preview",
                headers=headers,
                json={
                    "text": "x.\n\n" * 550,
                    "chunking_config": {
                        "chunk_size": 3,
                        "separators": ["\n\n"],
                        "strategy": "legacy",
                    },
                },
            )
            assert many.status_code == 200, many.text
            assert len(many.json()["data"]["chunks"]) == 500
            assert many.json()["data"]["stats"]["truncated_to"] > 500

    _run(case)


def test_knowledge_tag_post_and_faq_import_progress_are_persisted() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])

            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"faq-{uuid.uuid4().hex[:8]}", "type": "faq"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            tag = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/tags",
                headers=headers,
                json={"name": "release", "color": "#2288ff"},
            )
            assert tag.status_code == 201, tag.text
            tag_id = tag.json()["data"]["id"]
            knowledge = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=headers,
                json={"title": "manual", "content": "tagged content"},
            )
            assert knowledge.status_code == 201, knowledge.text
            knowledge_id = knowledge.json()["data"]["id"]

            tagged = await client.post(
                "/api/v1/knowledge/tags",
                headers=headers,
                json={"updates": {knowledge_id: [tag_id]}, "kb_id": kb_id},
            )
            assert tagged.status_code == 200, tagged.text
            loaded = await client.get(f"/api/v1/knowledge/{knowledge_id}", headers=headers)
            assert loaded.status_code == 200, loaded.text
            assert [item["id"] for item in loaded.json()["data"]["tags"]] == [tag_id]

            task_id = str(uuid.uuid4())
            now = db.utcnow()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO py_task_results(id, tenant_id, kind, status, total, processed, payload, result, error, created_at, updated_at) "
                    "VALUES (?, ?, 'faq_import', 'completed', 2, 2, ?, ?, '', ?, ?)",
                    (
                        task_id,
                        tenant_id,
                        json.dumps({"kb_id": kb_id}),
                        json.dumps({"task_id": task_id, "kb_id": kb_id, "status": "completed", "progress": 100, "display_status": "open"}),
                        now,
                        now,
                    ),
                )
            progress = await client.get(f"/api/v1/faq/import/progress/{task_id}", headers=headers)
            assert progress.status_code == 200, progress.text
            assert progress.json()["data"]["total"] == 2
            assert progress.json()["data"]["display_status"] == "open"

            hidden = await client.put(
                f"/api/v1/knowledge-bases/{kb_id}/faq/import/last-result/display",
                headers=headers,
                json={"display_status": "close"},
            )
            assert hidden.status_code == 200, hidden.text
            progress = await client.get(f"/api/v1/faq/import/progress/{task_id}", headers=headers)
            assert progress.json()["data"]["display_status"] == "close"

            missing = await client.get(f"/api/v1/faq/import/progress/{uuid.uuid4()}", headers=headers)
            assert missing.status_code == 404

    _run(case)


def test_faq_import_runs_async_with_dry_run_and_progress() -> None:
    async def wait_for_task(client, headers, task_id):
        for _ in range(100):
            response = await client.get(f"/api/v1/faq/import/progress/{task_id}", headers=headers)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            if data["status"] in {"completed", "failed"}:
                return data
            await asyncio.sleep(0.01)
        raise AssertionError("FAQ import task did not finish")

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"faq-import-{uuid.uuid4().hex[:8]}", "type": "faq"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            tag = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/tags",
                headers=headers,
                json={"name": "faq-import-tag"},
            )
            assert tag.status_code == 201, tag.text
            tag_seq_id = tag.json()["data"]["seq_id"]

            dry_run = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries",
                headers=headers,
                json={
                    "dry_run": True,
                    "entries": [
                        {"standard_question": "How do I reset it?", "answer": "Hold the button.", "tag_id": tag_seq_id},
                        {"standard_question": "How do I reset it?", "answer": "Duplicate."},
                        {"standard_question": "Missing answer"},
                    ],
                },
            )
            assert dry_run.status_code == 200, dry_run.text
            dry_run_id = dry_run.json()["data"]["task_id"]
            assert db.fetch_one(
                "SELECT max_retry FROM py_task_results WHERE id=?", (dry_run_id,)
            )["max_retry"] == 3
            data = await wait_for_task(client, headers, dry_run_id)
            assert data["dry_run"] is True
            assert data["progress"] == 100
            assert data["total"] == 3
            assert data["success_count"] == 1
            assert data["failed_count"] == 2
            listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/faq/entries", headers=headers)
            assert listed.json()["total"] == 0

            imported = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries",
                headers=headers,
                json={
                    "entries": [
                        {"standard_question": "How do I reset it?", "answer": "Hold the button.", "tag_id": tag_seq_id},
                        {"standard_question": "Where is the manual?", "answer": "In the box."},
                    ]
                },
            )
            assert imported.status_code == 200, imported.text
            imported_id = imported.json()["data"]["task_id"]
            assert db.fetch_one(
                "SELECT max_retry FROM py_task_results WHERE id=?", (imported_id,)
            )["max_retry"] == 5
            data = await wait_for_task(client, headers, imported_id)
            assert data["status"] == "completed"
            assert data["success_count"] == 2
            listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/faq/entries", headers=headers)
            assert listed.json()["total"] == 2
            assert any(item["tag_id"] == tag_seq_id for item in listed.json()["data"])

            replaced = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries",
                headers=headers,
                json={
                    "mode": "replace",
                    "entries": [{"standard_question": "New question?", "answer": "New answer."}],
                },
            )
            assert replaced.status_code == 200, replaced.text
            data = await wait_for_task(client, headers, replaced.json()["data"]["task_id"])
            assert data["success_count"] == 1
            listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/faq/entries", headers=headers)
            assert listed.json()["total"] == 1
            assert listed.json()["data"][0]["standard_question"] == "New question?"

    _run(case)


def test_faq_kb_copy_clones_entries_and_tags() -> None:
    async def wait_for_copy(client, headers, task_id):
        for _ in range(500):
            response = await client.get(
                f"/api/v1/knowledge-bases/copy/progress/{task_id}", headers=headers
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            if data["status"] in {"completed", "failed"}:
                return data
            await asyncio.sleep(0.01)
        raise AssertionError("FAQ knowledge base copy did not finish")

    async def case() -> None:
        suffix = uuid.uuid4().hex[:8]
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            source = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"faq-copy-source-{suffix}", "type": "faq"},
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]
            source_tag = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/tags",
                headers=headers,
                json={"name": f"faq-copy-tag-{suffix}", "color": "#2288ff"},
            )
            assert source_tag.status_code == 201, source_tag.text
            source_tag_seq = source_tag.json()["data"]["seq_id"]
            for payload in (
                {
                    "standard_question": "How do I reset it?",
                    "answer": "Hold the button.",
                    "similar_questions": ["reset steps", "restart"],
                    "tag_id": source_tag_seq,
                    "is_enabled": True,
                    "is_recommended": True,
                },
                {
                    "standard_question": "Where is the manual?",
                    "answer": "In the box.",
                    "is_enabled": False,
                },
            ):
                entry = await client.post(
                    f"/api/v1/knowledge-bases/{source_id}/faq/entry",
                    headers=headers,
                    json=payload,
                )
                assert entry.status_code == 201, entry.text

            target = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"faq-copy-target-{suffix}", "type": "faq"},
            )
            assert target.status_code == 201, target.text
            target_id = target.json()["data"]["id"]

            started = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": target_id},
            )
            assert started.status_code == 200, started.text
            progress = await wait_for_copy(client, headers, started.json()["data"]["task_id"])
            assert progress["status"] == "completed", progress
            assert progress["total"] == 2
            assert progress["processed"] == 2

            target_tags = await client.get(
                f"/api/v1/knowledge-bases/{target_id}/tags", headers=headers
            )
            assert target_tags.status_code == 200, target_tags.text
            copied_tag = next(
                tag for tag in target_tags.json()["data"]
                if tag["name"] == f"faq-copy-tag-{suffix}"
            )
            assert copied_tag["id"] != source_tag.json()["data"]["id"]
            assert copied_tag["seq_id"] != source_tag_seq

            target_entries = await client.get(
                f"/api/v1/knowledge-bases/{target_id}/faq/entries", headers=headers
            )
            assert target_entries.status_code == 200, target_entries.text
            assert target_entries.json()["total"] == 2
            by_question = {
                item["standard_question"]: item
                for item in target_entries.json()["data"]
            }
            copied_entry = by_question["How do I reset it?"]
            assert copied_entry["answer"] == "Hold the button."
            assert copied_entry["similar_questions"] == ["reset steps", "restart"]
            assert copied_entry["tag_id"] == copied_tag["seq_id"]
            assert copied_entry["is_enabled"] is True
            assert copied_entry["is_recommended"] is True
            assert by_question["Where is the manual?"]["is_enabled"] is False

            rerun = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": target_id},
            )
            assert rerun.status_code == 200, rerun.text
            progress = await wait_for_copy(client, headers, rerun.json()["data"]["task_id"])
            assert progress["status"] == "completed", progress
            listed = await client.get(
                f"/api/v1/knowledge-bases/{target_id}/faq/entries", headers=headers
            )
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] == 2

    _run(case)


def test_faq_import_respects_admin_cancellation(monkeypatch) -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=headers,
                json={"name": f"faq-cancel-{uuid.uuid4().hex[:8]}", "type": "faq"},
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]

            original_worker = knowledge_router._run_faq_import

            async def hold_worker(task_id: str) -> None:
                return

            monkeypatch.setattr(knowledge_router, "_run_faq_import", hold_worker)
            created = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries",
                headers=headers,
                json={"entries": [{"standard_question": "Pending?", "answer": "No."}]},
            )
            assert created.status_code == 200, created.text
            pending_id = created.json()["data"]["task_id"]
            cancelled = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{pending_id}/actions/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200, cancelled.text

            started = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries",
                headers=headers,
                json={"entries": [{"standard_question": "Processing?", "answer": "No."}]},
            )
            assert started.status_code == 200, started.text
            processing_id = started.json()["data"]["task_id"]
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE py_task_results SET status='processing', updated_at=? WHERE id=?",
                    (db.utcnow(), processing_id),
                )
            cancelled = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{processing_id}/actions/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200, cancelled.text

            monkeypatch.setattr(knowledge_router, "_run_faq_import", original_worker)
            await original_worker(pending_id)
            await original_worker(processing_id)

            rows = db.fetch_all(
                "SELECT status FROM py_task_results WHERE id IN (?, ?) ORDER BY id",
                (pending_id, processing_id),
            )
            assert [row["status"] for row in rows] == ["cancelled", "cancelled"]
            listed = await client.get(f"/api/v1/knowledge-bases/{kb_id}/faq/entries", headers=headers)
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] == 0

    _run(case)


def test_approval_resolution_survives_local_cache_loss() -> None:
    async def case() -> None:
        pending = approvals.request_tool(
            tenant_id=7,
            user_id="alice",
            session_id="session-1",
            assistant_message_id="assistant-1",
            request_id="request-1",
            service_id="service-1",
            service_name="Shared MCP",
            mcp_tool_name="lookup",
            registered_tool_name="mcp_shared_lookup",
            description="lookup",
            arguments={"query": "original"},
            tool_call_id="call-1",
            timeout_seconds=5,
        )
        approvals._pending.clear()
        resolved = approvals.resolve(
            pending.id,
            approvals.Decision(approved=True, modified_args={"query": "updated"}),
            kind="tool",
            tenant_id=7,
            user_id="alice",
        )
        assert resolved.id == pending.id
        decision = await approvals.wait(pending)
        assert decision.approved is True
        assert decision.modified_args == {"query": "updated"}
        assert db.fetch_one("SELECT status FROM py_pending_approvals WHERE id=?", (pending.id,))["status"] == "resolved"
        with pytest.raises(approvals.AlreadyResolved):
            approvals.resolve(
                pending.id,
                approvals.Decision(approved=False),
                kind="tool",
                tenant_id=7,
                user_id="alice",
            )

    _run(case)


def test_temporary_attachment_detail_and_delete_require_owner() -> None:
    async def case() -> None:
        async with await _client() as client:
            owner = await _login(client)
            owner_headers = _headers(owner)
            session = await client.post(
                "/api/v1/sessions", headers=owner_headers, json={"title": "attachment auth"}
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]

            uploaded = await client.post(
                f"/api/v1/sessions/{session_id}/attachments",
                headers=owner_headers,
                files={"file": ("note.txt", b"private attachment", "text/plain")},
            )
            assert uploaded.status_code == 201, uploaded.text
            attachment_id = uploaded.json()["data"][0]["id"]

            suffix = uuid.uuid4().hex[:10]
            guest_credentials = {
                "username": "attachment-guest-" + suffix,
                "email": f"attachment-guest-{suffix}@example.com",
                "password": "GuestPass123",
            }
            registered = await client.post("/api/v1/auth/register", json=guest_credentials)
            assert registered.status_code == 201, registered.text
            guest = await _login(
                client,
                {"email": guest_credentials["email"], "password": guest_credentials["password"]},
            )
            guest_headers = _headers(guest)

            for path in (
                f"/api/v1/sessions/{session_id}/attachments/{attachment_id}",
                f"/api/v1/sessions/{session_id}/attachments/{attachment_id}/preview",
            ):
                anonymous = await client.get(path)
                assert anonymous.status_code == 401
                denied = await client.get(path, headers=guest_headers)
                assert denied.status_code == 404

            denied_delete = await client.delete(
                f"/api/v1/sessions/{session_id}/attachments/{attachment_id}",
                headers=guest_headers,
            )
            assert denied_delete.status_code == 404
            still_there = await client.get(
                f"/api/v1/sessions/{session_id}/attachments/{attachment_id}",
                headers=owner_headers,
            )
            assert still_there.status_code == 200

            deleted = await client.delete(
                f"/api/v1/sessions/{session_id}/attachments/{attachment_id}",
                headers=owner_headers,
            )
            assert deleted.status_code == 200
            gone = await client.get(
                f"/api/v1/sessions/{session_id}/attachments/{attachment_id}",
                headers=owner_headers,
            )
            assert gone.status_code == 404

    _run(case)


def test_message_artifacts_and_suggestions_require_owner() -> None:
    async def case() -> None:
        async with await _client() as client:
            owner = await _login(client)
            owner_headers = _headers(owner)
            session = await client.post(
                "/api/v1/sessions", headers=owner_headers, json={"title": "artifact auth"}
            )
            assert session.status_code == 201, session.text
            session_id = session.json()["data"]["id"]
            message_id = str(uuid.uuid4())
            artifact_id = str(uuid.uuid4())
            source = Path(_DATA_DIR) / f"artifact-{artifact_id}.txt"
            source.write_text("private artifact", encoding="utf-8")
            now = db.utcnow()
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO messages(id, request_id, session_id, role, content, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'assistant', 'artifact reply', ?, ?)",
                    (message_id, str(uuid.uuid4()), session_id, now, now),
                )
                conn.execute(
                    "INSERT INTO message_artifacts(id, session_id, message_id, position, file_name, source_path, created_at) "
                    "VALUES (?, ?, ?, 0, 'artifact.txt', ?, ?)",
                    (artifact_id, session_id, message_id, str(source), now),
                )

            suffix = uuid.uuid4().hex[:10]
            guest_credentials = {
                "username": "artifact-guest-" + suffix,
                "email": f"artifact-guest-{suffix}@example.com",
                "password": "GuestPass123",
            }
            registered = await client.post("/api/v1/auth/register", json=guest_credentials)
            assert registered.status_code == 201, registered.text
            guest = await _login(
                client,
                {"email": guest_credentials["email"], "password": guest_credentials["password"]},
            )
            guest_headers = _headers(guest)
            artifact_path = (
                f"/api/v1/sessions/{session_id}/messages/{message_id}/artifacts"
            )
            download_path = artifact_path + "/0/download"
            suggestions_path = (
                f"/api/v1/sessions/{session_id}/messages/{message_id}/suggestions"
            )

            for method, path in (
                ("get", artifact_path),
                ("get", download_path),
                ("get", suggestions_path),
            ):
                anonymous = await client.request(method, path)
                assert anonymous.status_code == 401
                denied = await client.request(method, path, headers=guest_headers)
                assert denied.status_code == 404

            denied_delete = await client.delete(download_path.rsplit("/download", 1)[0], headers=guest_headers)
            assert denied_delete.status_code == 404
            denied_ensure = await client.post(suggestions_path, headers=guest_headers)
            assert denied_ensure.status_code == 404

            listed = await client.get(artifact_path, headers=owner_headers)
            assert listed.status_code == 200
            assert [row["id"] for row in listed.json()["data"]] == [artifact_id]
            downloaded = await client.get(download_path, headers=owner_headers)
            assert downloaded.status_code == 200
            assert downloaded.content == b"private artifact"
            ensured = await client.post(suggestions_path, headers=owner_headers)
            assert ensured.status_code == 200
            suggestions = await client.get(suggestions_path, headers=owner_headers)
            assert suggestions.status_code == 200
            assert suggestions.json()["data"]["questions"]
            deleted = await client.delete(download_path.rsplit("/download", 1)[0], headers=owner_headers)
            assert deleted.status_code == 200

    _run(case)


def test_viewer_configuration_endpoints_require_authentication() -> None:
    async def case() -> None:
        async with await _client() as client:
            for method, path in (
                ("GET", "/api/v1/agents/placeholders"),
                ("GET", "/api/v1/agents/type-presets"),
                ("GET", "/api/v1/models/providers"),
                ("GET", "/api/v1/models/catalog/resolve"),
                ("POST", "/api/v1/models/catalog/resolve"),
                ("GET", "/api/v1/storage-backends/types"),
                ("GET", "/api/v1/vector-stores/types"),
                ("GET", "/api/v1/web-search-providers/types"),
                ("GET", "/api/v1/datasource/types"),
                ("GET", "/api/v1/mcp-endpoints/tools"),
                ("GET", "/api/v1/system/info"),
                ("GET", "/api/v1/system/host-project-dir"),
                ("GET", "/api/v1/knowledge-bases/copy/progress/task-1"),
                ("GET", "/api/v1/chunker/preview?text=hello"),
                ("GET", "/api/v1/image-attrs/schema"),
                ("POST", "/api/v1/wechat/qrcode"),
                ("POST", "/api/v1/wechat/qrcode/status"),
                ("POST", "/api/v1/initialization/extract/fabri-tag"),
            ):
                response = await client.request(method, path, json={} if method == "POST" else None)
                assert response.status_code == 401, (method, path, response.text)

    _run(case)


def test_viewer_cannot_manage_sandbox_configs() -> None:
    async def case() -> None:
        async with await _client() as client:
            owner = await _login(client)
            owner_headers = _headers(owner)
            tenant_id = owner["active_tenant"]["id"]

            suffix = uuid.uuid4().hex[:10]
            credentials = {
                "username": "sandbox-viewer-" + suffix,
                "email": f"sandbox-viewer-{suffix}@example.com",
                "password": "ViewerPass123",
            }
            registered = await client.post("/api/v1/auth/register", json=credentials)
            assert registered.status_code == 201, registered.text
            viewer_id = registered.json()["user"]["id"]
            with db.transaction() as conn:
                conn.execute("UPDATE users SET tenant_id=? WHERE id=?", (tenant_id, viewer_id))
                conn.execute(
                    "UPDATE tenant_members SET tenant_id=?, role='viewer' WHERE user_id=?",
                    (tenant_id, viewer_id),
                )
            viewer = await _login(
                client, {"email": credentials["email"], "password": credentials["password"]}
            )
            viewer_headers = _headers(viewer)
            assert viewer["active_tenant"]["id"] == tenant_id

            listed = await client.get("/api/v1/sandbox-configs", headers=viewer_headers)
            assert listed.status_code == 200, listed.text
            denied_create = await client.post(
                "/api/v1/sandbox-configs",
                headers=viewer_headers,
                json={"name": "Viewer sandbox", "sandbox_type": "cube", "config": {"cube": {}}},
            )
            assert denied_create.status_code == 403

            created = await client.post(
                "/api/v1/sandbox-configs",
                headers=owner_headers,
                json={"name": "Admin sandbox", "sandbox_type": "cube", "config": {"cube": {}}},
            )
            assert created.status_code == 201, created.text
            config_id = created.json()["data"]["id"]
            denied_inventory = await client.get(
                f"/api/v1/sandbox-configs/{config_id}/sandboxes", headers=viewer_headers
            )
            assert denied_inventory.status_code == 403

    _run(case)


def test_sandbox_template_query_lists_provider_catalog(monkeypatch) -> None:
    seen_keys: list[str] = []

    def fake_list_templates(self):
        seen_keys.append(self.api_key)
        return [
            {
                "id": "tpl-readable",
                "name": "weknora",
                "status": "ready",
                "standard": True,
            },
            {
                "id": "tpl-building",
                "name": "weknora-desktop",
                "status": "building",
                "desktop": True,
            },
        ]

    monkeypatch.setattr(
        remote_sandbox.RemoteSandboxClient, "list_templates", fake_list_templates
    )

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            config_name = f"Catalog sandbox {uuid.uuid4().hex[:8]}"
            created = await client.post(
                "/api/v1/sandbox-configs",
                headers=headers,
                json={
                    "name": config_name,
                    "sandbox_type": "cube",
                    "config": {
                        "sandbox_type": "cube",
                        "cube": {
                            "api_url": "https://cube.test",
                            "api_key": "stored-secret",
                        },
                    },
                },
            )
            assert created.status_code == 201, created.text
            config_id = created.json()["data"]["id"]
            queried = await client.post(
                "/api/v1/sandbox-configs/templates/query",
                headers=headers,
                json={
                    "config_id": config_id,
                    "config": {"cube": {"api_key": "***"}},
                },
            )
            assert queried.status_code == 200, queried.text
            data = queried.json()["data"]
            assert seen_keys == ["stored-secret"]
            assert data["standard_template_id"] == "tpl-readable"
            assert data["desktop_template_id"] == "tpl-building"
            assert data["provisioned"] is False

    _run(case)


def test_sandbox_template_query_ensures_and_replaces_templates(monkeypatch) -> None:
    events: list[tuple[str, str, bool] | tuple[str, str, str]] = []

    class FakeClient:
        def __init__(self, provider, backend, config_data):
            assert provider == "cube"

        def list_templates(self):
            return []

        def ensure_template(self, kind, *, replace=False):
            events.append(("ensure", kind, replace))
            return {
                "id": "tpl-new",
                "name": "weknora",
                "status": "ready" if replace else "building",
                "standard": kind == "standard",
            }

        def delete_superseded_templates(self, kind, keep_id):
            events.append(("delete", kind, keep_id))

    monkeypatch.setattr(remote_sandbox, "RemoteSandboxClient", FakeClient)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/sandbox-configs",
                headers=headers,
                json={
                    "name": f"Template build sandbox {uuid.uuid4().hex[:8]}",
                    "sandbox_type": "cube",
                    "config": {
                        "sandbox_type": "cube",
                        "cube": {
                            "api_url": "https://cube.test",
                            "api_key": "stored-secret",
                            "template_id": "tpl-old",
                        },
                    },
                },
            )
            assert created.status_code == 201, created.text
            config_id = created.json()["data"]["id"]

            ensured = await client.post(
                "/api/v1/sandbox-configs/templates/query",
                headers=headers,
                json={
                    "config_id": config_id,
                    "config": {"cube": {"api_key": "***"}},
                    "ensure_standard": True,
                },
            )
            assert ensured.status_code == 200, ensured.text
            ensured_data = ensured.json()["data"]
            assert ensured_data["standard_template_id"] == "tpl-new"
            assert ensured_data["provisioned"] is True

            replaced = await client.post(
                "/api/v1/sandbox-configs/templates/query",
                headers=headers,
                json={
                    "config_id": config_id,
                    "config": {"cube": {"api_key": "***"}},
                    "replace_standard": True,
                },
            )
            assert replaced.status_code == 200, replaced.text
            replaced_data = replaced.json()["data"]
            assert replaced_data["standard_template_id"] == "tpl-new"
            assert replaced_data["provisioned"] is True

            fetched = await client.get(f"/api/v1/sandbox-configs/{config_id}", headers=headers)
            assert fetched.status_code == 200, fetched.text
            assert fetched.json()["data"]["config"]["cube"]["template_id"] == "tpl-new"
            assert events == [
                ("ensure", "standard", False),
                ("ensure", "standard", True),
                ("delete", "standard", "tpl-new"),
            ]

    _run(case)


def test_sandbox_template_query_lists_and_ensures_docker_images(monkeypatch) -> None:
    ensure_calls: list[tuple[str, bool]] = []
    list_calls = 0

    class FakeDockerClient:
        def __init__(self, backend):
            assert backend["image"] == "wechatopenai/weknora-sandbox:main"

        def list_templates(self):
            nonlocal list_calls
            list_calls += 1
            if list_calls > 1:
                return []
            return [
                {
                    "id": "wechatopenai/weknora-sandbox:main",
                    "name": "wechatopenai/weknora-sandbox:main",
                    "image": "wechatopenai/weknora-sandbox:main",
                    "status": "ready",
                    "standard": True,
                }
            ]

        def ensure_template(self, kind, *, replace=False):
            ensure_calls.append((kind, replace))
            return {
                "id": "wechatopenai/weknora-sandbox:main",
                "name": "wechatopenai/weknora-sandbox:main",
                "image": "wechatopenai/weknora-sandbox:main",
                "status": "ready",
                "standard": True,
            }

    monkeypatch.setattr(docker_templates, "DockerTemplateClient", FakeDockerClient)

    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            created = await client.post(
                "/api/v1/sandbox-configs",
                headers=headers,
                json={
                    "name": f"Docker catalog {uuid.uuid4().hex[:8]}",
                    "sandbox_type": "docker",
                    "config": {
                        "sandbox_type": "docker",
                        "docker": {"image": "wechatopenai/weknora-sandbox:main"},
                    },
                },
            )
            assert created.status_code == 201, created.text
            config_id = created.json()["data"]["id"]

            listed = await client.post(
                "/api/v1/sandbox-configs/templates/query",
                headers=headers,
                json={"config_id": config_id, "config": {}},
            )
            assert listed.status_code == 200, listed.text
            assert listed.json()["data"]["standard_template_id"] == (
                "wechatopenai/weknora-sandbox:main"
            )
            assert listed.json()["data"]["provisioned"] is False

            ensured = await client.post(
                "/api/v1/sandbox-configs/templates/query",
                headers=headers,
                json={"config_id": config_id, "config": {}, "ensure_standard": True},
            )
            assert ensured.status_code == 200, ensured.text
            assert ensured.json()["data"]["provisioned"] is True
            assert ensure_calls == [("standard", False)]

    _run(case)


def test_viewer_cannot_manage_tenant_infrastructure() -> None:
    async def case() -> None:
        async with await _client() as client:
            owner = await _login(client)
            tenant_id = owner["active_tenant"]["id"]
            viewer_headers = await _login_viewer(client, tenant_id)
            contributor_headers = await _login_tenant_member(client, tenant_id, "contributor")

            organization = await client.post(
                "/api/v1/organizations",
                headers=_headers(owner),
                json={"name": "Viewer permission organization"},
            )
            assert organization.status_code == 201, organization.text
            org_id = organization.json()["data"]["id"]
            knowledge_base = await client.post(
                "/api/v1/knowledge-bases",
                headers=_headers(owner),
                json={"name": "Viewer permission knowledge base"},
            )
            assert knowledge_base.status_code == 201, knowledge_base.text
            kb_id = knowledge_base.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual",
                headers=_headers(owner),
                json={"title": "Viewer permission manual", "content": "Protected knowledge content."},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]
            chunks = await client.get(f"/api/v1/chunks/{knowledge_id}", headers=_headers(owner))
            assert chunks.status_code == 200, chunks.text
            chunk_id = chunks.json()["data"][0]["id"]
            download = await client.get(f"/api/v1/knowledge/{knowledge_id}/download", headers=_headers(owner))
            assert download.status_code == 200, download.text
            batch_download = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/batch-download",
                headers=_headers(owner),
                json={"ids": [knowledge_id]},
            )
            assert batch_download.status_code == 200, batch_download.text
            tag = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/tags",
                headers=_headers(owner),
                json={"name": "viewer-denied"},
            )
            assert tag.status_code == 201, tag.text
            tag_id = tag.json()["data"]["id"]
            faq_entry = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/faq/entry",
                headers=_headers(owner),
                json={"standard_question": "Protected question?", "answer": "Protected answer."},
            )
            assert faq_entry.status_code == 201, faq_entry.text
            faq_entry_id = faq_entry.json()["data"]["id"]
            wiki_page = await client.post(
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages",
                headers=_headers(owner),
                json={"title": "Viewer permission wiki", "content": "Protected wiki content."},
            )
            assert wiki_page.status_code == 201, wiki_page.text
            wiki_slug = wiki_page.json()["data"]["slug"]
            wiki_folder = await client.post(
                f"/api/v1/knowledgebase/{kb_id}/wiki/folders",
                headers=_headers(owner),
                json={"name": "Viewer permission folder"},
            )
            assert wiki_folder.status_code == 201, wiki_folder.text
            wiki_folder_id = wiki_folder.json()["data"]["id"]
            agent = await client.post(
                "/api/v1/agents",
                headers=_headers(owner),
                json={"name": "Viewer permission agent"},
            )
            assert agent.status_code == 201, agent.text
            agent_id = agent.json()["data"]["id"]
            im_channel = await client.post(
                f"/api/v1/agents/{agent_id}/im-channels",
                headers=_headers(owner),
                json={"platform": "wecom", "credentials": {"api_key": "tenant-secret"}},
            )
            assert im_channel.status_code == 200, im_channel.text
            im_channel_id = im_channel.json()["data"]["id"]
            other_suffix = uuid.uuid4().hex[:10]
            other_credentials = {
                "username": "other-admin-" + other_suffix,
                "email": f"other-admin-{other_suffix}@example.com",
                "password": "OtherAdminPass123",
            }
            registered_other = await client.post("/api/v1/auth/register", json=other_credentials)
            assert registered_other.status_code == 201, registered_other.text
            other_admin = await _login(
                client,
                {"email": other_credentials["email"], "password": other_credentials["password"]},
            )

            denied_writes = (
                ("POST", "/api/v1/models"),
                ("PUT", "/api/v1/models/missing"),
                ("DELETE", "/api/v1/models/missing"),
                ("POST", "/api/v1/models/missing/debug"),
                ("POST", "/api/v1/models/missing/copy"),
                ("PUT", "/api/v1/models/missing/credentials"),
                ("DELETE", "/api/v1/models/missing/credentials/api_key"),
                ("POST", "/api/v1/weknoracloud/credentials"),
                ("POST", "/api/v1/storage-backends"),
                ("PUT", "/api/v1/storage-backends/missing"),
                ("DELETE", "/api/v1/storage-backends/missing"),
                ("POST", "/api/v1/storage-backends/test"),
                ("POST", "/api/v1/storage-backends/missing/test"),
                ("PUT", "/api/v1/storage-backends/missing/default"),
                ("POST", "/api/v1/vector-stores"),
                ("PUT", "/api/v1/vector-stores/missing"),
                ("DELETE", "/api/v1/vector-stores/missing"),
                ("POST", "/api/v1/vector-stores/test"),
                ("POST", "/api/v1/vector-stores/missing/test"),
                ("POST", "/api/v1/web-search-providers"),
                ("PUT", "/api/v1/web-search-providers/missing"),
                ("PUT", "/api/v1/web-search-providers/missing/credentials"),
                ("DELETE", "/api/v1/web-search-providers/missing/credentials/api_key"),
                ("DELETE", "/api/v1/web-search-providers/missing"),
                ("POST", "/api/v1/web-search-providers/test"),
                ("POST", "/api/v1/web-search-providers/missing/test"),
                ("POST", "/api/v1/datasource"),
                ("PUT", "/api/v1/datasource/missing"),
                ("PUT", "/api/v1/datasource/missing/credentials"),
                ("DELETE", "/api/v1/datasource/missing/credentials/credentials"),
                ("DELETE", "/api/v1/datasource/missing"),
                ("POST", "/api/v1/datasource/validate-credentials"),
                ("POST", "/api/v1/datasource/missing/validate"),
                ("POST", "/api/v1/datasource/missing/sync"),
                ("POST", "/api/v1/datasource/missing/pause"),
                ("POST", "/api/v1/datasource/missing/resume"),
                ("GET", "/api/v1/datasource/missing/resources"),
                ("POST", "/api/v1/datasource/missing/resource-ancestors"),
                ("POST", "/api/v1/system/parser-engines/check"),
                ("POST", "/api/v1/system/docreader/reconnect"),
                ("POST", "/api/v1/mcp-endpoints"),
                ("PUT", "/api/v1/mcp-endpoints/missing"),
                ("DELETE", "/api/v1/mcp-endpoints/missing"),
                ("POST", "/api/v1/mcp-endpoints/missing/rotate-token"),
                ("POST", "/api/v1/mcp-services"),
                ("PUT", "/api/v1/mcp-services/missing"),
                ("DELETE", "/api/v1/mcp-services/missing"),
                ("PUT", "/api/v1/mcp-services/missing/tools/demo"),
                ("PUT", "/api/v1/mcp-services/missing/tool-approvals/demo"),
                ("POST", "/api/v1/mcp-services/missing/usage-instructions/generate"),
                ("POST", "/api/v1/mcp-services/missing/test"),
                ("POST", "/api/v1/mcp-services/missing/metadata/refresh"),
                ("PUT", "/api/v1/mcp-services/missing/credentials"),
                ("DELETE", "/api/v1/mcp-services/missing/credentials/api_key"),
                ("POST", "/api/v1/skills/catalog"),
                ("POST", "/api/v1/skills/catalog/missing/install"),
                ("DELETE", "/api/v1/skills/catalog/missing"),
                ("GET", "/api/v1/skills/catalog/missing/files"),
                ("GET", "/api/v1/skills/catalog/missing/files/content"),
                ("POST", "/api/v1/system/sandbox-check"),
                ("POST", "/api/v1/evaluation/"),
                ("POST", "/api/v1/wechat/qrcode"),
                ("POST", "/api/v1/wechat/qrcode/status"),
                ("POST", "/api/v1/initialization/extract/fabri-tag"),
                ("POST", "/api/v1/initialization/extract/fabri-text"),
                ("POST", "/api/v1/initialization/extract/text-relation"),
                ("PUT", f"/api/v1/tenants/{tenant_id}"),
                ("POST", f"/api/v1/tenants/{tenant_id}/members"),
                ("PUT", f"/api/v1/tenants/{tenant_id}/members/missing"),
                ("DELETE", f"/api/v1/tenants/{tenant_id}/members/missing"),
                ("POST", f"/api/v1/tenants/{tenant_id}/invitations"),
                ("POST", f"/api/v1/tenants/{tenant_id}/invite-links"),
                ("DELETE", f"/api/v1/tenants/{tenant_id}/invitations/1"),
                ("PUT", "/api/v1/tenants/kv/memory-config"),
                ("GET", f"/api/v1/tenants/{tenant_id}/api-principal-config"),
                ("PUT", f"/api/v1/tenants/{tenant_id}/api-principal-config"),
                ("POST", f"/api/v1/tenants/{tenant_id}/api-principal-test-token"),
                ("GET", f"/api/v1/tenants/{tenant_id}/api-keys"),
                ("POST", f"/api/v1/tenants/{tenant_id}/api-keys"),
                ("PUT", f"/api/v1/tenants/{tenant_id}/api-keys/1"),
                ("DELETE", f"/api/v1/tenants/{tenant_id}/api-keys/1"),
                ("GET", f"/api/v1/tenants/{tenant_id}/audit-log"),
                ("POST", "/api/v1/organizations"),
                ("POST", "/api/v1/organizations/join"),
                ("POST", "/api/v1/organizations/join-request"),
                ("POST", "/api/v1/organizations/join-by-id"),
                ("PUT", f"/api/v1/organizations/{org_id}"),
                ("DELETE", f"/api/v1/organizations/{org_id}"),
                ("POST", f"/api/v1/organizations/{org_id}/leave"),
                ("POST", f"/api/v1/organizations/{org_id}/invite-code"),
                ("POST", f"/api/v1/organizations/{org_id}/invite"),
                ("PUT", f"/api/v1/organizations/{org_id}/members/1"),
                ("DELETE", f"/api/v1/organizations/{org_id}/members/1"),
                ("GET", f"/api/v1/organizations/{org_id}/join-requests"),
                ("PUT", f"/api/v1/organizations/{org_id}/join-requests/1/review"),
                ("POST", f"/api/v1/organizations/{org_id}/request-upgrade"),
                ("GET", f"/api/v1/organizations/{org_id}/search-tenants"),
                ("GET", f"/api/v1/organizations/{org_id}/search-users"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/shares"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/shares/1"),
                ("DELETE", f"/api/v1/knowledge-bases/{kb_id}/shares/1"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}"),
                ("DELETE", f"/api/v1/knowledge-bases/{kb_id}"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/profile/generate"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/rebuild-index"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/knowledge/folders"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/knowledge/url"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/knowledge/batch-download"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/tags"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/tags/{tag_id}"),
                ("DELETE", f"/api/v1/knowledge-bases/{kb_id}/tags/{tag_id}"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/faq/entries"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/faq/entry"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/faq/entries/{faq_entry_id}"),
                ("DELETE", f"/api/v1/knowledge-bases/{kb_id}/faq/entries"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/faq/entries/fields"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/faq/entries/tags"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/faq/entries/{faq_entry_id}/similar-questions"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/faq/import/last-result/display"),
                ("PUT", f"/api/v1/knowledge/{knowledge_id}"),
                ("DELETE", f"/api/v1/knowledge/{knowledge_id}"),
                ("GET", f"/api/v1/knowledge/{knowledge_id}/download"),
                ("PUT", f"/api/v1/knowledge/manual/{knowledge_id}"),
                ("POST", f"/api/v1/knowledge/{knowledge_id}/reparse"),
                ("POST", f"/api/v1/knowledge/{knowledge_id}/cancel-parse"),
                ("PUT", f"/api/v1/knowledge/image/{knowledge_id}/{chunk_id}"),
                ("PUT", f"/api/v1/chunks/by-id/{chunk_id}/questions"),
                ("DELETE", f"/api/v1/chunks/by-id/{chunk_id}/questions?question_id=missing"),
                ("POST", f"/api/v1/chunks/by-id/{chunk_id}/questions/regenerate"),
                ("PUT", f"/api/v1/chunks/{knowledge_id}/{chunk_id}"),
                ("DELETE", f"/api/v1/chunks/{knowledge_id}/{chunk_id}"),
                ("DELETE", f"/api/v1/chunks/{chunk_id}"),
                ("POST", f"/api/v1/chunks/{knowledge_id}/{chunk_id}/revert"),
                ("POST", f"/api/v1/knowledgebase/{kb_id}/wiki/pages"),
                ("PUT", f"/api/v1/knowledgebase/{kb_id}/wiki/pages/{wiki_slug}"),
                ("DELETE", f"/api/v1/knowledgebase/{kb_id}/wiki/pages/{wiki_slug}"),
                ("POST", f"/api/v1/knowledgebase/{kb_id}/wiki/folders"),
                ("PUT", f"/api/v1/knowledgebase/{kb_id}/wiki/folders/{wiki_folder_id}"),
                ("DELETE", f"/api/v1/knowledgebase/{kb_id}/wiki/folders/{wiki_folder_id}"),
                ("PUT", f"/api/v1/knowledgebase/{kb_id}/wiki/move-page"),
                ("POST", f"/api/v1/knowledgebase/{kb_id}/wiki/revert"),
                ("PUT", f"/api/v1/knowledgebase/{kb_id}/wiki/issues/missing/status"),
                ("POST", f"/api/v1/knowledgebase/{kb_id}/wiki/rebuild-links"),
                ("POST", f"/api/v1/knowledgebase/{kb_id}/wiki/auto-fix"),
                ("POST", f"/api/v1/agents/{agent_id}/shares"),
                ("GET", f"/api/v1/agents/{agent_id}/shares"),
                ("PUT", f"/api/v1/agents/{agent_id}/shares/1"),
                ("DELETE", f"/api/v1/agents/{agent_id}/shares/1"),
                ("POST", "/api/v1/shared-agents/disabled"),
                ("POST", "/api/v1/agents"),
                ("PUT", f"/api/v1/agents/{agent_id}"),
                ("DELETE", f"/api/v1/agents/{agent_id}"),
                ("POST", f"/api/v1/agents/{agent_id}/copy"),
                ("POST", f"/api/v1/agents/{agent_id}/im-channels"),
                ("PUT", "/api/v1/im-channels/missing"),
                ("DELETE", "/api/v1/im-channels/missing"),
                ("POST", "/api/v1/im-channels/missing/toggle"),
            )
            for method, path in denied_writes:
                response = await client.request(
                    method,
                    path,
                    headers=viewer_headers,
                    json={} if method in {"POST", "PUT", "PATCH"} else None,
                )
                assert response.status_code == 403, (method, path, response.text)

            cross_tenant_channel = await client.put(
                f"/api/v1/im-channels/{im_channel_id}",
                headers=_headers(other_admin),
                json={"name": "hijacked"},
            )
            assert cross_tenant_channel.status_code == 404, cross_tenant_channel.text
            assert "tenant-secret" not in cross_tenant_channel.text

            for path in (
                f"/api/v1/knowledge-bases/{kb_id}",
                f"/api/v1/knowledge/{knowledge_id}",
                f"/api/v1/chunks/{knowledge_id}",
                f"/api/v1/knowledge-bases/{kb_id}/tags",
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries",
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages",
                f"/api/v1/agents/{agent_id}",
                f"/api/v1/initialization/config/{kb_id}",
            ):
                response = await client.get(path, headers=_headers(other_admin))
                assert response.status_code == 404, (path, response.text)

            for method, path in (
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual"),
                ("PUT", f"/api/v1/knowledge/{knowledge_id}"),
                ("PUT", f"/api/v1/chunks/{knowledge_id}/{chunk_id}"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/tags/{tag_id}"),
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}/faq/entries/{faq_entry_id}"),
                ("PUT", f"/api/v1/knowledgebase/{kb_id}/wiki/pages/{wiki_slug}"),
                ("PUT", f"/api/v1/agents/{agent_id}"),
                ("PUT", f"/api/v1/initialization/config/{kb_id}"),
            ):
                response = await client.request(
                    method,
                    path,
                    headers=_headers(other_admin),
                    json={},
                )
                assert response.status_code == 404, (method, path, response.text)

            for method, path in (
                ("PUT", f"/api/v1/knowledge-bases/{kb_id}"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/knowledge/manual"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/tags"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/faq/entry"),
                ("GET", f"/api/v1/knowledge/{knowledge_id}/download"),
                ("POST", f"/api/v1/knowledge-bases/{kb_id}/knowledge/batch-download"),
                ("PUT", f"/api/v1/knowledge/{knowledge_id}"),
                ("PUT", f"/api/v1/chunks/{knowledge_id}/{chunk_id}"),
                ("PUT", f"/api/v1/knowledgebase/{kb_id}/wiki/pages/{wiki_slug}"),
                ("PUT", f"/api/v1/initialization/config/{kb_id}"),
                ("POST", f"/api/v1/initialization/initialize/{kb_id}"),
            ):
                response = await client.request(
                    method,
                    path,
                    headers=contributor_headers,
                    json={} if method in {"POST", "PUT", "PATCH"} else None,
                )
                assert response.status_code == 403, (method, path, response.text)

            for path in (
                "/api/v1/storage-backends",
                "/api/v1/vector-stores",
                "/api/v1/web-search-providers",
                "/api/v1/datasource",
                "/api/v1/mcp-endpoints",
                "/api/v1/mcp-services",
                "/api/v1/skills/catalog",
                "/api/v1/evaluation/",
                f"/api/v1/agents/{agent_id}",
                f"/api/v1/organizations/{org_id}",
                f"/api/v1/knowledge-bases/{kb_id}",
                f"/api/v1/knowledge/{knowledge_id}",
                f"/api/v1/chunks/{knowledge_id}",
                f"/api/v1/knowledge-bases/{kb_id}/tags",
                f"/api/v1/knowledge-bases/{kb_id}/faq/entries",
                f"/api/v1/knowledgebase/{kb_id}/wiki/pages",
                f"/api/v1/initialization/config/{kb_id}",
            ):
                response = await client.get(path, headers=viewer_headers)
                assert response.status_code == 200, (path, response.text)

    _run(case)


def test_cross_tenant_subresource_updates_do_not_leak_foreign_resources() -> None:
    async def register_user(client: httpx.AsyncClient, prefix: str) -> dict:
        suffix = uuid.uuid4().hex[:10]
        credentials = {
            "username": f"{prefix}-{suffix}",
            "email": f"{prefix}-{suffix}@example.com",
            "password": "CrossTenantPass123",
        }
        registered = await client.post("/api/v1/auth/register", json=credentials)
        assert registered.status_code == 201, registered.text
        return await _login(
            client, {"email": credentials["email"], "password": credentials["password"]}
        )

    async def case() -> None:
        async with await _client() as client:
            victim = await register_user(client, "cross-tenant-victim")
            attacker = await register_user(client, "cross-tenant-attacker")
            victim_headers = _headers(victim)
            attacker_headers = _headers(attacker)
            victim_tenant_id = int(victim["active_tenant"]["id"])
            attacker_tenant_id = int(attacker["active_tenant"]["id"])
            assert victim_tenant_id != attacker_tenant_id

            victim_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=victim_headers,
                json={"name": "Victim cross-tenant KB"},
            )
            assert victim_kb.status_code == 201, victim_kb.text
            victim_kb_id = victim_kb.json()["data"]["id"]
            attacker_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=attacker_headers,
                json={"name": "Attacker cross-tenant KB"},
            )
            assert attacker_kb.status_code == 201, attacker_kb.text
            attacker_kb_id = attacker_kb.json()["data"]["id"]
            attacker_knowledge = await client.post(
                f"/api/v1/knowledge-bases/{attacker_kb_id}/knowledge/manual",
                headers=attacker_headers,
                json={"title": "Attacker document", "content": "Attacker content."},
            )
            assert attacker_knowledge.status_code == 201, attacker_knowledge.text
            attacker_knowledge_id = attacker_knowledge.json()["data"]["id"]

            victim_tag = await client.post(
                f"/api/v1/knowledge-bases/{victim_kb_id}/tags",
                headers=victim_headers,
                json={"name": "victim-tag"},
            )
            assert victim_tag.status_code == 201, victim_tag.text
            victim_tag_id = victim_tag.json()["data"]["id"]
            victim_tag_seq_id = victim_tag.json()["data"]["seq_id"]
            victim_faq = await client.post(
                f"/api/v1/knowledge-bases/{victim_kb_id}/faq/entry",
                headers=victim_headers,
                json={"standard_question": "Victim question?", "answer": "Victim answer."},
            )
            assert victim_faq.status_code == 201, victim_faq.text
            victim_faq_id = victim_faq.json()["data"]["id"]
            victim_key = await client.post(
                f"/api/v1/tenants/{victim_tenant_id}/api-keys",
                headers=victim_headers,
                json={"name": "victim-api-key", "full_access": True},
            )
            assert victim_key.status_code == 201, victim_key.text
            victim_key_id = victim_key.json()["data"]["id"]

            foreign_tag = await client.put(
                f"/api/v1/knowledge-bases/{attacker_kb_id}/tags/{victim_tag_id}",
                headers=attacker_headers,
                json={"name": "hijacked-tag"},
            )
            assert foreign_tag.status_code == 404, foreign_tag.text
            assert "victim-tag" not in foreign_tag.text

            foreign_faq = await client.put(
                f"/api/v1/knowledge-bases/{attacker_kb_id}/faq/entries/{victim_faq_id}",
                headers=attacker_headers,
                json={"standard_question": "Hijacked question?", "answer": "Hijacked answer."},
            )
            assert foreign_faq.status_code == 404, foreign_faq.text
            assert "Victim question?" not in foreign_faq.text

            foreign_key = await client.put(
                f"/api/v1/tenants/{attacker_tenant_id}/api-keys/{victim_key_id}",
                headers=attacker_headers,
                json={"name": "hijacked-api-key"},
            )
            assert foreign_key.status_code == 404, foreign_key.text
            assert "victim-api-key" not in foreign_key.text

            foreign_tag_reference = await client.put(
                "/api/v1/knowledge/tags",
                headers=attacker_headers,
                json={"updates": {attacker_knowledge_id: [victim_tag_id]}, "kb_id": attacker_kb_id},
            )
            assert foreign_tag_reference.status_code == 400, foreign_tag_reference.text
            assert "victim-tag" not in foreign_tag_reference.text
            foreign_faq_tag = await client.post(
                f"/api/v1/knowledge-bases/{attacker_kb_id}/faq/entry",
                headers=attacker_headers,
                json={
                    "standard_question": "Foreign tag question?",
                    "answer": "Foreign tag answer.",
                    "tag_id": victim_tag_seq_id,
                },
            )
            assert foreign_faq_tag.status_code == 400, foreign_faq_tag.text
            assert "victim-tag" not in foreign_faq_tag.text
            attacker_knowledge_after = await client.get(
                f"/api/v1/knowledge/{attacker_knowledge_id}", headers=attacker_headers
            )
            assert attacker_knowledge_after.status_code == 200, attacker_knowledge_after.text
            assert attacker_knowledge_after.json()["data"]["tags"] == []

            victim_tags = await client.get(
                f"/api/v1/knowledge-bases/{victim_kb_id}/tags", headers=victim_headers
            )
            assert victim_tags.status_code == 200, victim_tags.text
            assert any(
                item["id"] == victim_tag_id and item["name"] == "victim-tag"
                for item in victim_tags.json()["data"]
            )
            victim_faq_after = await client.get(
                f"/api/v1/knowledge-bases/{victim_kb_id}/faq/entries/{victim_faq_id}",
                headers=victim_headers,
            )
            assert victim_faq_after.status_code == 200, victim_faq_after.text
            assert victim_faq_after.json()["data"]["standard_question"] == "Victim question?"
            victim_keys = await client.get(
                f"/api/v1/tenants/{victim_tenant_id}/api-keys", headers=victim_headers
            )
            assert victim_keys.status_code == 200, victim_keys.text
            assert any(
                item["id"] == victim_key_id and item["name"] == "victim-api-key"
                for item in victim_keys.json()["data"]
            )

    _run(case)


def test_api_key_authentication_capabilities_and_kb_scope() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            admin_headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])

            allowed_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=admin_headers,
                json={"name": "API key allowed KB"},
            )
            assert allowed_kb.status_code == 201, allowed_kb.text
            allowed_kb_id = allowed_kb.json()["data"]["id"]
            denied_kb = await client.post(
                "/api/v1/knowledge-bases",
                headers=admin_headers,
                json={"name": "API key denied KB"},
            )
            assert denied_kb.status_code == 201, denied_kb.text
            denied_kb_id = denied_kb.json()["data"]["id"]

            def create_key(**payload):
                return client.post(
                    f"/api/v1/tenants/{tenant_id}/api-keys",
                    headers=admin_headers,
                    json=payload,
                )

            full_key = await create_key(name="full", full_access=True)
            assert full_key.status_code == 201, full_key.text
            full_token = full_key.json()["data"]["token"]
            full_headers = _api_key_headers(full_token)

            retrieve_key = await create_key(
                name="retrieve-only",
                capabilities=["retrieve"],
                knowledge_base_ids=[allowed_kb_id],
            )
            assert retrieve_key.status_code == 201, retrieve_key.text
            retrieve_token = retrieve_key.json()["data"]["token"]
            retrieve_headers = _api_key_headers(retrieve_token)

            ingest_key = await create_key(
                name="ingest-only",
                capabilities=["ingest"],
                knowledge_base_ids=[allowed_kb_id],
            )
            assert ingest_key.status_code == 201, ingest_key.text
            ingest_token = ingest_key.json()["data"]["token"]
            ingest_headers = _api_key_headers(ingest_token)

            file_retrieve_key = await create_key(
                name="retrieve-all-files", capabilities=["retrieve"]
            )
            assert file_retrieve_key.status_code == 201, file_retrieve_key.text
            file_retrieve_headers = _api_key_headers(file_retrieve_key.json()["data"]["token"])
            chat_key = await create_key(name="chat-files", capabilities=["chat"])
            assert chat_key.status_code == 201, chat_key.text
            chat_headers = _api_key_headers(chat_key.json()["data"]["token"])

            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{allowed_kb_id}/knowledge/file",
                headers=admin_headers,
                files={"file": ("api-key.txt", b"api key file", "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            file_path = uploaded.json()["data"]["file_path"]
            file_query = urllib.parse.urlencode({"file_path": file_path})

            me = await client.get("/api/v1/auth/me", headers=retrieve_headers)
            assert me.status_code == 200, me.text
            assert me.json()["data"]["memberships"][0]["role"] == "owner"

            capabilities = await client.get("/api/v1/system/capabilities", headers=retrieve_headers)
            assert capabilities.status_code == 200, capabilities.text

            full_file = await client.get("/files?" + file_query, headers=full_headers)
            assert full_file.status_code == 200, full_file.text
            unrestricted_file = await client.get("/files?" + file_query, headers=file_retrieve_headers)
            assert unrestricted_file.status_code == 200, unrestricted_file.text
            restricted_file = await client.get("/files?" + file_query, headers=retrieve_headers)
            assert restricted_file.status_code == 403, restricted_file.text
            wrong_capability_file = await client.get("/files?" + file_query, headers=chat_headers)
            assert wrong_capability_file.status_code == 403, wrong_capability_file.text

            listed = await client.get("/api/v1/knowledge-bases", headers=retrieve_headers)
            assert listed.status_code == 200, listed.text
            assert {item["id"] for item in listed.json()["data"]} == {allowed_kb_id}

            allowed = await client.get(f"/api/v1/knowledge-bases/{allowed_kb_id}", headers=retrieve_headers)
            assert allowed.status_code == 200, allowed.text
            denied = await client.get(f"/api/v1/knowledge-bases/{denied_kb_id}", headers=retrieve_headers)
            assert denied.status_code == 403, denied.text

            search_denied = await client.post(
                "/api/v1/knowledge-search",
                headers=retrieve_headers,
                json={"query": "scope", "knowledge_base_ids": [denied_kb_id]},
            )
            assert search_denied.status_code == 403, search_denied.text
            batch_denied = await client.get(
                f"/api/v1/knowledge/batch?kb_id={denied_kb_id}", headers=retrieve_headers
            )
            assert batch_denied.status_code == 403, batch_denied.text

            read_only = await client.post(
                f"/api/v1/knowledge-bases/{allowed_kb_id}/knowledge/manual",
                headers=retrieve_headers,
                json={"title": "blocked", "content": "blocked"},
            )
            assert read_only.status_code == 403, read_only.text
            ingest_list = await client.get("/api/v1/knowledge-bases", headers=ingest_headers)
            assert ingest_list.status_code == 403, ingest_list.text
            created = await client.post(
                f"/api/v1/knowledge-bases/{allowed_kb_id}/knowledge/manual",
                headers=ingest_headers,
                json={"title": "ingested", "content": "ingested by api key"},
            )
            assert created.status_code == 201, created.text

            full_listed = await client.get("/api/v1/knowledge-bases", headers=full_headers)
            assert full_listed.status_code == 200, full_listed.text
            assert {item["id"] for item in full_listed.json()["data"]} >= {allowed_kb_id, denied_kb_id}
            key_management = await client.get(
                f"/api/v1/tenants/{tenant_id}/api-keys", headers=full_headers
            )
            assert key_management.status_code == 403, key_management.text

            other_tenant = await client.post(
                "/api/v1/tenants", headers=admin_headers, json={"name": "API key other workspace"}
            )
            assert other_tenant.status_code == 201, other_tenant.text
            switched = await client.get(
                "/api/v1/knowledge-bases",
                headers=_api_key_headers(full_token, int(other_tenant.json()["data"]["id"])),
            )
            assert switched.status_code == 403, switched.text

            revoked_key = await create_key(
                name="revoked", capabilities=["retrieve"], knowledge_base_ids=[allowed_kb_id]
            )
            assert revoked_key.status_code == 201, revoked_key.text
            revoked_data = revoked_key.json()["data"]
            revoked_response = await client.delete(
                f"/api/v1/tenants/{tenant_id}/api-keys/{revoked_data['id']}", headers=admin_headers
            )
            assert revoked_response.status_code == 200, revoked_response.text
            revoked_me = await client.get(
                "/api/v1/auth/me", headers=_api_key_headers(revoked_data["token"])
            )
            assert revoked_me.status_code == 401, revoked_me.text

            expired_key = await create_key(
                name="expired", capabilities=["retrieve"], knowledge_base_ids=[allowed_kb_id]
            )
            assert expired_key.status_code == 201, expired_key.text
            expired_data = expired_key.json()["data"]
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE tenant_api_keys SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                    (expired_data["id"],),
                )
            expired_me = await client.get(
                "/api/v1/auth/me", headers=_api_key_headers(expired_data["token"])
            )
            assert expired_me.status_code == 401, expired_me.text

            fallback = await client.get(
                "/api/v1/auth/me",
                headers={"Authorization": "Bearer invalid-token", "X-API-Key": retrieve_token},
            )
            assert fallback.status_code == 200, fallback.text
            invalid = await client.get("/api/v1/auth/me", headers={"X-API-Key": "wk_invalid"})
            assert invalid.status_code == 401, invalid.text

            platform_retrieve = await client.post(
                "/api/v1/system/admin/api-keys",
                headers=admin_headers,
                json={"name": "platform retrieve", "capabilities": ["retrieve"]},
            )
            assert platform_retrieve.status_code == 201, platform_retrieve.text
            platform_token = platform_retrieve.json()["data"]["token"]
            missing_tenant = await client.get(
                "/api/v1/knowledge-bases", headers=_api_key_headers(platform_token)
            )
            assert missing_tenant.status_code == 409, missing_tenant.text
            targeted = await client.get(
                "/api/v1/knowledge-bases",
                headers=_api_key_headers(platform_token, tenant_id),
            )
            assert targeted.status_code == 200, targeted.text

            platform_settings = await client.post(
                "/api/v1/system/admin/api-keys",
                headers=admin_headers,
                json={"name": "platform settings", "capabilities": ["system_settings_read"]},
            )
            assert platform_settings.status_code == 201, platform_settings.text
            settings_token = platform_settings.json()["data"]["token"]
            settings_headers = _api_key_headers(settings_token)
            settings = await client.get("/api/v1/system/admin/settings", headers=settings_headers)
            assert settings.status_code == 200, settings.text
            wrong_capability = await client.get(
                "/api/v1/system/admin/settings", headers=_api_key_headers(platform_token)
            )
            assert wrong_capability.status_code == 403, wrong_capability.text
            platform_key_management = await client.get(
                "/api/v1/system/admin/api-keys", headers=settings_headers
            )
            assert platform_key_management.status_code == 403, platform_key_management.text

    _run(case)


def test_knowledge_transfer_progress_is_persisted_and_tenant_scoped() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            tenant_id = int(login["active_tenant"]["id"])

            async def wait_for_progress(path: str) -> dict:
                for _ in range(500):
                    response = await client.get(path, headers=headers)
                    assert response.status_code == 200, response.text
                    data = response.json()["data"]
                    if data["status"] in {"completed", "failed"}:
                        return data
                    await asyncio.sleep(0.01)
                raise AssertionError(f"task did not finish: {path}")

            source = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Transfer source"}
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]
            copy_target = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Copy target"}
            )
            assert copy_target.status_code == 201, copy_target.text
            copy_target_id = copy_target.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "Transfer document", "content": "transfer content"},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]

            copied = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": copy_target_id},
            )
            assert copied.status_code == 200, copied.text
            copy_task_id = copied.json()["data"]["task_id"]
            copy_data = await wait_for_progress(
                f"/api/v1/knowledge-bases/copy/progress/{copy_task_id}"
            )
            assert copy_data["status"] == "completed"
            assert copy_data["progress"] == 100
            assert copy_data["source_id"] == source_id
            assert copy_data["target_id"] == copy_target_id
            copied_items = await client.get(
                f"/api/v1/knowledge-bases/{copy_target_id}/knowledge", headers=headers
            )
            assert copied_items.status_code == 200, copied_items.text
            assert [item["title"] for item in copied_items.json()["data"]] == ["Transfer document"]

            move_target = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Move target"}
            )
            assert move_target.status_code == 201, move_target.text
            move_target_id = move_target.json()["data"]["id"]
            moved = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": move_target_id,
                    "knowledge_ids": [knowledge_id],
                    "mode": "reuse_vectors",
                },
            )
            assert moved.status_code == 200, moved.text
            move_task_id = moved.json()["data"]["task_id"]
            assert moved.json()["data"]["knowledge_count"] == 1
            move_data = await wait_for_progress(
                f"/api/v1/knowledge/move/progress/{move_task_id}"
            )
            assert move_data["status"] == "completed"
            assert move_data["source_kb_id"] == source_id
            assert move_data["target_kb_id"] == move_target_id
            assert move_data["processed"] == 1

            reparsed = await client.post(
                "/api/v1/knowledge/batch-reparse",
                headers=headers,
                json={"kb_id": move_target_id, "ids": [knowledge_id]},
            )
            assert reparsed.status_code == 200, reparsed.text
            reparse_task_id = reparsed.json()["data"]["task_id"]
            for _ in range(500):
                reparse_row = db.fetch_one(
                    "SELECT tenant_id, kind, status, total, processed FROM py_task_results WHERE id=?",
                    (reparse_task_id,),
                )
                if reparse_row and reparse_row["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert reparse_row == {
                "tenant_id": tenant_id,
                "kind": "knowledge_reparse",
                "status": "completed",
                "total": 1,
                "processed": 1,
            }

            missing_copy = await client.get(
                f"/api/v1/knowledge-bases/copy/progress/{uuid.uuid4()}", headers=headers
            )
            assert missing_copy.status_code == 404, missing_copy.text
            missing_move = await client.get(
                f"/api/v1/knowledge/move/progress/{uuid.uuid4()}", headers=headers
            )
            assert missing_move.status_code == 404, missing_move.text

            other_tenant = await client.post(
                "/api/v1/tenants", headers=headers, json={"name": "Transfer other workspace"}
            )
            assert other_tenant.status_code == 201, other_tenant.text
            other_headers = {
                **headers,
                "X-Tenant-ID": str(int(other_tenant.json()["data"]["id"])),
            }
            crossed_copy = await client.get(
                f"/api/v1/knowledge-bases/copy/progress/{copy_task_id}", headers=other_headers
            )
            assert crossed_copy.status_code == 404, crossed_copy.text
            crossed_move = await client.get(
                f"/api/v1/knowledge/move/progress/{move_task_id}", headers=other_headers
            )
            assert crossed_move.status_code == 404, crossed_move.text

    _run(case)


def test_knowledge_move_respects_reuse_vectors_and_reparse_modes() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            async def create_kb(
                name: str,
                chunking_config: dict | None = None,
                wiki_config: dict | None = None,
            ) -> str:
                payload = {"name": name}
                if chunking_config is not None:
                    payload["chunking_config"] = chunking_config
                if wiki_config is not None:
                    payload["wiki_config"] = wiki_config
                response = await client.post(
                    "/api/v1/knowledge-bases", headers=headers, json=payload
                )
                assert response.status_code == 201, response.text
                return response.json()["data"]["id"]

            async def wait_for_move(task_id: str) -> dict:
                for _ in range(500):
                    response = await client.get(
                        f"/api/v1/knowledge/move/progress/{task_id}", headers=headers
                    )
                    assert response.status_code == 200, response.text
                    data = response.json()["data"]
                    if data["status"] in {"completed", "failed"}:
                        return data
                    await asyncio.sleep(0.01)
                raise AssertionError("knowledge move task did not finish")

            source_id = await create_kb(
                "Move mode source", wiki_config={"enabled": True}
            )
            reuse_target_id = await create_kb("Move mode reuse target")
            reparse_target_id = await create_kb(
                "Move mode reparse target",
                {"chunk_size": 100, "chunk_overlap": 0},
                {"enabled": True},
            )

            reuse_doc = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "Reuse vectors document", "content": "reuse vectors content"},
            )
            assert reuse_doc.status_code == 201, reuse_doc.text
            reuse_id = reuse_doc.json()["data"]["id"]
            reparse_doc = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={
                    "title": "Reparse document",
                    "content": "reparse target chunking content " * 30,
                },
            )
            assert reparse_doc.status_code == 201, reparse_doc.text
            reparse_id = reparse_doc.json()["data"]["id"]
            source_pages = db.fetch_all(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (source_id,),
            )
            assert len(source_pages) == 2
            tag = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/tags",
                headers=headers,
                json={"name": "move-tag", "color": "#2288ff"},
            )
            assert tag.status_code == 201, tag.text
            tagged = await client.post(
                "/api/v1/knowledge/tags",
                headers=headers,
                json={"updates": {reuse_id: [tag.json()["data"]["id"]], reparse_id: [tag.json()["data"]["id"]]}},
            )
            assert tagged.status_code == 200, tagged.text

            reuse_chunk_ids = [
                row["id"]
                for row in db.fetch_all(
                    "SELECT id FROM chunks WHERE knowledge_id=? ORDER BY chunk_index", (reuse_id,)
                )
            ]
            reparse_chunk_ids = [
                row["id"]
                for row in db.fetch_all(
                    "SELECT id FROM chunks WHERE knowledge_id=? ORDER BY chunk_index", (reparse_id,)
                )
            ]

            moved = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": reuse_target_id,
                    "knowledge_ids": [reuse_id],
                    "mode": "reuse_vectors",
                },
            )
            assert moved.status_code == 200, moved.text
            reuse_progress = await wait_for_move(moved.json()["data"]["task_id"])
            assert reuse_progress["status"] == "completed"
            assert reuse_progress["failed"] == 0
            assert db.fetch_one("SELECT knowledge_base_id FROM knowledges WHERE id=?", (reuse_id,))["knowledge_base_id"] == reuse_target_id
            assert [
                row["id"]
                for row in db.fetch_all(
                    "SELECT id FROM chunks WHERE knowledge_id=? ORDER BY chunk_index", (reuse_id,)
                )
            ] == reuse_chunk_ids
            reuse_loaded = await client.get(f"/api/v1/knowledge/{reuse_id}", headers=headers)
            assert reuse_loaded.status_code == 200, reuse_loaded.text
            assert reuse_loaded.json()["data"]["tags"] == []
            assert db.fetch_one(
                "SELECT COUNT(*) AS count FROM wiki_pages "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (reuse_target_id,),
            )["count"] == 0
            source_pages = db.fetch_all(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (source_id,),
            )
            assert len(source_pages) == 1
            assert any(
                reparse_id in [ref.get("knowledge_id") for ref in json.loads(page["source_refs"])]
                for page in source_pages
            )

            moved = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": reparse_target_id,
                    "knowledge_ids": [reparse_id],
                    "mode": "reparse",
                },
            )
            assert moved.status_code == 200, moved.text
            reparse_progress = await wait_for_move(moved.json()["data"]["task_id"])
            assert reparse_progress["status"] == "completed"
            assert reparse_progress["failed"] == 0
            reparse_row = db.fetch_one("SELECT * FROM knowledges WHERE id=?", (reparse_id,))
            assert reparse_row["knowledge_base_id"] == reparse_target_id
            assert reparse_row["parse_status"] == "completed"
            rebuilt_chunks = db.fetch_all(
                "SELECT id, knowledge_base_id, content FROM chunks WHERE knowledge_id=? ORDER BY chunk_index",
                (reparse_id,),
            )
            assert set(row["id"] for row in rebuilt_chunks).isdisjoint(reparse_chunk_ids)
            assert len(rebuilt_chunks) > 1
            assert all(row["knowledge_base_id"] == reparse_target_id for row in rebuilt_chunks)
            assert all(len(row["content"]) <= 100 for row in rebuilt_chunks)
            reparse_loaded = await client.get(f"/api/v1/knowledge/{reparse_id}", headers=headers)
            assert reparse_loaded.status_code == 200, reparse_loaded.text
            assert reparse_loaded.json()["data"]["tags"] == []
            assert db.fetch_one(
                "SELECT COUNT(*) AS count FROM knowledge_tag_relations WHERE knowledge_id IN (?, ?)",
                (reuse_id, reparse_id),
            )["count"] == 0
            assert db.fetch_one(
                "SELECT COUNT(*) AS count FROM wiki_pages "
                "WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (source_id,),
            )["count"] == 0
            target_pages = db.fetch_all(
                "SELECT * FROM wiki_pages WHERE knowledge_base_id=? AND deleted_at IS NULL",
                (reparse_target_id,),
            )
            assert len(target_pages) == 1
            assert [ref.get("knowledge_id") for ref in json.loads(target_pages[0]["source_refs"])] == [reparse_id]

    _run(case)


def test_wiki_remove_knowledge_source_keeps_shared_pages() -> None:
    db.init_db()
    user = db.fetch_one("SELECT * FROM users WHERE email=?", (ADMIN["email"],))
    assert user is not None
    tenant_id = int(user["tenant_id"])
    kb_id = str(uuid.uuid4())
    page_id = str(uuid.uuid4())
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO wiki_pages(id, tenant_id, knowledge_base_id, slug, title, "
            "source_refs, chunk_refs, page_metadata, created_at, updated_at) "
            "VALUES (?, ?, ?, 'shared-page', 'Shared page', ?, ?, ?, ?, ?)",
            (
                page_id,
                tenant_id,
                kb_id,
                json.dumps(
                    [
                        {"knowledge_id": "knowledge-1", "title": "One"},
                        {"knowledge_id": "knowledge-2", "title": "Two"},
                    ]
                ),
                json.dumps(["chunk-1", "chunk-2"]),
                json.dumps({"knowledge_id": "knowledge-1", "auto_generated": True}),
                now,
                now,
            ),
        )

    assert wiki_ingest.remove_knowledge_source(
        kb_id, tenant_id, "knowledge-1", ["chunk-1"]
    ) == 1
    page = db.fetch_one("SELECT * FROM wiki_pages WHERE id=?", (page_id,))
    assert page is not None
    assert page["deleted_at"] is None
    assert [ref["knowledge_id"] for ref in json.loads(page["source_refs"])] == ["knowledge-2"]
    assert json.loads(page["chunk_refs"]) == ["chunk-2"]
    assert json.loads(page["page_metadata"]) == {"auto_generated": True}
    assert page["version"] == 2

    assert wiki_ingest.remove_knowledge_source(
        kb_id, tenant_id, "knowledge-2", ["chunk-2"]
    ) == 1
    page = db.fetch_one("SELECT * FROM wiki_pages WHERE id=?", (page_id,))
    assert page["deleted_at"] is not None


def test_knowledge_move_can_be_cancelled_and_rerun(monkeypatch) -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            source = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Cancel move source"}
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]
            target = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Cancel move target"}
            )
            assert target.status_code == 201, target.text
            target_id = target.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "Cancel move document", "content": "move me"},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]

            gate = asyncio.Event()

            async def hold_worker(task_id: str) -> None:
                await gate.wait()

            original_worker = knowledge_router.TASK_WORKERS["knowledge_move"]
            monkeypatch.setitem(knowledge_router.TASK_WORKERS, "knowledge_move", hold_worker)
            moved = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": target_id,
                    "knowledge_ids": [knowledge_id],
                    "mode": "reuse_vectors",
                },
            )
            assert moved.status_code == 200, moved.text
            task_id = moved.json()["data"]["task_id"]
            assert db.fetch_one("SELECT status FROM py_task_results WHERE id=?", (task_id,))["status"] == "pending"

            cancelled = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{task_id}/actions/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200, cancelled.text
            assert db.fetch_one("SELECT status FROM py_task_results WHERE id=?", (task_id,))["status"] == "cancelled"
            assert db.fetch_one("SELECT knowledge_base_id FROM knowledges WHERE id=?", (knowledge_id,))["knowledge_base_id"] == source_id
            knowledge_router._save_worker_progress(
                task_id,
                status="completed",
                progress={"status": "completed", "total": 1, "processed": 1},
            )
            assert db.fetch_one("SELECT status FROM py_task_results WHERE id=?", (task_id,))["status"] == "cancelled"

            gate.set()
            await asyncio.sleep(0.05)
            monkeypatch.setitem(knowledge_router.TASK_WORKERS, "knowledge_move", original_worker)
            rerun = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{task_id}/actions/run_now",
                headers=headers,
            )
            assert rerun.status_code == 200, rerun.text
            for _ in range(500):
                progress = await client.get(
                    f"/api/v1/knowledge/move/progress/{task_id}", headers=headers
                )
                assert progress.status_code == 200, progress.text
                data = progress.json()["data"]
                if data["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert data["status"] == "completed"
            assert data["processed"] == 1
            assert db.fetch_one("SELECT knowledge_base_id FROM knowledges WHERE id=?", (knowledge_id,))["knowledge_base_id"] == target_id

    _run(case)


def test_knowledge_copy_creates_target_and_reruns_without_duplicates(monkeypatch) -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            source = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Async copy source"}
            )
            assert source.status_code == 201, source.text
            source_id = source.json()["data"]["id"]
            manual = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "Async copy document", "content": "copy me"},
            )
            assert manual.status_code == 201, manual.text

            gate = asyncio.Event()

            async def hold_worker(task_id: str) -> None:
                await gate.wait()

            original_worker = knowledge_router.TASK_WORKERS["kb_clone"]
            monkeypatch.setitem(knowledge_router.TASK_WORKERS, "kb_clone", hold_worker)
            copied = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id},
            )
            assert copied.status_code == 200, copied.text
            data = copied.json()["data"]
            task_id = data["task_id"]
            target_id = data["target_id"]
            assert target_id not in {"", source_id}
            assert db.fetch_one("SELECT 1 FROM knowledge_bases WHERE id=?", (target_id,)) is None

            cancelled = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{task_id}/actions/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200, cancelled.text
            gate.set()
            await asyncio.sleep(0.05)
            monkeypatch.setitem(knowledge_router.TASK_WORKERS, "kb_clone", original_worker)
            rerun = await client.post(
                f"/api/v1/system/admin/runtime/queues/database/tasks/{task_id}/actions/run_now",
                headers=headers,
            )
            assert rerun.status_code == 200, rerun.text
            for _ in range(500):
                progress = await client.get(
                    f"/api/v1/knowledge-bases/copy/progress/{task_id}", headers=headers
                )
                assert progress.status_code == 200, progress.text
                progress_data = progress.json()["data"]
                if progress_data["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert progress_data["status"] == "completed"
            assert progress_data["processed"] == 1
            copied_items = await client.get(
                f"/api/v1/knowledge-bases/{target_id}/knowledge", headers=headers
            )
            assert copied_items.status_code == 200, copied_items.text
            assert [item["title"] for item in copied_items.json()["data"]] == [
                "Async copy document"
            ]

    _run(case)


def test_batch_reparse_rebuilds_source_chunks() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)
            kb = await client.post(
                "/api/v1/knowledge-bases", headers=headers, json={"name": "Reparse source"}
            )
            assert kb.status_code == 201, kb.text
            kb_id = kb.json()["data"]["id"]
            source_text = "original retrieval token\n\nsecond paragraph survives reparse"
            uploaded = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/knowledge/file",
                headers=headers,
                files={"file": ("reparse.txt", source_text.encode(), "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            knowledge_id = uploaded.json()["data"]["id"]
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE chunks SET content='stale content', source_content='stale content' WHERE knowledge_id=?",
                    (knowledge_id,),
                )

            reparsed = await client.post(
                "/api/v1/knowledge/batch-reparse",
                headers=headers,
                json={"kb_id": kb_id, "ids": [knowledge_id]},
            )
            assert reparsed.status_code == 200, reparsed.text
            task_id = reparsed.json()["data"]["task_id"]
            for _ in range(500):
                task = db.fetch_one(
                    "SELECT status, processed, result FROM py_task_results WHERE id=?",
                    (task_id,),
                )
                if task and task["status"] in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.01)
            assert task["status"] == "completed"
            assert task["processed"] == 1
            assert json.loads(task["result"])["failed"] == 0

            chunks = db.fetch_all(
                "SELECT content FROM chunks WHERE knowledge_id=? AND deleted_at IS NULL ORDER BY chunk_index",
                (knowledge_id,),
            )
            chunk_text = "\n\n".join(row["content"] for row in chunks)
            assert "original retrieval token" in chunk_text
            assert "stale content" not in chunk_text
            found = await client.post(
                f"/api/v1/knowledge-bases/{kb_id}/hybrid-search",
                headers=headers,
                json={"query": "original retrieval token"},
            )
            assert found.status_code == 200, found.text
            assert any(
                item["knowledge_id"] == knowledge_id for item in found.json()["data"]
            )

    _run(case)


def test_pending_task_workers_recover_after_restart(monkeypatch) -> None:
    async def case() -> None:
        task_id = db.new_id()
        now = db.utcnow()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO py_task_results"
                "(id, tenant_id, kind, status, total, processed, payload, result, error, created_at, updated_at) "
                "VALUES (?, 1, 'knowledge_reparse', 'processing', 0, 0, '{}', '{}', '', ?, ?)",
                (task_id, now, now),
            )
        recovered: list[str] = []

        async def record_worker(recovered_task_id: str) -> None:
            recovered.append(recovered_task_id)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE py_task_results SET status='completed', updated_at=? WHERE id=?",
                    (db.utcnow(), recovered_task_id),
                )

        monkeypatch.setitem(
            knowledge_router.TASK_WORKERS, "knowledge_reparse", record_worker
        )
        async with app.router.lifespan_context(app):
            for _ in range(500):
                if task_id in recovered:
                    break
                await asyncio.sleep(0.01)
        assert task_id in recovered

    _run(case)


def test_leased_task_workers_recover_after_lease_expiry(monkeypatch) -> None:
    async def case() -> None:
        task_id = db.new_id()
        now = db.utcnow()
        lease_until = f"{time.time() + 0.1:.3f}"
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO py_task_results"
                "(id, tenant_id, kind, status, total, processed, payload, result, error, "
                "worker_token, lease_until, created_at, updated_at) "
                "VALUES (?, 1, 'knowledge_reparse', 'processing', 0, 0, '{}', '{}', '', "
                "'dead-worker', ?, ?, ?)",
                (task_id, lease_until, now, now),
            )
        recovered: list[str] = []

        async def record_worker(recovered_task_id: str) -> None:
            recovered.append(recovered_task_id)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE py_task_results SET status='completed', updated_at=? WHERE id=?",
                    (db.utcnow(), recovered_task_id),
                )

        monkeypatch.setitem(
            knowledge_router.TASK_WORKERS, "knowledge_reparse", record_worker
        )
        async with app.router.lifespan_context(app):
            for _ in range(500):
                if task_id in recovered:
                    break
                await asyncio.sleep(0.01)
        assert task_id in recovered
        assert time.time() >= float(lease_until)

    _run(case)


def test_task_worker_claim_prevents_duplicate_execution(monkeypatch) -> None:
    async def case() -> None:
        task_id = db.new_id()
        now = db.utcnow()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO py_task_results"
                "(id, tenant_id, kind, status, total, processed, payload, result, error, created_at, updated_at) "
                "VALUES (?, 1, 'knowledge_reparse', 'pending', 0, 0, '{}', '{}', '', ?, ?)",
                (task_id, now, now),
            )
        calls: list[str] = []
        leases: list[str] = []

        async def record_worker(recovered_task_id: str) -> None:
            calls.append(recovered_task_id)
            leases.append(
                db.fetch_one(
                    "SELECT lease_until FROM py_task_results WHERE id=?", (task_id,)
                )["lease_until"]
            )
            await asyncio.sleep(0.05)
            leases.append(
                db.fetch_one(
                    "SELECT lease_until FROM py_task_results WHERE id=?", (task_id,)
                )["lease_until"]
            )
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE py_task_results SET status='completed', updated_at=? WHERE id=?",
                    (db.utcnow(), task_id),
                )

        monkeypatch.setitem(
            knowledge_router.TASK_WORKERS, "knowledge_reparse", record_worker
        )
        monkeypatch.setattr(knowledge_router, "TASK_WORKER_LEASE_SECONDS", 0.05)
        monkeypatch.setattr(knowledge_router, "TASK_WORKER_RENEW_SECONDS", 0.01)

        assert knowledge_router.schedule_task_worker(task_id) is True
        assert knowledge_router.schedule_task_worker(task_id) is False
        for _ in range(500):
            row = db.fetch_one("SELECT status FROM py_task_results WHERE id=?", (task_id,))
            if (
                row
                and row["status"] == "completed"
                and task_id not in knowledge_router._task_worker_claims
            ):
                break
            await asyncio.sleep(0.01)
        assert calls == [task_id]
        assert len(leases) == 2
        assert float(leases[1]) > float(leases[0])
        assert task_id not in knowledge_router._task_worker_claims

    _run(case)


def test_task_worker_retries_until_budget_is_exhausted(monkeypatch) -> None:
    async def case() -> None:
        task_id = db.new_id()
        now = db.utcnow()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO py_task_results"
                "(id, tenant_id, kind, status, total, processed, payload, result, error, max_retry, created_at, updated_at) "
                "VALUES (?, 1, 'knowledge_reparse', 'pending', 1, 0, ?, '{}', '', 1, ?, ?)",
                (
                    task_id,
                    json.dumps({"kb_id": "", "knowledge_ids": ["missing-knowledge"]}),
                    now,
                    now,
                ),
            )

        monkeypatch.setattr(knowledge_router, "TASK_RETRY_BASE_SECONDS", 0.01)
        assert knowledge_router.schedule_task_worker(task_id) is True
        for _ in range(500):
            row = db.fetch_one(
                "SELECT status, retry_count, max_retry FROM py_task_results WHERE id=?",
                (task_id,),
            )
            if (
                row
                and row["status"] == "failed"
                and task_id not in knowledge_router._task_worker_claims
            ):
                break
            await asyncio.sleep(0.01)
        assert row is not None
        assert row["status"] == "failed"
        assert row["retry_count"] == 1
        assert row["max_retry"] == 1
        assert task_id not in knowledge_router._knowledge_task_workers

    _run(case)


def test_faq_import_worker_uses_claim_and_retry_budget(monkeypatch) -> None:
    async def case() -> None:
        task_id = db.new_id()
        now = db.utcnow()
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO py_task_results"
                "(id, tenant_id, kind, status, total, processed, payload, result, error, max_retry, created_at, updated_at) "
                "VALUES (?, 1, 'faq_import', 'pending', 0, 0, ?, '{}', '', 1, ?, ?)",
                (
                    task_id,
                    json.dumps(
                        {
                            "tenant_id": 1,
                            "kb_id": "missing-kb",
                            "entries": [],
                            "mode": "append",
                            "dry_run": False,
                        }
                    ),
                    now,
                    now,
                ),
            )

        calls = 0

        def fail_clean(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise RuntimeError("transient FAQ failure")

        monkeypatch.setattr(knowledge_router, "_faq_clean_entries", fail_clean)
        monkeypatch.setattr(knowledge_router, "TASK_RETRY_BASE_SECONDS", 0.01)

        assert knowledge_router.schedule_task_worker(task_id) is True
        assert knowledge_router.schedule_task_worker(task_id) is False
        for _ in range(500):
            row = db.fetch_one(
                "SELECT status, retry_count, max_retry FROM py_task_results WHERE id=?",
                (task_id,),
            )
            if (
                row
                and row["status"] == "failed"
                and task_id not in knowledge_router._task_worker_claims
            ):
                break
            await asyncio.sleep(0.01)
        assert row is not None
        assert row["status"] == "failed"
        assert row["retry_count"] == 1
        assert row["max_retry"] == 1
        assert calls == 2

    _run(case)


def test_knowledge_transfer_validation_matches_upstream() -> None:
    async def case() -> None:
        async with await _client() as client:
            login = await _login(client)
            headers = _headers(login)

            async def create_kb(name: str) -> str:
                response = await client.post(
                    "/api/v1/knowledge-bases", headers=headers, json={"name": name}
                )
                assert response.status_code == 201, response.text
                return response.json()["data"]["id"]

            source_id = await create_kb("Transfer validation source")
            target_id = await create_kb("Transfer validation target")
            other_id = await create_kb("Transfer validation other")
            manual = await client.post(
                f"/api/v1/knowledge-bases/{source_id}/knowledge/manual",
                headers=headers,
                json={"title": "Transfer validation document", "content": "validation"},
            )
            assert manual.status_code == 201, manual.text
            knowledge_id = manual.json()["data"]["id"]

            missing_source = await client.post(
                "/api/v1/knowledge-bases/copy", headers=headers, json={}
            )
            assert missing_source.status_code == 400, missing_source.text
            self_copy = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": source_id},
            )
            assert self_copy.status_code == 400, self_copy.text

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET embedding_model_id='other-model' WHERE id=?",
                    (target_id,),
                )
            model_mismatch = await client.post(
                "/api/v1/knowledge-bases/copy",
                headers=headers,
                json={"source_id": source_id, "target_id": target_id},
            )
            assert model_mismatch.status_code == 400, model_mismatch.text
            assert "embedding models" in model_mismatch.json()["error"]["message"]
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET embedding_model_id='' WHERE id=?", (target_id,)
                )

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET vector_store_id='other-store' WHERE id=?",
                    (target_id,),
                )
            vector_mismatch = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": target_id,
                    "knowledge_ids": [knowledge_id],
                    "mode": "reuse_vectors",
                },
            )
            assert vector_mismatch.status_code == 400, vector_mismatch.text
            assert "different vector stores" in vector_mismatch.json()["error"]["message"]

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET vector_store_id=NULL, type='faq' WHERE id=?",
                    (target_id,),
                )
            type_mismatch = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": target_id,
                    "knowledge_ids": [knowledge_id],
                    "mode": "reparse",
                },
            )
            assert type_mismatch.status_code == 400, type_mismatch.text
            assert "same type" in type_mismatch.json()["error"]["message"]
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledge_bases SET type='document' WHERE id=?", (target_id,)
                )

            missing_mode = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": target_id,
                    "knowledge_ids": [knowledge_id],
                },
            )
            assert missing_mode.status_code == 400, missing_mode.text
            missing_move_source = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "target_kb_id": target_id,
                    "knowledge_ids": [knowledge_id],
                    "mode": "reparse",
                },
            )
            assert missing_move_source.status_code == 400, missing_move_source.text
            wrong_source = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": other_id,
                    "target_kb_id": target_id,
                    "knowledge_ids": [knowledge_id],
                    "mode": "reparse",
                },
            )
            assert wrong_source.status_code == 400, wrong_source.text
            assert "does not belong" in wrong_source.json()["error"]["message"]

            with db.transaction() as conn:
                conn.execute(
                    "UPDATE knowledges SET parse_status='processing' WHERE id=?", (knowledge_id,)
                )
            incomplete = await client.post(
                "/api/v1/knowledge/move",
                headers=headers,
                json={
                    "source_kb_id": source_id,
                    "target_kb_id": target_id,
                    "knowledge_ids": [knowledge_id],
                    "mode": "reparse",
                },
            )
            assert incomplete.status_code == 400, incomplete.text
            assert "completed status" in incomplete.json()["error"]["message"]

    _run(case)


def test_api_key_route_policy_matrix() -> None:
    from app import api_keys

    def assert_policy(method: str, path: str, *, full: bool, caps: set[str] | None = None, platform: bool = False) -> None:
        policy = api_keys.policy_for(method, path)
        assert policy is not None, (method, path)
        assert policy.full_access is full, (method, path, policy)
        assert policy.platform_only is platform, (method, path, policy)
        assert policy.capabilities == frozenset(caps or ()), (method, path, policy)

    for method, path in (
        ("GET", "/api/v1/auth/me"),
        ("GET", "/api/v1/system/capabilities"),
        ("GET", "/files"),
    ):
        assert_policy(method, path, full=False)

    for method, path, caps in (
        (
            "GET",
            "/api/v1/system/admin/settings",
            {api_keys.SYSTEM_SETTINGS_READ, api_keys.SYSTEM_SETTINGS_MANAGE},
        ),
        (
            "GET",
            "/api/v1/tenants/all",
            {api_keys.SYSTEM_TENANTS_READ, api_keys.SYSTEM_TENANTS_MANAGE},
        ),
        ("POST", "/api/v1/tenants", {api_keys.SYSTEM_TENANTS_MANAGE}),
    ):
        assert_policy(method, path, full=False, platform=True, caps=caps)

    for method, path in (
        ("POST", "/api/v1/system/admin/api-keys"),
        ("POST", "/api/v1/system/host-project-dir"),
        ("GET", "/api/v1/knowledge-bases/{}/activity"),
        ("GET", "/api/v1/web-search/providers"),
        ("POST", "/api/v1/agent/tool-approvals/{}"),
        ("GET", "/api/v1/user/favorites"),
        ("GET", "/api/v1/skills/catalog"),
    ):
        assert api_keys.policy_for(method, path) is None, (method, path)

    for method, path in (
        ("GET", "/api/v1/im-channels"),
        ("GET", "/api/v1/embed-channels"),
        ("GET", "/api/v1/agents/{}/embed-channels"),
        ("GET", "/api/v1/mcp-endpoints"),
        ("POST", "/api/v1/wechat/qrcode"),
    ):
        assert_policy(method, path, full=True, caps={api_keys.MANAGE_CHANNELS})

    assert_policy(
        "GET", "/api/v1/web-search-providers", full=True, caps={api_keys.MANAGE_WEB_SEARCH}
    )
    assert_policy("GET", "/api/v1/knowledge/move/progress/{}", full=True, caps={api_keys.RETRIEVE})
    assert_policy(
        "POST",
        "/api/v1/knowledge/move",
        full=True,
        caps={api_keys.INGEST},
    )
    assert_policy(
        "GET",
        "/api/v1/faq/import/progress/{}",
        full=True,
        caps={api_keys.RETRIEVE, api_keys.INGEST},
    )
    assert_policy("DELETE", "/api/v1/knowledge-bases/{}/knowledge", full=True)
    assert_policy("POST", "/api/v1/sandbox-configs", full=True)
    assert_policy("POST", "/api/v1/skills/catalog", full=True)
    assert_policy("GET", "/api/v1/memory/items", full=True)
    assert_policy("GET", "/api/v1/agents", full=True, caps={api_keys.READ_AGENTS, api_keys.MANAGE_AGENTS, api_keys.CHAT})

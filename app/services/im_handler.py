"""Shared IM message handling for HTTP callbacks and long connections."""

from __future__ import annotations

import asyncio
import logging
import anyio.to_thread
from typing import Any

from .. import db
from ..utils import loads
from . import im as im_service, im_attachments


logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task[Any]] = set()


async def answer_message(
    channel: dict[str, Any],
    message: dict[str, Any],
) -> dict[str, Any]:
    channel_id = str(channel["id"])
    tenant_id = int(channel["tenant_id"])
    platform = str(channel.get("platform") or "")
    dedupe = im_service.dedupe_key(platform, message)
    if dedupe:
        existing = db.fetch_one(
            "SELECT session_id FROM py_im_messages WHERE channel_id=? AND dedupe_key=?",
            (channel_id, dedupe),
        )
        if existing:
            return {
                "session_id": existing["session_id"],
                "answer": "",
                "duplicate": True,
                "error": "",
            }

    session = _im_session(channel, message)
    session_id = str(session["id"])
    prepared = {"attachments": [], "images": []}
    if str(message.get("message_type") or "") in {"image", "file"}:
        try:
            prepared = await anyio.to_thread.run_sync(
                lambda: im_attachments.prepare_attachment(
                    {**message, "platform": platform}
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep the channel alive on bad media
            return {
                "session_id": session_id,
                "answer": "附件读取失败，请稍后重试。",
                "duplicate": False,
                "error": str(exc),
            }
        _schedule_attachment_persistence(channel, prepared)
    query = str(message.get("content") or "").strip() or _attachment_query(prepared)
    from ..routers.chat import stream_chat_events

    principal = f"im:{platform}:{message.get('user_id') or 'anonymous'}"
    answer = ""
    try:
        events = stream_chat_events(
            session=session,
            session_id=session_id,
            tenant_id=tenant_id,
            payload={
                "query": query,
                "attachments": prepared["attachments"],
                "images": prepared["images"],
                **({"knowledge_base_ids": [channel["knowledge_base_id"]]} if channel.get("knowledge_base_id") else {}),
            },
            agent_mode=True,
            channel=platform,
            user_id=principal,
            preferences="{}",
        )
        async for raw in events:
            text = raw.decode("utf-8", errors="replace")
            for line in text.splitlines():
                if not line.startswith("data: "):
                    continue
                try:
                    event = loads(line[6:], {})
                except Exception:  # noqa: BLE001
                    continue
                if event.get("response_type") == "answer" and event.get("content"):
                    answer += str(event["content"])
                elif event.get("response_type") == "complete":
                    answer = str((event.get("data") or {}).get("final_content") or answer)
    except Exception as exc:  # noqa: BLE001
        return {
            "session_id": session_id,
            "answer": "处理消息时发生错误，请稍后重试。",
            "duplicate": False,
            "error": str(exc),
        }

    if dedupe:
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO py_im_messages(channel_id, dedupe_key, session_id, created_at) VALUES (?, ?, ?, ?)",
                (channel_id, dedupe, session_id, db.utcnow()),
            )
    return {
        "session_id": session_id,
        "answer": answer,
        "duplicate": False,
        "error": "",
    }


def _attachment_query(prepared: dict[str, Any]) -> str:
    attachments = prepared.get("attachments") if isinstance(prepared.get("attachments"), list) else []
    name = "未命名文件"
    if attachments and isinstance(attachments[0], dict):
        name = str(attachments[0].get("file_name") or name)
    return f"我上传了文件「{name}」。请确认已收到，并告知我接下来可以如何协助。"


def _schedule_attachment_persistence(
    channel: dict[str, Any],
    prepared: dict[str, Any],
) -> None:
    download = prepared.get("download")
    if not channel.get("knowledge_base_id") or not isinstance(download, dict):
        return
    content = download.get("content")
    if not isinstance(content, bytes):
        return
    task = asyncio.create_task(
        anyio.to_thread.run_sync(
            _persist_attachment,
            channel,
            str(download.get("file_name") or "attachment"),
            content,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _persist_attachment(
    channel: dict[str, Any],
    file_name: str,
    content: bytes,
) -> None:
    try:
        from ..routers.knowledge import persist_im_attachment

        knowledge = persist_im_attachment(channel, file_name, content)
        if knowledge:
            logger.info(
                "IM attachment saved to knowledge base: kb=%s knowledge=%s file=%s",
                channel.get("knowledge_base_id"),
                knowledge.get("id"),
                file_name,
            )
    except Exception as exc:  # noqa: BLE001 - persistence must not affect the QA reply
        logger.warning("IM attachment persistence failed for %s: %s", file_name, exc)


def _im_session(channel: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    channel_id = str(channel["id"])
    tenant_id = int(channel["tenant_id"])
    external_user = str(message.get("user_id") or "")
    chat_id = str(message.get("chat_id") or "")
    thread_id = str(message.get("thread_id") or "")
    row = db.fetch_one(
        "SELECT session_id FROM py_im_sessions WHERE channel_id=? AND external_user_id=? AND chat_id=? AND thread_id=?",
        (channel_id, external_user, chat_id, thread_id),
    )
    if row:
        session = db.fetch_one(
            "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (row["session_id"], tenant_id),
        )
        if session:
            return session
    session_id = db.new_id()
    now = db.utcnow()
    title = str(message.get("user_name") or external_user or "IM user")
    principal = f"im:{channel.get('platform') or 'webhook'}:{external_user or 'anonymous'}"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sessions(id, tenant_id, title, description, max_rounds,
              enable_rewrite, fallback_strategy, fallback_response, keyword_threshold,
              vector_threshold, embedding_top_k, rerank_top_k, rerank_threshold,
              summary_parameters, agent_config, context_config, agent_id, user_id,
              is_pinned, created_at, updated_at, sandbox_config_tenant_id)
            VALUES (?, ?, ?, '', 5, 1, 'fixed', '很抱歉，我暂时无法回答这个问题。', 0.5,
              0.5, 10, 10, 0.65, '{}', '{}', '{}', ?, ?, 0, ?, ?, 0)
            """,
            (session_id, tenant_id, title, channel.get("agent_id") or None, principal, now, now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO py_im_sessions(channel_id, tenant_id, external_user_id, chat_id, thread_id, session_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, tenant_id, external_user, chat_id, thread_id, session_id, now, now),
        )
    return db.fetch_one("SELECT * FROM sessions WHERE id=?", (session_id,))

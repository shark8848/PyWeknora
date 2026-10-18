"""Runtime lifecycle for IM channel transports."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .. import db
from ..utils import loads
from . import im_handler, im_leases, wecom_longconn


_RENEW_INTERVAL = 5.0
_RETRY_INTERVAL = 10.0
_INSTANCE_KEY = uuid.uuid4().hex
_active = False
_loop: asyncio.AbstractEventLoop | None = None
_clients: dict[str, wecom_longconn.WeComLongConn] = {}
_tasks: dict[str, asyncio.Task[None]] = {}
_lease_tasks: dict[str, asyncio.Task[None]] = {}
_retry_tasks: dict[str, asyncio.Task[None]] = {}


async def start() -> None:
    global _active, _loop
    if _active:
        return
    _active = True
    _loop = asyncio.get_running_loop()
    rows = db.fetch_all(
        "SELECT * FROM im_channels WHERE platform='wecom' AND mode='websocket' "
        "AND enabled=1 AND deleted_at IS NULL ORDER BY created_at, id"
    )
    for row in rows:
        await _start_channel(row)


async def shutdown() -> None:
    global _active, _loop
    _active = False
    for channel_id in set(_clients) | set(_tasks) | set(_lease_tasks) | set(_retry_tasks):
        await _stop_channel(channel_id)
    _loop = None


def notify_channel(channel_id: str) -> None:
    """Refresh one channel after CRUD when the application runtime is active."""
    loop = _loop
    if not _active or loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        loop.create_task(sync_channel(channel_id))
    else:
        asyncio.run_coroutine_threadsafe(sync_channel(channel_id), loop)


async def sync_channel(channel_id: str) -> None:
    if not _active:
        return
    await _stop_channel(channel_id)
    row = db.fetch_one(
        "SELECT * FROM im_channels WHERE id=? AND deleted_at IS NULL",
        (channel_id,),
    )
    if row:
        await _start_channel(row)


async def _start_channel(row: dict[str, Any]) -> None:
    if not _active:
        return
    if str(row.get("platform") or "") != "wecom" or str(row.get("mode") or "") != "websocket":
        return
    if not bool(row.get("enabled")):
        return
    credentials = loads(row.get("credentials"), {})
    if not isinstance(credentials, dict):
        credentials = {}
    bot_id = str(credentials.get("bot_id") or "").strip()
    secret = str(credentials.get("bot_secret") or "").strip()
    if not bot_id or not secret:
        return

    channel_id = str(row["id"])
    if channel_id in _clients:
        return
    if not im_leases.acquire(channel_id, _INSTANCE_KEY):
        _schedule_retry(channel_id)
        return

    retry_task = _retry_tasks.pop(channel_id, None)
    if retry_task is not None and retry_task is not asyncio.current_task():
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)

    holder: dict[str, wecom_longconn.WeComLongConn] = {}

    async def handle(message: dict[str, Any]) -> None:
        client = holder["client"]
        req_id = str(message.get("reply_request_id") or "")
        message_type = str(message.get("message_type") or "")
        if message_type not in {"text", "image", "file"} or (
            message_type == "text" and not message.get("content")
        ):
            await _send_reply(client, req_id, "目前暂不支持该消息类型。")
            return
        result = await im_handler.answer_message(row, message)
        if result["duplicate"]:
            return
        reply = str(result["answer"] or result["error"] or "处理消息时发生错误，请稍后重试。")
        await _send_reply(client, req_id, reply)

    try:
        client = wecom_longconn.WeComLongConn(
            bot_id,
            secret,
            endpoint=str(credentials.get("ws_endpoint") or ""),
            bot_name=str(credentials.get("bot_name") or ""),
            handler=handle,
        )
    except Exception:  # noqa: BLE001 - invalid channel config stays isolated
        im_leases.release(channel_id, _INSTANCE_KEY)
        return
    holder["client"] = client
    _clients[channel_id] = client
    task = asyncio.create_task(client.run())
    _tasks[channel_id] = task
    task.add_done_callback(lambda done, cid=channel_id: _client_finished(cid, done))
    _lease_tasks[channel_id] = asyncio.create_task(_renew_lease(channel_id))


def _client_finished(channel_id: str, task: asyncio.Task[None]) -> None:
    if _tasks.get(channel_id) is not task:
        return
    _tasks.pop(channel_id, None)
    loop = _loop
    if _active and loop is not None and not loop.is_closed():
        loop.create_task(_recover_client(channel_id))


async def _recover_client(channel_id: str) -> None:
    if not _active or channel_id not in _clients:
        return
    await _stop_channel(channel_id)
    _schedule_retry(channel_id)


async def _renew_lease(channel_id: str) -> None:
    current = asyncio.current_task()
    try:
        while _active and channel_id in _clients:
            await asyncio.sleep(_RENEW_INTERVAL)
            if not _active or channel_id not in _clients:
                return
            if im_leases.renew(channel_id, _INSTANCE_KEY):
                continue
            await _stop_channel(channel_id, keep_lease_task=current)
            _schedule_retry(channel_id)
            return
    finally:
        if _lease_tasks.get(channel_id) is current:
            _lease_tasks.pop(channel_id, None)


def _schedule_retry(channel_id: str) -> None:
    if not _active or channel_id in _clients or channel_id in _retry_tasks:
        return
    loop = _loop
    if loop is None or loop.is_closed():
        return
    _retry_tasks[channel_id] = loop.create_task(_retry_lease(channel_id))


async def _retry_lease(channel_id: str) -> None:
    current = asyncio.current_task()
    try:
        while _active and channel_id not in _clients:
            await asyncio.sleep(_RETRY_INTERVAL)
            if not _active:
                return
            row = db.fetch_one(
                "SELECT * FROM im_channels WHERE id=? AND deleted_at IS NULL",
                (channel_id,),
            )
            if not row:
                return
            await _start_channel(row)
            if channel_id in _clients:
                return
    finally:
        if _retry_tasks.get(channel_id) is current:
            _retry_tasks.pop(channel_id, None)


async def _stop_channel(
    channel_id: str,
    *,
    keep_lease_task: asyncio.Task[None] | None = None,
) -> None:
    retry = _retry_tasks.pop(channel_id, None)
    if retry is not None and retry is not asyncio.current_task():
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)
    client = _clients.pop(channel_id, None)
    task = _tasks.pop(channel_id, None)
    if client is not None:
        await client.stop()
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    lease_task = _lease_tasks.pop(channel_id, None)
    if lease_task is not None and lease_task is not keep_lease_task:
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
    im_leases.release(channel_id, _INSTANCE_KEY)


async def _send_reply(
    client: wecom_longconn.WeComLongConn,
    req_id: str,
    content: str,
) -> None:
    for attempt in range(4):
        try:
            await client.send_reply(req_id, content)
            return
        except Exception:  # noqa: BLE001 - retry through reconnect windows
            if attempt == 3:
                return
            await asyncio.sleep(0.5)

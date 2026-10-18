"""WeCom intelligent-bot WebSocket long connection."""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from typing import Any, Awaitable, Callable

from . import url_guard


DEFAULT_WS_ENDPOINT = "wss://openws.work.weixin.qq.com"
HEARTBEAT_INTERVAL = 30.0
READ_TIMEOUT = 90.0
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0
MAX_FRAME_SIZE = 4 * 1024 * 1024

CMD_SUBSCRIBE = "aibot_subscribe"
CMD_PING = "ping"
CMD_MSG_CALLBACK = "aibot_msg_callback"
CMD_EVENT_CALLBACK = "aibot_event_callback"
CMD_RESPONSE = "aibot_respond_msg"


class WeComLongConnError(RuntimeError):
    pass


async def _default_connect(endpoint: str):
    import websockets

    return await websockets.connect(
        endpoint,
        open_timeout=10,
        close_timeout=5,
        ping_interval=None,
        max_size=MAX_FRAME_SIZE,
        proxy=None,
    )


def validate_endpoint(endpoint: str) -> str:
    value = str(endpoint or DEFAULT_WS_ENDPOINT).strip().rstrip("/")
    if value == DEFAULT_WS_ENDPOINT:
        return value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "wss" or not parsed.hostname:
        raise WeComLongConnError("invalid ws_endpoint: endpoint must use wss:// scheme")
    masked = urllib.parse.urlunsplit(
        ("https", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    )
    try:
        url_guard.validate_url_for_ssrf(masked)
    except url_guard.URLGuardError as exc:
        raise WeComLongConnError(
            "invalid ws_endpoint: "
            + str(exc)
            + " (for private deployments on internal networks, add the hostname to SSRF_WHITELIST)"
        ) from exc
    return value


def reconnect_delay(attempt: int) -> float:
    return min(RECONNECT_BASE_DELAY * (2 ** max(0, attempt - 1)), RECONNECT_MAX_DELAY)


def strip_at_mention(content: str, bot_name: str = "") -> str:
    value = str(content or "").strip()
    if not value.startswith("@"):
        return value
    if "  " in value:
        return value.split("  ", 1)[1].strip()
    if bot_name:
        prefix = "@" + bot_name
        if value == prefix:
            return ""
        if value.startswith(prefix + " "):
            return value[len(prefix):].strip()
    for index, char in enumerate(value[1:], start=1):
        if char == " " and index + 1 < len(value):
            following = value[index + 1]
            if following == "/" or ord(following) >= 0x80:
                return value[index + 1:].strip()
    parts = value.split(" ", 1)
    return parts[1].strip() if len(parts) == 2 else value


def parse_callback_message(
    body: dict[str, Any],
    req_id: str,
    bot_name: str = "",
) -> dict[str, Any] | None:
    msg_type = str(body.get("msgtype") or "")
    if msg_type == "event":
        return None
    from_data = body.get("from") if isinstance(body.get("from"), dict) else {}
    user_id = str(from_data.get("userid") or "")
    chat_type = str(body.get("chattype") or "single")
    is_group = chat_type == "group"
    chat_id = str(body.get("chatid") or "") if is_group else ""
    message_id = str(body.get("msgid") or "")
    base = {
        "platform": "wecom",
        "user_id": user_id,
        "user_name": user_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_id": message_id,
        "thread_id": "",
        "reply_request_id": req_id,
        "extra": {"req_id": req_id},
    }
    if msg_type == "text":
        text = body.get("text") if isinstance(body.get("text"), dict) else {}
        content = str(text.get("content") or "")
        if is_group:
            content = strip_at_mention(content, bot_name)
        return {
            **base,
            "message_type": "text",
            "content": content.strip(),
        }
    if msg_type == "voice":
        voice = body.get("voice") if isinstance(body.get("voice"), dict) else {}
        return {
            **base,
            "message_type": "text",
            "content": str(voice.get("content") or "").strip(),
        }
    if msg_type in {"image", "file"}:
        attachment = body.get(msg_type) if isinstance(body.get(msg_type), dict) else {}
        return {
            **base,
            "message_type": msg_type,
            "content": "",
            "file_key": str(attachment.get("url") or ""),
            "file_name": message_id + (".png" if msg_type == "image" else ""),
            "extra": {
                **base["extra"],
                "aes_key": str(attachment.get("aeskey") or ""),
            },
        }
    if msg_type == "mixed":
        mixed = body.get("mixed") if isinstance(body.get("mixed"), dict) else {}
        text_parts: list[str] = []
        first_image: dict[str, Any] | None = None
        for item in mixed.get("msg_item") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("msgtype") or "")
            if item_type == "text":
                text = item.get("text") if isinstance(item.get("text"), dict) else {}
                part = str(text.get("content") or "")
                if is_group:
                    part = strip_at_mention(part, bot_name)
                if part.strip():
                    text_parts.append(part.strip())
            elif item_type == "image" and first_image is None:
                image = item.get("image") if isinstance(item.get("image"), dict) else {}
                if image.get("url"):
                    first_image = image
        content = "\n".join(text_parts)
        if first_image is not None:
            return {
                **base,
                "message_type": "image",
                "content": content,
                "file_key": str(first_image.get("url") or ""),
                "file_name": message_id + ".png",
                "extra": {
                    **base["extra"],
                    "aes_key": str(first_image.get("aeskey") or ""),
                },
            }
        if content:
            return {**base, "message_type": "text", "content": content}
    return None


class WeComLongConn:
    def __init__(
        self,
        bot_id: str,
        secret: str,
        *,
        endpoint: str = "",
        bot_name: str = "",
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        connect: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self.bot_id = str(bot_id or "")
        self.secret = str(secret or "")
        self.endpoint = validate_endpoint(endpoint)
        self.bot_name = str(bot_name or "")
        self.handler = handler
        self._connect = connect or _default_connect
        self._conn: Any = None
        self._send_lock = asyncio.Lock()
        self._stopped = False
        self._stream_seq = 0
        self._callback_tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        attempts = 0
        while not self._stopped:
            connected_at = time.monotonic()
            try:
                await self._connect_and_run()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect loop owns connection failures
                if self._stopped:
                    return
            if self._stopped:
                return
            if time.monotonic() - connected_at > RECONNECT_MAX_DELAY:
                attempts = 0
            attempts += 1
            await asyncio.sleep(reconnect_delay(attempts))

    async def stop(self) -> None:
        self._stopped = True
        await self._close_current(self._conn)
        tasks = list(self._callback_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._callback_tasks.clear()

    async def send_reply(self, req_id: str, content: str) -> None:
        if not req_id:
            raise WeComLongConnError("missing req_id in incoming message")
        self._stream_seq += 1
        await self._send_frame({
            "cmd": CMD_RESPONSE,
            "headers": {"req_id": req_id},
            "body": {
                "msgtype": "stream",
                "stream": {
                    "id": f"stream_{self._stream_seq}",
                    "finish": True,
                    "content": content,
                },
            },
        })

    async def _connect_and_run(self) -> None:
        conn = await self._connect(self.endpoint)
        self._conn = conn
        try:
            await self._authenticate()
            heartbeat = asyncio.create_task(self._heartbeat(conn))
            try:
                while not self._stopped:
                    try:
                        raw = await asyncio.wait_for(conn.recv(), timeout=READ_TIMEOUT)
                    except asyncio.TimeoutError as exc:
                        raise WeComLongConnError("WeCom read timeout") from exc
                    await self._handle_frame(raw)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        finally:
            await self._close_current(conn)

    async def _authenticate(self) -> None:
        req_id = f"{CMD_SUBSCRIBE}_{time.time_ns()}"
        await self._send_frame({
            "cmd": CMD_SUBSCRIBE,
            "headers": {"req_id": req_id},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        })
        conn = self._conn
        if conn is None:
            raise WeComLongConnError("connection closed during subscribe")
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=10)
        except asyncio.TimeoutError as exc:
            raise WeComLongConnError("WeCom subscribe timed out") from exc
        response = self._decode(raw)
        if int(response.get("errcode") or 0) != 0:
            raise WeComLongConnError(
                f"WeCom subscribe failed: code={response.get('errcode')} "
                f"msg={response.get('errmsg') or ''}"
            )

    async def _heartbeat(self, conn: Any) -> None:
        while not self._stopped and conn is self._conn:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self._send_frame({
                    "cmd": CMD_PING,
                    "headers": {"req_id": f"{CMD_PING}_{time.time_ns()}"},
                })
            except Exception:  # noqa: BLE001 - close to trigger reconnect
                await self._close_current(conn)
                return

    async def _handle_frame(self, raw: Any) -> None:
        frame = self._decode(raw)
        command = str(frame.get("cmd") or "")
        if command not in {CMD_MSG_CALLBACK, CMD_EVENT_CALLBACK}:
            return
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        if command == CMD_EVENT_CALLBACK or str(body.get("msgtype") or "") == "event":
            event = body.get("event") if isinstance(body.get("event"), dict) else {}
            if str(event.get("eventtype") or "") == "disconnected_event":
                await self._close_current(self._conn)
            return
        headers = frame.get("headers") if isinstance(frame.get("headers"), dict) else {}
        req_id = str(headers.get("req_id") or "")
        message = parse_callback_message(body, req_id, self.bot_name)
        if message is None:
            return
        task = asyncio.create_task(self._run_handler(message))
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)

    async def _run_handler(self, message: dict[str, Any]) -> None:
        try:
            await self.handler(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad callback must not kill the socket
            return

    async def _send_frame(self, frame: dict[str, Any]) -> None:
        conn = self._conn
        if conn is None:
            raise WeComLongConnError("connection closed")
        payload = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            await conn.send(payload)

    async def _close_current(self, conn: Any) -> None:
        if conn is None:
            return
        if self._conn is conn:
            self._conn = None
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 - closing an already broken socket is fine
            return

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise WeComLongConnError("invalid WeCom WebSocket frame") from exc
        if not isinstance(value, dict):
            raise WeComLongConnError("invalid WeCom WebSocket frame")
        return value

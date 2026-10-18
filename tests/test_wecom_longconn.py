"""Unit tests for the WeCom intelligent-bot long connection."""

from __future__ import annotations

import asyncio
import json

import anyio
import pytest

from app.services import im_runtime, wecom_longconn


class _Closed(Exception):
    pass


class FakeConnection:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[object] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return str(value)

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(_Closed())


async def _wait_for(predicate, timeout: float = 2) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for condition")


def test_wecom_endpoint_validation_masks_wss_for_ssrf(monkeypatch) -> None:
    calls: list[str] = []

    def validate(url: str):
        calls.append(url)

    monkeypatch.setattr(wecom_longconn.url_guard, "validate_url_for_ssrf", validate)
    assert wecom_longconn.validate_endpoint("") == wecom_longconn.DEFAULT_WS_ENDPOINT
    assert (
        wecom_longconn.validate_endpoint("wss://bots.internal.example/ws/")
        == "wss://bots.internal.example/ws"
    )
    assert calls == ["https://bots.internal.example/ws"]
    with pytest.raises(wecom_longconn.WeComLongConnError, match="wss://"):
        wecom_longconn.validate_endpoint("ws://bots.internal.example/ws")


def test_wecom_callback_parsing_matches_long_connection_shapes() -> None:
    text = wecom_longconn.parse_callback_message(
        {
            "msgid": "msg-1",
            "chatid": "chat-1",
            "chattype": "group",
            "msgtype": "text",
            "from": {"userid": "user-1"},
            "text": {"content": "@WeKnora Bot  /stop"},
        },
        "req-1",
        "WeKnora Bot",
    )
    assert text == {
        "platform": "wecom",
        "user_id": "user-1",
        "user_name": "user-1",
        "chat_id": "chat-1",
        "chat_type": "group",
        "message_id": "msg-1",
        "thread_id": "",
        "reply_request_id": "req-1",
        "extra": {"req_id": "req-1"},
        "message_type": "text",
        "content": "/stop",
    }

    mixed = wecom_longconn.parse_callback_message(
        {
            "msgid": "msg-2",
            "chattype": "single",
            "msgtype": "mixed",
            "from": {"userid": "user-2"},
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "what is this?"}},
                    {"msgtype": "image", "image": {"url": "https://example.test/a", "aeskey": "key"}},
                ]
            },
        },
        "req-2",
    )
    assert mixed is not None
    assert mixed["message_type"] == "image"
    assert mixed["content"] == "what is this?"
    assert mixed["file_key"] == "https://example.test/a"
    assert mixed["extra"]["aes_key"] == "key"


def test_wecom_long_connection_subscribes_handles_and_replies() -> None:
    async def case() -> None:
        connection = FakeConnection()
        received: list[dict] = []
        holder: dict[str, wecom_longconn.WeComLongConn] = {}

        async def handler(message: dict) -> None:
            received.append(message)
            await holder["client"].send_reply(message["reply_request_id"], "answer")

        async def connect(_endpoint: str):
            return connection

        client = wecom_longconn.WeComLongConn(
            "bot-1",
            "secret-1",
            handler=handler,
            connect=connect,
        )
        holder["client"] = client
        task = asyncio.create_task(client.run())
        try:
            await _wait_for(lambda: bool(connection.sent))
            subscribe = connection.sent[0]
            assert subscribe["cmd"] == "aibot_subscribe"
            assert subscribe["body"] == {"bot_id": "bot-1", "secret": "secret-1"}

            await connection.incoming.put(json.dumps({"errcode": 0}))
            await connection.incoming.put(
                json.dumps(
                    {
                        "cmd": "aibot_msg_callback",
                        "headers": {"req_id": "req-1"},
                        "body": {
                            "msgid": "msg-1",
                            "chattype": "single",
                            "msgtype": "text",
                            "from": {"userid": "user-1"},
                            "text": {"content": "hello"},
                        },
                    }
                )
            )
            await _wait_for(lambda: len(connection.sent) >= 2)
            assert received[0]["content"] == "hello"
            reply = connection.sent[-1]
            assert reply["cmd"] == "aibot_respond_msg"
            assert reply["headers"] == {"req_id": "req-1"}
            assert reply["body"]["stream"]["finish"] is True
            assert reply["body"]["stream"]["content"] == "answer"
        finally:
            await client.stop()
            await asyncio.gather(task, return_exceptions=True)
        assert connection.closed is True

    anyio.run(case)


def test_wecom_reconnect_delay_is_bounded() -> None:
    assert [wecom_longconn.reconnect_delay(n) for n in (1, 2, 3, 6)] == [1, 2, 4, 30]
    assert wecom_longconn.reconnect_delay(1000) == 30


def test_wecom_runtime_syncs_enabled_channels(monkeypatch) -> None:
    channel = {
        "id": "channel-1",
        "platform": "wecom",
        "mode": "websocket",
        "enabled": 1,
        "credentials": json.dumps({"bot_id": "bot-1", "bot_secret": "secret-1"}),
    }
    state = {"channel": channel}

    class FakeClient:
        instances: list["FakeClient"] = []

        def __init__(self, *args, **kwargs) -> None:
            self.stopped = False
            self.done = asyncio.Event()
            self.instances.append(self)

        async def run(self) -> None:
            await self.done.wait()

        async def stop(self) -> None:
            self.stopped = True
            self.done.set()

    monkeypatch.setattr(im_runtime.db, "fetch_all", lambda _sql: [channel])
    monkeypatch.setattr(im_runtime.db, "fetch_one", lambda _sql, _params: state["channel"])
    monkeypatch.setattr(im_runtime.im_leases, "acquire", lambda _channel, _key: True)
    monkeypatch.setattr(im_runtime.im_leases, "renew", lambda _channel, _key: True)
    monkeypatch.setattr(im_runtime.im_leases, "release", lambda _channel, _key: None)
    monkeypatch.setattr(im_runtime.wecom_longconn, "WeComLongConn", FakeClient)

    async def case() -> None:
        try:
            await im_runtime.start()
            assert len(FakeClient.instances) == 1
            client = FakeClient.instances[0]
            assert "channel-1" in im_runtime._clients

            state["channel"] = {**channel, "enabled": 0}
            await im_runtime.sync_channel("channel-1")
            assert client.stopped is True
            assert "channel-1" not in im_runtime._clients
        finally:
            await im_runtime.shutdown()

    anyio.run(case)


def test_wecom_runtime_waits_when_another_process_owns_the_channel(monkeypatch) -> None:
    channel = {
        "id": "channel-2",
        "platform": "wecom",
        "mode": "websocket",
        "enabled": 1,
        "credentials": json.dumps({"bot_id": "bot-2", "bot_secret": "secret-2"}),
    }
    monkeypatch.setattr(im_runtime.db, "fetch_all", lambda _sql: [channel])
    monkeypatch.setattr(im_runtime.im_leases, "acquire", lambda _channel, _key: False)
    monkeypatch.setattr(im_runtime.im_leases, "release", lambda _channel, _key: None)

    async def case() -> None:
        await im_runtime.start()
        assert "channel-2" not in im_runtime._clients
        assert "channel-2" in im_runtime._retry_tasks
        await im_runtime.shutdown()
        assert not im_runtime._retry_tasks

    anyio.run(case)


def test_wecom_runtime_stops_adapter_when_lease_renewal_is_lost(monkeypatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    client = FakeClient()
    retries: list[str] = []

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(im_runtime.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(im_runtime.im_leases, "renew", lambda _channel, _key: False)
    monkeypatch.setattr(im_runtime.im_leases, "release", lambda _channel, _key: None)
    monkeypatch.setattr(im_runtime, "_schedule_retry", retries.append)

    async def case() -> None:
        im_runtime._active = True
        im_runtime._clients["channel-3"] = client
        try:
            await im_runtime._renew_lease("channel-3")
            assert client.stopped is True
            assert retries == ["channel-3"]
        finally:
            im_runtime._clients.pop("channel-3", None)
            im_runtime._lease_tasks.pop("channel-3", None)
            im_runtime._active = False

    anyio.run(case)

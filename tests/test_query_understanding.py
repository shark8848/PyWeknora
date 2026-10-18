from __future__ import annotations

import asyncio
import json

import pytest

from app.routers import chat as chat_router
from app.services import llm, query_understanding


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("kb_search", True),
        ("clarification", True),
        ("", True),
        ("summarize", False),
        ("web_search", False),
        ("greeting", False),
        ("chitchat", False),
        ("image_only", False),
        ("doc_only", False),
        ("unknown", False),
    ],
)
def test_needs_kb_retrieval_matches_go(intent: str, expected: bool) -> None:
    assert query_understanding.needs_kb_retrieval(intent) is expected


@pytest.mark.parametrize(
    ("intent", "web_enabled", "expected"),
    [
        ("kb_search", False, True),
        ("summarize", False, False),
        ("web_search", True, True),
        ("web_search", False, False),
    ],
)
def test_needs_retrieval_handles_web_search(
    intent: str, web_enabled: bool, expected: bool
) -> None:
    assert (
        query_understanding.needs_retrieval(
            intent, web_search_enabled=web_enabled
        )
        is expected
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" KB-Search ", "kb_search"),
        ("summarize", "summarize"),
        ("web search", "web_search"),
        ("unknown-label", ""),
        ("", ""),
    ],
)
def test_normalize_intent(raw: str, expected: str) -> None:
    assert query_understanding.normalize_intent(raw) == expected


def test_parse_output_accepts_plain_and_wrapped_json() -> None:
    plain = query_understanding.parse_output(
        '{"rewrite_query":"standalone","intent":"summarize"}'
    )
    assert plain == {
        "rewrite_query": "standalone",
        "intent": "summarize",
        "image_description": "",
    }

    wrapped = query_understanding.parse_output(
        'Here is the answer:\n```json\n'
        '{"query":"rewritten","intent":"KB-Search","description":"screen"}\n'
        "```"
    )
    assert wrapped == {
        "rewrite_query": "rewritten",
        "intent": "kb_search",
        "image_description": "screen",
    }


def test_parse_output_merges_ocr_and_salvages_truncated_description() -> None:
    parsed = query_understanding.parse_output(
        '{"rewrite_query":"q","intent":"summarize",'
        '"image_description":"caption","ocr_text":"text"}'
    )
    assert parsed is not None
    assert parsed["image_description"] == "caption\n\n[OCR]\ntext"

    truncated = query_understanding.parse_output(
        '{"rewrite_query":"q","intent":"summarize",'
        '"image_description":"partial caption'
    )
    assert truncated == {
        "rewrite_query": "q",
        "intent": "summarize",
        "image_description": "partial caption",
    }

    cut_rewrite = query_understanding.parse_output('{"rewrite_query":"partial')
    assert cut_rewrite is None


def test_understand_query_uses_conversation_and_falls_back(monkeypatch) -> None:
    messages_seen: list[list[dict]] = []

    def fake_chat(_runtime, messages):
        messages_seen.append(messages)
        return {
            "role": "assistant",
            "content": '{"rewrite_query":"summarize our launch decision","intent":"summarize"}',
        }

    result = query_understanding.understand_query(
        "总结一下我们刚才聊了什么",
        [{"role": "user", "content": "we chose kestrel"}, {"role": "assistant", "content": "understood"}],
        runtime={"base_url": "https://llm.test/v1"},
        chat=fake_chat,
    )
    assert result.rewrite_query == "summarize our launch decision"
    assert result.intent == "summarize"
    assert result.classified is True
    assert "user: we chose kestrel" in messages_seen[0][1]["content"]

    fallback = query_understanding.understand_query(
        "original",
        [],
        runtime={"base_url": "https://llm.test/v1"},
        chat=lambda *_args: None,
    )
    assert fallback.rewrite_query == "original"
    assert fallback.intent == ""
    assert fallback.classified is False

    unavailable = query_understanding.understand_query(
        "original", [], runtime=None, chat=lambda *_args: pytest.fail("must not call")
    )
    assert unavailable.intent == ""


def _run_stream(monkeypatch, understanding: str, *, rewrite_enabled: bool = True):
    calls: dict[str, list] = {"retrieve": [], "chat": 0}
    monkeypatch.setattr(
        chat_router,
        "resolve_config",
        lambda *_args, **_kwargs: {"enable_rewrite": rewrite_enabled},
    )
    monkeypatch.setattr(
        chat_router.model_runtime,
        "chat_runtime",
        lambda *_args, **_kwargs: {
            "name": "fake",
            "base_url": "https://llm.test/v1",
            "supports_vision": False,
        },
    )
    monkeypatch.setattr(
        chat_router.model_runtime,
        "available",
        lambda runtime: bool(runtime and runtime.get("base_url")),
    )
    monkeypatch.setattr(
        chat_router.model_runtime,
        "embedding_runtime",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        chat_router, "_create_message", lambda **kwargs: "message-" + kwargs["role"]
    )
    monkeypatch.setattr(chat_router, "_history", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        chat_router, "_session_knowledge_bases", lambda *_args, **_kwargs: ["kb-1"]
    )
    monkeypatch.setattr(
        chat_router, "_extract_knowledge_ids", lambda *_args, **_kwargs: []
    )

    def fake_retrieve(tenant_id, query, **_kwargs):
        calls["retrieve"].append((tenant_id, query))
        return []

    monkeypatch.setattr(chat_router, "retrieve", fake_retrieve)
    monkeypatch.setattr(chat_router, "references", lambda _rows: [])
    monkeypatch.setattr(chat_router, "build_context", lambda _rows: "")
    monkeypatch.setattr(chat_router, "generate_answer", lambda *_a, **_k: "summary answer")
    monkeypatch.setattr(chat_router, "_claim_steer_follow_up", lambda **_kwargs: None)
    monkeypatch.setattr(chat_router, "_update_message", lambda *_a, **_k: None)

    def fake_chat(messages, tools=None):
        calls["chat"] += 1
        assert "Conversation History" in messages[1]["content"]
        return {"role": "assistant", "content": understanding}

    monkeypatch.setattr(llm, "_remote_chat", fake_chat)

    async def collect() -> list[dict]:
        session = {
            "id": "session-1",
            "tenant_id": 1,
            "title": "Existing title",
            "agent_id": "",
            "agent_config": "{}",
        }
        return [
            json.loads(frame.decode().split("data: ", 1)[1])
            async for frame in chat_router.stream_chat_events(
                session=session,
                session_id="session-1",
                tenant_id=1,
                payload={"query": "原问题"},
                agent_mode=False,
                channel="web",
            )
        ]

    return asyncio.run(collect()), calls


def test_summarize_intent_skips_kb_retrieval(monkeypatch) -> None:
    events, calls = _run_stream(
        monkeypatch,
        '{"rewrite_query":"总结我们的对话","intent":"summarize"}',
    )

    assert calls["chat"] == 1
    assert calls["retrieve"] == []
    complete = next(event for event in events if event["response_type"] == "complete")
    assert complete["data"]["final_content"] == "summary answer"


def test_kb_search_intent_retrieves_rewritten_query(monkeypatch) -> None:
    events, calls = _run_stream(
        monkeypatch,
        '{"rewrite_query":"launch window marker","intent":"kb_search"}',
    )

    assert calls["chat"] == 1
    assert calls["retrieve"] == [(1, "launch window marker")]
    assert any(event["response_type"] == "complete" for event in events)


def test_invalid_understanding_falls_back_to_retrieval(monkeypatch) -> None:
    _events, calls = _run_stream(monkeypatch, "not json")

    assert calls["chat"] == 1
    assert calls["retrieve"] == [(1, "原问题")]


def test_rewrite_disabled_skips_query_understanding(monkeypatch) -> None:
    _events, calls = _run_stream(
        monkeypatch,
        '{"rewrite_query":"unused","intent":"summarize"}',
        rewrite_enabled=False,
    )

    assert calls["chat"] == 0
    assert calls["retrieve"] == [(1, "原问题")]

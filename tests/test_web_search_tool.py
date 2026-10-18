from __future__ import annotations

from app.services import agent_tools, web_search as web_search_service


def test_web_search_schema_uses_configured_maximum() -> None:
    tool = agent_tools.builtin_web_search_tool(7)
    limit = tool["function"]["parameters"]["properties"]["limit"]

    assert limit["maximum"] == 7
    assert limit["default"] == 7
    assert "1 to 7" in limit["description"]
    assert tool["_max_results"] == 7

    assert agent_tools.builtin_web_search_tool(0)["_max_results"] == 5
    assert agent_tools.builtin_web_search_tool(99)["_max_results"] == 20


def _execute(monkeypatch, *, limit, maximum=3):
    seen: list[int] = []
    monkeypatch.setattr(
        web_search_service,
        "default_provider",
        lambda _tenant_id: {"provider": "fake", "parameters": {}},
    )

    def fake_search(_provider, _parameters, query, count):
        seen.append(count)
        return [{"title": query, "url": "https://example.test"}][:count]

    monkeypatch.setattr(web_search_service, "search", fake_search)
    tool = agent_tools.builtin_web_search_tool(maximum)
    arguments = {"query": "release"}
    if limit is not None:
        arguments["limit"] = limit
    result = agent_tools.execute_tool(tool, arguments, tenant_id=1, user_id="u")
    return result, seen


def test_web_search_defaults_to_configured_maximum(monkeypatch) -> None:
    result, seen = _execute(monkeypatch, limit=None)

    assert result["success"] is True
    assert seen == [3]


def test_web_search_clamps_only_above_maximum(monkeypatch) -> None:
    result, seen = _execute(monkeypatch, limit=9)

    assert result["success"] is True
    assert seen == [3]
    assert "clamped to 3" in result["output"]

    rejected, rejected_seen = _execute(monkeypatch, limit=0)
    assert rejected["success"] is False
    assert "limit must be between 1 and 3" in rejected["output"]
    assert rejected_seen == []

    malformed, malformed_seen = _execute(monkeypatch, limit="many")
    assert malformed["success"] is False
    assert "limit must be between 1 and 3" in malformed["output"]
    assert malformed_seen == []

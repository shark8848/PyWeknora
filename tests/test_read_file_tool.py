from __future__ import annotations

import base64

from app.services import agent_tools, session_sandbox
from app.services import remote_sandbox


def _fenced_body(output: str) -> str:
    return output.split("```\n", 1)[1].split("```\n", 1)[0]


def test_read_file_schema_covers_workspace_and_paging() -> None:
    tool = agent_tools.builtin_read_file_tool(["release-check"], workspace=True)
    function = tool["function"]
    properties = function["parameters"]["properties"]

    assert "relative paths resolve from /workspace" in function["description"]
    assert "skill://<name>/SKILL.md" in function["description"]
    assert {"path", "line_offset", "offset", "limit", "max_bytes"} <= set(properties)
    assert properties["max_bytes"]["maximum"] == 65536


def test_list_sandbox_files_schema_defaults_to_output_directory() -> None:
    tool = agent_tools.builtin_list_sandbox_files_tool()
    function = tool["function"]
    properties = function["parameters"]["properties"]

    assert "omitted path lists /workspace/output" in function["description"]
    assert properties["max_entries"]["maximum"] == 500
    assert function["parameters"].get("required") is None


def test_paginate_file_preserves_lines_and_never_splits_utf8() -> None:
    content = "".join("这是一行中文文本，用来测试分页边界\n" for _ in range(100))
    offset = 1
    restored = ""
    for _ in range(100):
        page = agent_tools._paginate_file_text(content, offset, 0, 55, 1000)
        assert page["text"].encode("utf-8").decode("utf-8") == page["text"]
        assert page["text"].endswith("\n")
        restored += page["text"]
        if not page["next_offset"]:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert restored == content

    rune_page = agent_tools._paginate_file_text(
        ("a" * 100 + "\n") * 50, 1, 0, 1 << 20, 250
    )
    assert len(rune_page["text"]) <= 250
    assert rune_page["next_offset"]

    defaulted = agent_tools._paginate_file_text("a\nb\nc\n", 1, -5, -1, 1000)
    assert defaulted["text"] == "a\nb\nc\n"
    assert defaulted["end_line"] == 3


def test_render_file_page_rejects_path_that_consumes_output_budget() -> None:
    path = "/workspace/" + "x" * agent_tools.MAX_READ_FILE_OUTPUT_RUNES
    result = agent_tools._render_file_page(b"hello\n", path, "/workspace", "session-1")

    assert result["success"] is False
    assert "output budget" in result["output"]
    assert "next_offset" not in result["raw"]


def test_execute_read_file_uses_sandbox_relative_and_absolute_paths(monkeypatch) -> None:
    session = {"id": "session-1", "tenant_id": 1}
    seen: list[str] = []
    monkeypatch.setattr(agent_tools.db, "fetch_one", lambda *_args, **_kwargs: session)

    def stat(_tenant_id: int, _session, path: str):
        seen.append(path)
        return {"kind": "file", "size": 6}

    monkeypatch.setattr(session_sandbox, "stat_file", stat)
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: b"hello\n"
    )
    tool = {
        **agent_tools.builtin_read_file_tool([], workspace=True),
        "_session_id": "session-1",
    }

    relative = agent_tools.execute_tool(
        tool, {"path": "notes.txt"}, tenant_id=1, user_id="user-1"
    )
    absolute = agent_tools.execute_tool(
        tool, {"path": "/tmp/report.txt"}, tenant_id=1, user_id="user-1"
    )

    assert relative["success"] is True
    assert relative["raw"]["path"] == "/workspace/notes.txt"
    assert relative["raw"]["root"] == "/workspace"
    assert absolute["raw"]["path"] == "/tmp/report.txt"
    assert absolute["raw"]["root"] == "/tmp"
    assert seen == ["/workspace/notes.txt", "/tmp/report.txt"]


def test_execute_read_file_rejects_directory_and_suppresses_binary(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.db,
        "fetch_one",
        lambda *_args, **_kwargs: {"id": "session-1", "tenant_id": 1},
    )
    calls: list[str] = []

    def stat(_tenant_id: int, _session, path: str):
        return {"kind": "directory" if path.endswith("folder") else "file", "size": 4}

    def read(_tenant_id: int, _session, path: str):
        calls.append(path)
        return b"\x00\x01\x02\x03"

    monkeypatch.setattr(session_sandbox, "stat_file", stat)
    monkeypatch.setattr(session_sandbox, "read_file", read)
    tool = {
        **agent_tools.builtin_read_file_tool([], workspace=True),
        "_session_id": "session-1",
    }

    directory = agent_tools.execute_tool(
        tool, {"path": "folder"}, tenant_id=1, user_id="user-1"
    )
    binary = agent_tools.execute_tool(
        tool, {"path": "/tmp/image.bin"}, tenant_id=1, user_id="user-1"
    )

    assert directory["success"] is False
    assert "directory, not a file" in directory["output"]
    assert calls == ["/tmp/image.bin"]
    assert binary["success"] is True
    assert binary["raw"]["binary"] is True
    assert binary["raw"]["returned_bytes"] == 0
    assert "content suppressed" in binary["output"]


def test_execute_read_file_refuses_oversize_before_read(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.db,
        "fetch_one",
        lambda *_args, **_kwargs: {"id": "session-1", "tenant_id": 1},
    )
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {
            "kind": "file",
            "size": agent_tools.MAX_READ_FILE_DOWNLOAD_BYTES + 1,
        },
    )

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("oversize files must not be downloaded")

    monkeypatch.setattr(session_sandbox, "read_file", unexpected_read)
    tool = {
        **agent_tools.builtin_read_file_tool([], workspace=True),
        "_session_id": "session-1",
    }
    result = agent_tools.execute_tool(
        tool,
        {"path": "huge.log", "max_bytes": 999999},
        tenant_id=1,
        user_id="user-1",
    )

    assert result["success"] is False
    assert result["raw"]["read_refused"] is True
    assert result["raw"]["size"] == agent_tools.MAX_READ_FILE_DOWNLOAD_BYTES + 1
    assert "shell_exec" in result["output"]


def test_execute_read_file_pages_reconstruct_exactly(monkeypatch) -> None:
    content = "".join(f"line {index:03d} " + "x" * 80 + "\n" for index in range(100))
    monkeypatch.setattr(
        agent_tools.db,
        "fetch_one",
        lambda *_args, **_kwargs: {"id": "session-1", "tenant_id": 1},
    )
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": len(content.encode())},
    )
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: content.encode()
    )
    tool = {
        **agent_tools.builtin_read_file_tool([], workspace=True),
        "_session_id": "session-1",
    }
    offset = 1
    restored = ""
    for _ in range(100):
        result = agent_tools.execute_tool(
            tool,
            {"path": "report.txt", "offset": offset, "max_bytes": 2048},
            tenant_id=1,
            user_id="user-1",
        )
        assert result["success"] is True
        assert len(result["output"]) <= agent_tools.MAX_READ_FILE_OUTPUT_RUNES
        assert result["raw"]["returned_bytes"] == len(_fenced_body(result["output"]).encode())
        restored += _fenced_body(result["output"])
        if not result["raw"]["truncated"]:
            break
        assert result["raw"]["next_offset"] > offset
        offset = result["raw"]["next_offset"]
    assert restored == content


def test_execute_read_file_never_opens_host_paths(monkeypatch) -> None:
    path = "/etc/passwd"
    monkeypatch.setattr(
        agent_tools.db,
        "fetch_one",
        lambda *_args, **_kwargs: {"id": "session-1", "tenant_id": 1},
    )
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": 15},
    )
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: b"sandbox content\n"
    )
    tool = {
        **agent_tools.builtin_read_file_tool([], workspace=True),
        "_session_id": "session-1",
    }
    result = agent_tools.execute_tool(
        tool, {"path": path}, tenant_id=1, user_id="user-1"
    )

    assert result["raw"]["path"] == path
    assert "sandbox content" in result["output"]
    assert "root:x:" not in result["output"]


def test_execute_read_file_keeps_skill_resource_access(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        agent_tools,
        "read_skill_file",
        lambda _tenant_id, name, path: calls.append((name, path)) or "skill instructions\n",
    )

    def unexpected_stat(*_args, **_kwargs):
        raise AssertionError("skill resources must not touch sandbox storage")

    monkeypatch.setattr(session_sandbox, "stat_file", unexpected_stat)
    tool = agent_tools.builtin_read_file_tool(["release-check"])
    result = agent_tools.execute_tool(
        tool,
        {"path": "skill://release-check/SKILL.md"},
        tenant_id=1,
        user_id="user-1",
    )

    assert result["success"] is True
    assert result["raw"]["skill_name"] == "release-check"
    assert "skill instructions" in result["output"]
    assert calls == [("release-check", "SKILL.md")]


def test_session_sandbox_stat_and_read_file_commands(monkeypatch) -> None:
    commands: list[str] = []

    def fake_exec(_tenant_id, _session, command, **_kwargs):
        commands.append(command)
        if command.startswith("LC_ALL=C stat"):
            return remote_sandbox.RemoteExecResult("regular file|12\n", "", 0, 1)
        return remote_sandbox.RemoteExecResult(
            base64.encodebytes(b"hello world\n").decode("ascii"), "", 0, 1
        )

    monkeypatch.setattr(session_sandbox, "exec_command", fake_exec)
    stat = session_sandbox.stat_file(1, {"id": "session-1"}, "/tmp/report.txt")
    data = session_sandbox.read_file(1, {"id": "session-1"}, "/tmp/report.txt")

    assert stat == {
        "kind": "file",
        "size": 12,
        "type": "regular file",
    }
    assert data == b"hello world\n"
    assert "stat -c" in commands[0]
    assert commands[1] == "base64 < /tmp/report.txt"


def test_execute_list_sandbox_files_defaults_sorts_and_truncates(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        agent_tools.db,
        "fetch_one",
        lambda *_args, **_kwargs: {"id": "session-1", "tenant_id": 1},
    )

    def list_files(_tenant_id: int, _session, path: str):
        seen.append(path)
        return [
            {"name": "b.txt", "path": path + "/b.txt", "size": 2, "modified_at": "2026-01-01T00:00:00Z"},
            {"name": "a.txt", "path": path + "/a.txt", "size": 1, "modified_at": "2026-01-01T00:00:00Z"},
            {"name": "c.txt", "path": path + "/c.txt", "size": 3, "modified_at": "2026-01-01T00:00:00Z"},
        ]

    monkeypatch.setattr(session_sandbox, "list_files", list_files)
    tool = {
        **agent_tools.builtin_list_sandbox_files_tool(),
        "_session_id": "session-1",
    }
    result = agent_tools.execute_tool(
        tool, {"max_entries": 2}, tenant_id=1, user_id="user-1"
    )

    assert result["success"] is True
    assert result["raw"]["path"] == "/workspace/output"
    assert result["raw"]["root"] == "/workspace"
    assert result["raw"]["truncated"] is True
    assert [entry["name"] for entry in result["raw"]["entries"]] == ["b.txt", "a.txt"]
    assert "increase max_entries" in result["output"]
    assert seen == ["/workspace/output"]

    relative = agent_tools.execute_tool(
        tool, {"path": "reports", "max_entries": 0}, tenant_id=1, user_id="user-1"
    )
    assert relative["raw"]["path"] == "/workspace/reports"
    assert seen[-1] == "/workspace/reports"


def test_session_sandbox_list_files_parses_nul_delimited_find(monkeypatch) -> None:
    commands: list[str] = []
    listing = (
        "/workspace/output/b.txt\x002\x001700000000.0\x00"
        "/workspace/output/a.txt\x001\x001700000001.0\x00"
    )

    def fake_exec(_tenant_id, _session, command, **_kwargs):
        commands.append(command)
        return remote_sandbox.RemoteExecResult(listing, "", 0, 1)

    monkeypatch.setattr(session_sandbox, "exec_command", fake_exec)
    entries = session_sandbox.list_files(
        1, {"id": "session-1"}, "/workspace/output"
    )

    assert [entry["path"] for entry in entries] == [
        "/workspace/output/a.txt",
        "/workspace/output/b.txt",
    ]
    assert entries[0]["modified_at"] == "2023-11-14T22:13:21Z"
    assert "find \"$target\" -type f -printf" in commands[0]

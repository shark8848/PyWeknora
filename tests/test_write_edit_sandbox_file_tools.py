from __future__ import annotations

import base64
import re

import pytest

from app.services import agent_tools, session_sandbox
from app.services import remote_sandbox


def _session_tool(tool: dict) -> dict:
    return {**tool, "_session_id": "session-1"}


def _use_session(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.db,
        "fetch_one",
        lambda *_args, **_kwargs: {"id": "session-1", "tenant_id": 1},
    )


def test_write_and_edit_schemas_match_upstream_shape() -> None:
    write = agent_tools.builtin_write_sandbox_file_tool()["function"]
    edit = agent_tools.builtin_edit_sandbox_file_tool()["function"]

    assert write["name"] == "write_sandbox_file"
    assert write["parameters"]["required"] == ["path", "content"]
    assert write["parameters"]["properties"]["mode"]["enum"] == [
        "overwrite",
        "append",
    ]
    assert "262144 bytes per call" in write["description"]
    assert "excluding /workspace/input" in write["description"]

    assert edit["name"] == "edit_sandbox_file"
    assert edit["parameters"]["required"] == ["path", "edits"]
    items = edit["parameters"]["properties"]["edits"]["items"]
    assert items["required"] == ["old_string", "new_string"]
    assert items["properties"]["replace_all"]["type"] == "boolean"


def test_execute_write_sandbox_file_overwrites_relative_path(monkeypatch) -> None:
    _use_session(monkeypatch)
    writes: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda _tenant_id, _session, path, data: writes.append((path, data)),
    )
    tool = _session_tool(agent_tools.builtin_write_sandbox_file_tool())

    result = agent_tools.execute_tool(
        tool,
        {"path": "drafts/report.txt", "content": "hello\n"},
        tenant_id=1,
        user_id="user-1",
    )

    assert result["success"] is True
    assert writes == [("/workspace/drafts/report.txt", b"hello\n")]
    assert result["raw"] == {
        "display_type": "write_sandbox_file",
        "session_id": "session-1",
        "path": "/workspace/drafts/report.txt",
        "root": "/workspace",
        "name": "report.txt",
        "size": 6,
        "mode": "overwrite",
        "appended": 6,
    }
    assert "=== Wrote sandbox file: /workspace/drafts/report.txt ===" in result["output"]
    assert "bytes=6" in result["output"]
    assert "hello" not in result["output"]


def test_write_sandbox_file_uses_output_root_and_rejects_scope(monkeypatch) -> None:
    _use_session(monkeypatch)
    writes: list[str] = []
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda _tenant_id, _session, path, _data: writes.append(path),
    )
    tool = _session_tool(agent_tools.builtin_write_sandbox_file_tool())

    output = agent_tools.execute_tool(
        tool,
        {"path": "/workspace/output/report.txt", "content": "ok"},
        tenant_id=1,
        user_id="user-1",
    )
    assert output["success"] is True
    assert output["raw"]["root"] == "/workspace/output"

    for path in (
        "/",
        "/workspace",
        "/workspace/output",
        "/workspace/input",
        "/workspace/input/secret.txt",
        "/tmp/task/check.txt",
        "../tmp/task/check.txt",
    ):
        result = agent_tools.execute_tool(
            tool, {"path": path, "content": "nope"}, tenant_id=1, user_id="user-1"
        )
        assert result["success"] is False, path
        assert "outside that scope" in result["output"]
    assert writes == ["/workspace/output/report.txt"]


def test_write_sandbox_file_rejects_binary_oversize_and_bad_mode(monkeypatch) -> None:
    _use_session(monkeypatch)
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda *_args, **_kwargs: pytest.fail("invalid writes must be refused"),
    )
    tool = _session_tool(agent_tools.builtin_write_sandbox_file_tool())

    binary = agent_tools.execute_tool(
        tool, {"path": "x.txt", "content": "pre\x00post"}, tenant_id=1, user_id="u"
    )
    oversize = agent_tools.execute_tool(
        tool,
        {"path": "big.txt", "content": "a" * (agent_tools.MAX_SANDBOX_FILE_BYTES + 1)},
        tenant_id=1,
        user_id="u",
    )
    bad_mode = agent_tools.execute_tool(
        tool, {"path": "x.txt", "content": "ok", "mode": "replace"}, tenant_id=1, user_id="u"
    )

    assert binary["success"] is False and "binary" in binary["output"]
    assert oversize["success"] is False and "file limit" in oversize["output"]
    assert bad_mode["success"] is False and "unknown mode" in bad_mode["output"]


def test_write_sandbox_file_append_requires_existing_regular_file(monkeypatch) -> None:
    _use_session(monkeypatch)
    writes: list[bytes] = []
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda _tenant_id, _session, _path, data: writes.append(data),
    )
    tool = _session_tool(agent_tools.builtin_write_sandbox_file_tool())

    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "missing", "size": 0},
    )
    missing = agent_tools.execute_tool(
        tool,
        {"path": "notes.txt", "content": "next", "mode": "append"},
        tenant_id=1,
        user_id="u",
    )
    assert missing["success"] is False
    assert "does not exist yet" in missing["output"]

    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "directory", "size": 0},
    )
    directory = agent_tools.execute_tool(
        tool,
        {"path": "notes.txt", "content": "next", "mode": "append"},
        tenant_id=1,
        user_id="u",
    )
    assert directory["success"] is False
    assert "not a regular file" in directory["output"]
    assert writes == []


def test_write_sandbox_file_append_reports_running_size(monkeypatch) -> None:
    _use_session(monkeypatch)
    writes: list[bytes] = []
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda _tenant_id, _session, _path, data: writes.append(data),
    )
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": 6},
    )
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: b"hello\n"
    )
    tool = _session_tool(agent_tools.builtin_write_sandbox_file_tool())

    result = agent_tools.execute_tool(
        tool,
        {"path": "notes.txt", "content": "world\n", "mode": "append"},
        tenant_id=1,
        user_id="u",
    )

    assert result["success"] is True
    assert writes == [b"hello\nworld\n"]
    assert result["raw"]["mode"] == "append"
    assert result["raw"]["appended"] == 6
    assert result["raw"]["size"] == 12
    assert "appended=6, total_bytes=12" in result["output"]


def test_write_sandbox_file_append_respects_total_file_cap(monkeypatch) -> None:
    _use_session(monkeypatch)
    monkeypatch.setattr(agent_tools, "MAX_SANDBOX_FILE_BYTES", 10)
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": 8},
    )
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: b"12345678"
    )
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda *_args, **_kwargs: pytest.fail("oversize append must not write"),
    )
    tool = _session_tool(agent_tools.builtin_write_sandbox_file_tool())

    result = agent_tools.execute_tool(
        tool,
        {"path": "notes.txt", "content": "abc", "mode": "append"},
        tenant_id=1,
        user_id="u",
    )

    assert result["success"] is False
    assert "past the 10-byte file limit" in result["output"]


def test_apply_sandbox_edits_unique_replace_all_and_errors() -> None:
    content = "a = '/home/user/Desktop/deck.pptx'\nprint(a)\n"
    updated, count = agent_tools._apply_sandbox_edits(
        content,
        [
            {
                "old_string": "/home/user/Desktop/deck.pptx",
                "new_string": "/workspace/output/deck.pptx",
            }
        ],
    )
    assert count == 1
    assert updated == "a = '/workspace/output/deck.pptx'\nprint(a)\n"

    updated, count = agent_tools._apply_sandbox_edits(
        "xx xx xx", [{"old_string": "xx", "new_string": "yy", "replace_all": True}]
    )
    assert (updated, count) == ("yy yy yy", 3)

    with pytest.raises(ValueError, match="3 times"):
        agent_tools._apply_sandbox_edits(
            "xx xx xx", [{"old_string": "xx", "new_string": "yy"}]
        )
    with pytest.raises(ValueError, match="not found"):
        agent_tools._apply_sandbox_edits(
            content, [{"old_string": "missing", "new_string": "x"}]
        )
    with pytest.raises(ValueError, match="identical"):
        agent_tools._apply_sandbox_edits(
            content, [{"old_string": "print(a)", "new_string": "print(a)"}]
        )
    with pytest.raises(ValueError, match="old_string is required"):
        agent_tools._apply_sandbox_edits(
            content, [{"old_string": "", "new_string": "x"}]
        )


def test_apply_sandbox_edits_resolves_original_and_rejects_overlap() -> None:
    updated, count = agent_tools._apply_sandbox_edits(
        "alpha\nbeta\ngamma\n",
        [
            {"old_string": "gamma", "new_string": "GAMMA"},
            {"old_string": "alpha", "new_string": "ALPHA"},
        ],
    )
    assert (updated, count) == ("ALPHA\nbeta\nGAMMA\n", 2)

    with pytest.raises(ValueError, match="overlapping"):
        agent_tools._apply_sandbox_edits(
            "the quick brown fox\n",
            [
                {"old_string": "quick brown", "new_string": "slow"},
                {"old_string": "brown fox", "new_string": "grey wolf"},
            ],
        )

    with pytest.raises(ValueError, match="edits\\[1\\].*not found"):
        agent_tools._apply_sandbox_edits(
            "alpha\nbeta\n",
            [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "nowhere", "new_string": "x"},
            ],
        )


def test_normalize_sandbox_edits_accepts_model_shapes() -> None:
    expected = [{"old_string": "a", "new_string": "b", "replace_all": False}]
    assert agent_tools._normalize_sandbox_edits(
        [{"old_string": "a", "new_string": "b"}]
    ) == (expected, "")
    assert agent_tools._normalize_sandbox_edits(
        {"old_string": "a", "new_string": "b"}
    ) == (expected, "")
    assert agent_tools._normalize_sandbox_edits(
        '[{"old_string":"a","new_string":"b"}]'
    ) == (expected, "")
    assert agent_tools._normalize_sandbox_edits("not json")[0] is None


def test_execute_edit_sandbox_file_writes_once_without_echo(monkeypatch) -> None:
    _use_session(monkeypatch)
    writes: list[bytes] = []
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": 20},
    )
    monkeypatch.setattr(
        session_sandbox,
        "read_file",
        lambda *_args, **_kwargs: b"alpha\nbeta\ngamma\n",
    )
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda _tenant_id, _session, _path, data: writes.append(data),
    )
    tool = _session_tool(agent_tools.builtin_edit_sandbox_file_tool())

    result = agent_tools.execute_tool(
        tool,
        {
            "path": "notes.txt",
            "edits": [
                {"old_string": "gamma", "new_string": "GAMMA"},
                {"old_string": "alpha", "new_string": "ALPHA"},
            ],
        },
        tenant_id=1,
        user_id="u",
    )

    assert result["success"] is True
    assert writes == [b"ALPHA\nbeta\nGAMMA\n"]
    assert result["raw"]["replacements"] == 2
    assert result["raw"]["size"] == 17
    assert result["raw"]["root"] == "/workspace"
    assert "=== Edited sandbox file: /workspace/notes.txt ===" in result["output"]
    assert "replacements=2" in result["output"]
    assert "GAMMA" not in result["output"]


def test_execute_edit_sandbox_file_validates_before_write(monkeypatch) -> None:
    _use_session(monkeypatch)
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": 11},
    )
    monkeypatch.setattr(
        session_sandbox,
        "read_file",
        lambda *_args, **_kwargs: b"alpha\nbeta\n",
    )
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda *_args, **_kwargs: pytest.fail("failed edits must not write"),
    )
    tool = _session_tool(agent_tools.builtin_edit_sandbox_file_tool())

    result = agent_tools.execute_tool(
        tool,
        {
            "path": "notes.txt",
            "edits": [
                {"old_string": "alpha", "new_string": "ALPHA"},
                {"old_string": "nowhere", "new_string": "x"},
            ],
        },
        tenant_id=1,
        user_id="u",
    )

    assert result["success"] is False
    assert "edits[1]" in result["output"]
    assert "not found" in result["output"]


def test_execute_edit_sandbox_file_rejects_missing_directory_and_binary(monkeypatch) -> None:
    _use_session(monkeypatch)
    tool = _session_tool(agent_tools.builtin_edit_sandbox_file_tool())
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda *_args, **_kwargs: pytest.fail("invalid edits must not write"),
    )

    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "missing", "size": 0},
    )
    missing = agent_tools.execute_tool(
        tool,
        {"path": "missing.txt", "edits": [{"old_string": "a", "new_string": "b"}]},
        tenant_id=1,
        user_id="u",
    )
    assert missing["success"] is False and "file not found" in missing["output"]

    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "directory", "size": 0},
    )
    directory = agent_tools.execute_tool(
        tool,
        {"path": "folder", "edits": [{"old_string": "a", "new_string": "b"}]},
        tenant_id=1,
        user_id="u",
    )
    assert directory["success"] is False and "is a directory" in directory["output"]

    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": 4},
    )
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: b"\x00\x01\x02\x03"
    )
    binary = agent_tools.execute_tool(
        tool,
        {"path": "image.bin", "edits": [{"old_string": "a", "new_string": "b"}]},
        tenant_id=1,
        user_id="u",
    )
    assert binary["success"] is False and "binary files cannot be edited" in binary["output"]


def test_execute_edit_sandbox_file_enforces_size_limits(monkeypatch) -> None:
    _use_session(monkeypatch)
    tool = _session_tool(agent_tools.builtin_edit_sandbox_file_tool())
    monkeypatch.setattr(
        session_sandbox,
        "write_file",
        lambda *_args, **_kwargs: pytest.fail("oversize edits must not write"),
    )
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {
            "kind": "file",
            "size": agent_tools.MAX_WRITE_SANDBOX_BYTES + 1,
        },
    )

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("oversize files must not be downloaded")

    monkeypatch.setattr(session_sandbox, "read_file", unexpected_read)
    too_large = agent_tools.execute_tool(
        tool,
        {"path": "big.txt", "edits": [{"old_string": "a", "new_string": "b"}]},
        tenant_id=1,
        user_id="u",
    )
    assert too_large["success"] is False
    assert "file too large to edit" in too_large["output"]

    monkeypatch.setattr(agent_tools, "MAX_WRITE_SANDBOX_BYTES", 20)
    monkeypatch.setattr(
        session_sandbox,
        "stat_file",
        lambda *_args, **_kwargs: {"kind": "file", "size": 10},
    )
    monkeypatch.setattr(
        session_sandbox, "read_file", lambda *_args, **_kwargs: b"alpha\nbeta\n"
    )
    result_too_large = agent_tools.execute_tool(
        tool,
        {
            "path": "notes.txt",
            "edits": [{"old_string": "alpha", "new_string": "A" * 30}],
        },
        tenant_id=1,
        user_id="u",
    )
    assert result_too_large["success"] is False
    assert "result too large" in result_too_large["output"]


def test_session_sandbox_write_file_uploads_base64_chunks(monkeypatch) -> None:
    data = b"x" * (100 * 1024) + "中文\n".encode()
    commands: list[str] = []

    def fake_exec(_tenant_id, _session, command, **_kwargs):
        commands.append(command)
        return remote_sandbox.RemoteExecResult("", "", 0, 1)

    monkeypatch.setattr(session_sandbox, "exec_command", fake_exec)
    session_sandbox.write_file(1, {"id": "session-1"}, "/workspace/output/big.bin", data)

    encoded = "".join(
        re.findall(r"printf %s ([A-Za-z0-9+/=]+)", "\n".join(commands))
    )
    assert base64.b64decode(encoded) == data
    assert "mkdir -p -- /workspace/output" in commands[0]
    assert ".weknora-write-" in commands[0]
    assert "mv -f -- " in commands[-1]
    assert "/workspace/output/.weknora-write-" in commands[-1]
    assert len(commands) > 1


def test_session_sandbox_write_file_raises_and_cleans_temp(monkeypatch) -> None:
    commands: list[str] = []
    calls = 0

    def fake_exec(_tenant_id, _session, command, **_kwargs):
        nonlocal calls
        commands.append(command)
        calls += 1
        if calls == 1:
            return remote_sandbox.RemoteExecResult("", "disk full", 1, 1)
        return remote_sandbox.RemoteExecResult("", "", 0, 1)

    monkeypatch.setattr(session_sandbox, "exec_command", fake_exec)

    with pytest.raises(session_sandbox.SessionSandboxError) as excinfo:
        session_sandbox.write_file(
            1, {"id": "session-1"}, "/workspace/output/file.txt", b"hello"
        )

    assert excinfo.value.code == "sandbox_file_write_failed"
    assert "disk full" in str(excinfo.value)
    assert commands[-1].startswith("rm -f -- /workspace/output/.weknora-write-")

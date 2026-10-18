"""Tool catalog and execution for agent chat."""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
import re
import struct
import time
from typing import Any

from .. import db
from ..utils import dumps, loads
from . import browser_skill
from . import mcp as mcp_client
from . import session_sandbox
from . import web_search as web_search_service
from . import wiki_agent_tools
from .retrieval import retrieve
from .skills import read_skill_file, skill_display_name


MAX_TOOL_NAME = 64
MAX_BROWSER_OUTPUT_RUNES = 24000
MAX_BROWSER_SCREENSHOT_BYTES = 8 * 1024 * 1024
MAX_BROWSER_SCREENSHOT_PIXELS = 40_000_000
MAX_SHELL_COMMAND_BYTES = 65536
MAX_SHELL_OUTPUT_BYTES = 65536
MAX_SHELL_STDERR_BYTES = 16384
MAX_READ_FILE_OUTPUT_RUNES = 24000
MAX_READ_FILE_DOWNLOAD_BYTES = 8 * 1024 * 1024
MAX_WRITE_SANDBOX_BYTES = 256 * 1024
MAX_SANDBOX_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_READ_FILE_MAX_BYTES = 64 * 1024
DEFAULT_READ_FILE_MAX_LINES = 2000
READ_FILE_PAGE_OVERHEAD = 512
DEFAULT_LIST_SANDBOX_MAX_ENTRIES = 200
MAX_LIST_SANDBOX_MAX_ENTRIES = 500


class ToolApprovalRequired(RuntimeError):
    def __init__(self, name: str, service_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__(f"Tool {name} requires human approval")
        self.name = name
        self.service_id = service_id
        self.tool_name = tool_name
        self.arguments = arguments


class MCPOAuthRequired(RuntimeError):
    def __init__(self, name: str, service_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        super().__init__(f"Tool {name} requires MCP OAuth authorization")
        self.name = name
        self.service_id = service_id
        self.tool_name = tool_name
        self.arguments = arguments


def sanitize_name(name: str) -> str:
    value = str(name or "").lower().replace(" ", "_").replace("-", "_")
    return re.sub(r"[^a-z0-9_]", "", value)


def qualified_name(service_name: str, tool_name: str) -> str:
    service = sanitize_name(service_name)
    tool = sanitize_name(tool_name)
    name = f"mcp_{service}_{tool}"
    if len(name) <= MAX_TOOL_NAME:
        return name
    max_service = max(4, MAX_TOOL_NAME - 5 - len(tool))
    return (f"mcp_{service[:max_service]}_{tool}")[:MAX_TOOL_NAME]


def _truncate_browser_output(text: str) -> str:
    if len(text) <= MAX_BROWSER_OUTPUT_RUNES:
        return text
    reserve = min(200, MAX_BROWSER_OUTPUT_RUNES // 10)
    usable = MAX_BROWSER_OUTPUT_RUNES - reserve
    head_size = int(usable * 0.7)
    tail_size = usable - head_size
    marker = (
        f"\n\n... [output truncated: {len(text)} -> {MAX_BROWSER_OUTPUT_RUNES} chars, "
        f"showing first {head_size} + last {tail_size}] ...\n\n"
    )
    return text[:head_size] + marker + text[-tail_size:]


def _truncate_utf8(text: str, limit: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    omitted = len(raw) - limit
    for _ in range(2):
        marker = f"\n...[truncated {omitted} bytes]...\n"
        kept = limit - len(marker.encode("utf-8"))
        if kept <= 0:
            return raw[-limit:].decode("utf-8", errors="ignore"), True
        head = kept // 4
        tail = kept - head
        omitted = len(raw) - head - tail
    marker = f"\n...[truncated {omitted} bytes]...\n"
    head = max(0, (limit - len(marker.encode("utf-8"))) // 4)
    tail = max(0, limit - len(marker.encode("utf-8")) - head)
    return (
        raw[:head].decode("utf-8", errors="ignore")
        + marker
        + raw[-tail:].decode("utf-8", errors="ignore"),
        True,
    )


def _is_binary_content(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    if "\x00" in text:
        return True
    return any(ord(char) < 0x20 and char not in "\n\r\t\b\f\x1b" for char in text)


def _resolve_workspace_path(path: str) -> str:
    value = path.strip()
    if not value.startswith("/"):
        value = posixpath.join("/workspace", value)
    clean = posixpath.normpath(value)
    if clean.startswith("//"):
        clean = "/" + clean.lstrip("/")
    return clean


def _workspace_root(path: str) -> str:
    for root in ("/workspace", "/tmp"):
        if path == root or path.startswith(root + "/"):
            return root
    return "/"


def _writable_sandbox_root(path: str) -> str:
    """Return the writable sandbox root for a normalized path, or ""."""
    if path in {"/workspace", "/workspace/output"}:
        return ""
    if path == "/workspace/input" or path.startswith("/workspace/input/"):
        return ""
    if path.startswith("/workspace/output/"):
        return "/workspace/output"
    if path.startswith("/workspace/"):
        return "/workspace"
    return ""


def _sandbox_write_scope_error(requested: str) -> str:
    return (
        "this tool only writes files under /workspace (not under /workspace/input, "
        "and not the directory roots themselves). "
        f"path {requested!r} is outside that scope; use shell_exec for other locations"
    )


def _normalize_sandbox_edits(raw: Any) -> tuple[list[dict[str, Any]] | None, str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None, "edits must be an array of {old_string, new_string} objects"
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None, "edits must be an array of {old_string, new_string} objects"
    edits: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return None, "edits must be an array of {old_string, new_string} objects"
        old_string = item.get("old_string")
        new_string = item.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return None, "each edit requires string old_string and new_string fields"
        edits.append(
            {
                "old_string": old_string,
                "new_string": new_string,
                "replace_all": bool(item.get("replace_all")),
            }
        )
    return edits, ""


def _apply_sandbox_edits(content: str, edits: list[dict[str, Any]]) -> tuple[str, int]:
    """Apply all edits against the original content, rejecting overlaps."""
    if not edits:
        raise ValueError(
            "edits is required: an array of {old_string, new_string}, "
            "with one entry even for a single change"
        )
    multi = len(edits) > 1
    spans: list[tuple[int, int, int, str]] = []
    for index, edit in enumerate(edits):
        old_string = str(edit.get("old_string") or "")
        new_string = str(edit.get("new_string") or "")
        if not old_string:
            if multi:
                raise ValueError(
                    f"edits[{index}].old_string is required; copy the exact text "
                    "to change, including whitespace"
                )
            raise ValueError(
                "old_string is required; copy the exact text to change, including whitespace"
            )
        if old_string == new_string:
            if multi:
                raise ValueError(
                    f"edits[{index}].old_string and new_string are identical; "
                    "no change would be made"
                )
            raise ValueError(
                "old_string and new_string are identical; no change would be made"
            )
        found: list[int] = []
        offset = 0
        while True:
            position = content.find(old_string, offset)
            if position < 0:
                break
            found.append(position)
            offset = position + len(old_string)
        if not found:
            if multi:
                raise ValueError(
                    f"edits[{index}].old_string was not found in the file. "
                    "Copy the exact text (including whitespace) from the file"
                )
            raise ValueError(
                "old_string was not found in the file. "
                "Copy the exact text (including whitespace) from the file"
            )
        if len(found) > 1 and not bool(edit.get("replace_all")):
            if multi:
                raise ValueError(
                    f"edits[{index}].old_string matched {len(found)} times. "
                    "Include more surrounding context so it is unique, or set "
                    "replace_all on that entry"
                )
            raise ValueError(
                f"old_string matched {len(found)} times. Include more surrounding "
                "context so it is unique, or set replace_all"
            )
        for start in found:
            spans.append((start, start + len(old_string), index, new_string))

    spans.sort(key=lambda span: span[0])
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            raise ValueError(
                f"edits[{previous[2]}] and edits[{current[2]}] cover overlapping "
                "text. Merge them into one edit that spans both changes"
            )

    parts: list[str] = []
    cursor = 0
    for start, end, _index, new_string in spans:
        parts.append(content[cursor:start])
        parts.append(new_string)
        cursor = end
    parts.append(content[cursor:])
    return "".join(parts), len(spans)


def _paginate_file_text(
    content: str,
    offset: int,
    limit: int,
    max_bytes: int,
    max_runes: int,
) -> dict[str, Any]:
    start_offset = max(1, int(offset or 1))
    max_lines = int(limit or 0)
    if max_lines <= 0:
        max_lines = DEFAULT_READ_FILE_MAX_LINES
    byte_budget = int(max_bytes or 0)
    if byte_budget <= 0:
        byte_budget = DEFAULT_READ_FILE_MAX_BYTES
    rune_budget = int(max_runes or 0)
    if rune_budget <= 0:
        rune_budget = byte_budget

    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if start_offset > total:
        return {
            "text": "",
            "start_line": start_offset,
            "end_line": start_offset - 1,
            "total_lines": total,
            "next_offset": 0,
            "line_too_large": False,
            "line_bytes": 0,
        }

    start = start_offset - 1
    end = start
    used_bytes = 0
    used_runes = 0
    chunks: list[str] = []
    while end < total and end - start < max_lines:
        line = lines[end]
        cost_bytes = len(line.encode("utf-8")) + 1
        cost_runes = len(line) + 1
        if used_bytes + cost_bytes > byte_budget or used_runes + cost_runes > rune_budget:
            if end == start:
                return {
                    "text": "",
                    "start_line": start_offset,
                    "end_line": 0,
                    "total_lines": total,
                    "next_offset": 0,
                    "line_too_large": True,
                    "line_bytes": len(line.encode("utf-8")),
                }
            break
        chunks.append(line)
        chunks.append("\n")
        used_bytes += cost_bytes
        used_runes += cost_runes
        end += 1

    return {
        "text": "".join(chunks),
        "start_line": start_offset,
        "end_line": end,
        "total_lines": total,
        "next_offset": end + 1 if end < total else 0,
        "line_too_large": False,
        "line_bytes": 0,
    }


def _render_file_page(
    data: bytes,
    path: str,
    root: str,
    session_id: str,
    *,
    offset: int = 0,
    limit: int = 0,
    max_bytes: int = 0,
) -> dict[str, Any]:
    byte_budget = int(max_bytes or 0)
    if byte_budget <= 0:
        byte_budget = DEFAULT_READ_FILE_MAX_BYTES
    byte_budget = min(byte_budget, DEFAULT_READ_FILE_MAX_BYTES)
    total = len(data)
    binary = _is_binary_content(data)
    raw: dict[str, Any] = {
        "session_id": session_id,
        "path": path,
        "root": root,
        "size": total,
        "binary": binary,
    }
    header = f"=== File: {path} ===\n\n"
    if binary:
        if path.startswith("skill://"):
            note = (
                "binary skill resource — text suppressed. Use the skill's execution "
                "guidance to process bundled binary files; reading a resource does not "
                "create a downloadable artifact.\n"
            )
        else:
            note = "binary file — content suppressed; use the artifact attachment to download it.\n"
        raw["returned_bytes"] = 0
        raw["truncated"] = False
        return {
            "success": True,
            "output": header + f"size={total} bytes, returned=0 bytes\n" + note,
            "raw": raw,
        }

    max_runes = MAX_READ_FILE_OUTPUT_RUNES - READ_FILE_PAGE_OVERHEAD - len(path)
    if max_runes <= 0:
        return {
            "success": False,
            "output": "file path and metadata exceed this read's output budget; increase the tool output limit",
            "raw": raw,
        }
    page = _paginate_file_text(data.decode("utf-8"), offset, limit, byte_budget, max_runes)
    if page["line_too_large"] and path.startswith("skill://"):
        return {
            "success": False,
            "output": (
                f"Line {page['start_line']} exceeds this read's output budget. "
                f"Increase max_bytes up to 65536 if lower; otherwise use a smaller "
                "skill resource. A skill:// address is not a shell path."
            ),
            "raw": raw,
        }
    if page["line_too_large"]:
        raw.update(
            {
                "returned_bytes": 0,
                "truncated": True,
                "total_lines": page["total_lines"],
            }
        )
        return {
            "success": True,
            "output": (
                header
                + f"size={total} bytes, returned=0 bytes\n\n"
                + f"[Line {page['start_line']} is {page['line_bytes']} bytes, over the "
                + f"{byte_budget} byte budget for one call. Use shell_exec: "
                + f"sed -n '{page['start_line']}p' {path} | head -c {byte_budget}]\n"
            ),
            "raw": raw,
        }

    returned_bytes = len(page["text"].encode("utf-8"))
    output = (
        header
        + f"size={total} bytes, returned={returned_bytes} bytes\n\n```\n"
        + page["text"]
    )
    if page["text"] and not page["text"].endswith("\n"):
        output += "\n"
    output += "```\n"
    if page["next_offset"]:
        output += (
            f"\n[Showing lines {page['start_line']}-{page['end_line']} of "
            f"{page['total_lines']}. Use offset={page['next_offset']} to continue.]\n"
        )
    elif page["start_line"] > 1:
        output += (
            f"\n[Showing lines {page['start_line']}-{page['end_line']} of "
            f"{page['total_lines']} — end of file.]\n"
        )
    raw.update(
        {
            "returned_bytes": returned_bytes,
            "truncated": bool(page["next_offset"]),
            "total_lines": page["total_lines"],
            "start_line": page["start_line"],
            "end_line": page["end_line"],
        }
    )
    if page["next_offset"]:
        raw["next_offset"] = page["next_offset"]
    return {"success": True, "output": output, "raw": raw}


def _oversized_read_file(path: str, root: str, size: int, session_id: str) -> dict[str, Any]:
    output = (
        f"=== Sandbox file too large to read: {path} ===\n\n"
        f"size={size} bytes, limit={MAX_READ_FILE_DOWNLOAD_BYTES} bytes, returned=0 bytes\n\n"
        "The file was not downloaded. Use shell_exec with sed -n, head, tail, grep, or awk "
        "to inspect only the relevant text section. Binary files remain available through "
        "the artifact attachment.\n"
    )
    return {
        "success": False,
        "output": output,
        "raw": {
            "session_id": session_id,
            "path": path,
            "root": root,
            "size": size,
            "limit": MAX_READ_FILE_DOWNLOAD_BYTES,
            "returned_bytes": 0,
            "truncated": True,
            "read_refused": True,
        },
    }


def _read_workspace_file(
    tenant_id: int,
    session: dict[str, Any],
    session_id: str,
    requested_path: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if "\x00" in requested_path:
        return {
            "success": False,
            "output": "path is invalid",
            "raw": {"session_id": session_id},
        }
    clean = _resolve_workspace_path(requested_path)
    root = _workspace_root(clean)
    try:
        stat = session_sandbox.stat_file(tenant_id, session, clean)
    except session_sandbox.SessionSandboxError as exc:
        return {
            "success": False,
            "output": f"failed to inspect {clean} before reading: {exc}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    kind = str(stat.get("kind") or "")
    size = int(stat.get("size") or 0)
    if kind == "missing":
        return {
            "success": False,
            "output": f"file not found: {clean}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if kind == "directory":
        return {
            "success": False,
            "output": f"path is a directory, not a file: {clean}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if kind != "file":
        return {
            "success": False,
            "output": f"path is not a regular file: {clean}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if size > MAX_READ_FILE_DOWNLOAD_BYTES:
        return _oversized_read_file(clean, root, size, session_id)
    try:
        data = session_sandbox.read_file(tenant_id, session, clean)
    except session_sandbox.SessionSandboxError as exc:
        return {
            "success": False,
            "output": f"failed to read {clean}: {exc}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if len(data) > MAX_READ_FILE_DOWNLOAD_BYTES:
        return _oversized_read_file(clean, root, len(data), session_id)
    try:
        offset = int(arguments.get("offset") or 0)
        limit = int(arguments.get("limit") or 0)
        max_bytes = int(arguments.get("max_bytes") or 0)
    except (TypeError, ValueError):
        offset, limit, max_bytes = 0, 0, 0
    return _render_file_page(
        data,
        clean,
        root,
        session_id,
        offset=offset,
        limit=limit,
        max_bytes=max_bytes,
    )


def _list_workspace_files(
    tenant_id: int,
    session: dict[str, Any],
    session_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    requested_path = str(arguments.get("path") or "").strip()
    if "\x00" in requested_path:
        return {
            "success": False,
            "output": "path is invalid",
            "raw": {"session_id": session_id},
        }
    target = _resolve_workspace_path(requested_path) if requested_path else "/workspace/output"
    root = _workspace_root(target)
    try:
        max_entries = int(arguments.get("max_entries") or 0)
    except (TypeError, ValueError):
        max_entries = 0
    if max_entries <= 0:
        max_entries = DEFAULT_LIST_SANDBOX_MAX_ENTRIES
    max_entries = min(max_entries, MAX_LIST_SANDBOX_MAX_ENTRIES)
    try:
        entries = session_sandbox.list_files(tenant_id, session, target)
    except session_sandbox.SessionSandboxError as exc:
        return {
            "success": False,
            "output": f"failed to list {target}: {exc}",
            "raw": {"session_id": session_id, "path": target, "root": root},
        }
    truncated = len(entries) > max_entries
    visible = entries[:max_entries]
    output = f"=== Sandbox listing: {target} ===\n\n"
    if not visible:
        output += (
            "No files found under this path. Either nothing has been written here yet, "
            "or the sandbox has been reaped.\n"
        )
    else:
        output += f"Found {len(visible)} file(s)"
        if truncated:
            output += (
                f" (truncated to {max_entries}; increase max_entries to see more)"
            )
        output += ":\n\n"
        for entry in visible:
            output += (
                f"- {entry['path']} (size={entry['size']}, "
                f"modified={entry['modified_at']})\n"
            )
    return {
        "success": True,
        "output": output,
        "raw": {
            "session_id": session_id,
            "path": target,
            "root": root,
            "entries": visible,
            "count": len(visible),
            "truncated": truncated,
        },
    }


def _write_workspace_file(
    tenant_id: int,
    session: dict[str, Any],
    session_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    requested_path = str(arguments.get("path") or "").strip()
    if not requested_path:
        return {
            "success": False,
            "output": "path is required; write under /workspace",
            "raw": {"session_id": session_id},
        }
    if "\x00" in requested_path:
        return {
            "success": False,
            "output": "path is invalid",
            "raw": {"session_id": session_id},
        }
    clean = _resolve_workspace_path(requested_path)
    root = _writable_sandbox_root(clean)
    if not root:
        return {
            "success": False,
            "output": _sandbox_write_scope_error(requested_path),
            "raw": {"session_id": session_id, "path": clean},
        }

    content = arguments.get("content")
    if not isinstance(content, str):
        return {
            "success": False,
            "output": "content is required and must be a string",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    chunk = content.encode("utf-8")
    if len(chunk) > MAX_SANDBOX_FILE_BYTES:
        return {
            "success": False,
            "output": (
                f"content is {len(chunk)} bytes, past the {MAX_SANDBOX_FILE_BYTES}-byte "
                'file limit. Write the first part now and send the rest with mode="append"'
            ),
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if _is_binary_content(chunk):
        return {
            "success": False,
            "output": (
                "binary content is not accepted; write a text script and have it "
                "produce binary files under /workspace/output"
            ),
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }

    mode = str(arguments.get("mode") or "").strip().lower()
    if not mode:
        mode = "overwrite"
    if mode not in {"overwrite", "append"}:
        return {
            "success": False,
            "output": f'unknown mode {mode!r}; use "overwrite" (default) or "append"',
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }

    total = chunk
    if mode == "append":
        try:
            stat = session_sandbox.stat_file(tenant_id, session, clean)
        except session_sandbox.SessionSandboxError as exc:
            return {
                "success": False,
                "output": (
                    f"cannot append to {clean}: it does not exist yet ({exc}). "
                    'Write the first chunk with mode="overwrite", then append the rest'
                ),
                "raw": {"session_id": session_id, "path": clean, "root": root},
            }
        if str(stat.get("kind") or "") == "missing":
            return {
                "success": False,
                "output": (
                    f"cannot append to {clean}: it does not exist yet. "
                    'Write the first chunk with mode="overwrite", then append the rest'
                ),
                "raw": {"session_id": session_id, "path": clean, "root": root},
            }
        if str(stat.get("kind") or "") != "file":
            return {
                "success": False,
                "output": f"cannot append to {clean}: it is not a regular file",
                "raw": {"session_id": session_id, "path": clean, "root": root},
            }
        try:
            existing = session_sandbox.read_file(tenant_id, session, clean)
        except session_sandbox.SessionSandboxError as exc:
            return {
                "success": False,
                "output": (
                    f"cannot append to {clean}: reading the current contents failed: {exc}"
                ),
                "raw": {"session_id": session_id, "path": clean, "root": root},
            }
        if len(existing) + len(chunk) > MAX_SANDBOX_FILE_BYTES:
            return {
                "success": False,
                "output": (
                    f"appending {len(chunk)} bytes would take {clean} past the "
                    f"{MAX_SANDBOX_FILE_BYTES}-byte file limit "
                    f"(currently {len(existing)})"
                ),
                "raw": {"session_id": session_id, "path": clean, "root": root},
            }
        total = existing + chunk

    try:
        session_sandbox.write_file(tenant_id, session, clean, total)
    except session_sandbox.SessionSandboxError as exc:
        return {
            "success": False,
            "output": f"failed to write {clean}: {exc}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }

    if mode == "append":
        size_line = f"appended={len(chunk)}, total_bytes={len(total)}"
    else:
        size_line = f"bytes={len(total)}"
    return {
        "success": True,
        "output": f"=== Wrote sandbox file: {clean} ===\n\n{size_line}\n",
        "raw": {
            "display_type": "write_sandbox_file",
            "session_id": session_id,
            "path": clean,
            "root": root,
            "name": posixpath.basename(clean),
            "size": len(total),
            "mode": mode,
            "appended": len(chunk),
        },
    }


def _edit_workspace_file(
    tenant_id: int,
    session: dict[str, Any],
    session_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    requested_path = str(arguments.get("path") or "").strip()
    if not requested_path:
        return {
            "success": False,
            "output": (
                "path is required; edit a file inside the session sandbox "
                "(not /workspace/input)"
            ),
            "raw": {"session_id": session_id},
        }
    if "\x00" in requested_path:
        return {
            "success": False,
            "output": "path is invalid",
            "raw": {"session_id": session_id},
        }
    clean = _resolve_workspace_path(requested_path)
    root = _writable_sandbox_root(clean)
    if not root:
        return {
            "success": False,
            "output": _sandbox_write_scope_error(requested_path),
            "raw": {"session_id": session_id, "path": clean},
        }
    edits, error = _normalize_sandbox_edits(arguments.get("edits"))
    if error:
        return {
            "success": False,
            "output": error,
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }

    try:
        stat = session_sandbox.stat_file(tenant_id, session, clean)
    except session_sandbox.SessionSandboxError as exc:
        return {
            "success": False,
            "output": f"failed to stat {clean}: {exc}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    kind = str(stat.get("kind") or "")
    size = int(stat.get("size") or 0)
    if kind == "missing":
        return {
            "success": False,
            "output": f"file not found: {clean}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if kind == "directory":
        return {
            "success": False,
            "output": f"{clean} is a directory; edit_sandbox_file only edits files",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if kind != "file":
        return {
            "success": False,
            "output": f"{clean} is not a regular file",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if size > MAX_WRITE_SANDBOX_BYTES:
        return {
            "success": False,
            "output": (
                f"file too large to edit ({size} bytes; max {MAX_WRITE_SANDBOX_BYTES}). "
                "Split the work or rewrite a smaller file with write_sandbox_file"
            ),
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    try:
        raw = session_sandbox.read_file(tenant_id, session, clean)
    except session_sandbox.SessionSandboxError as exc:
        return {
            "success": False,
            "output": f"failed to read {clean}: {exc}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if len(raw) > MAX_WRITE_SANDBOX_BYTES:
        return {
            "success": False,
            "output": (
                f"file too large to edit ({len(raw)} bytes; max "
                f"{MAX_WRITE_SANDBOX_BYTES})"
            ),
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if _is_binary_content(raw):
        return {
            "success": False,
            "output": (
                "binary files cannot be edited; write a text script and have it "
                "produce binary artifacts under /workspace/output"
            ),
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    try:
        updated, replacements = _apply_sandbox_edits(raw.decode("utf-8"), edits)
    except (UnicodeDecodeError, ValueError) as exc:
        return {
            "success": False,
            "output": str(exc),
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    content = updated.encode("utf-8")
    if len(content) > MAX_WRITE_SANDBOX_BYTES:
        return {
            "success": False,
            "output": (
                f"result too large ({len(content)} bytes; max {MAX_WRITE_SANDBOX_BYTES}). "
                "Shrink new_string or split the file"
            ),
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    if _is_binary_content(content):
        return {
            "success": False,
            "output": "replacement would introduce binary content, which is not accepted",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    try:
        session_sandbox.write_file(tenant_id, session, clean, content)
    except session_sandbox.SessionSandboxError as exc:
        return {
            "success": False,
            "output": f"failed to write {clean}: {exc}",
            "raw": {"session_id": session_id, "path": clean, "root": root},
        }
    return {
        "success": True,
        "output": (
            f"=== Edited sandbox file: {clean} ===\n\n"
            f"replacements={replacements}\nbytes={len(content)}\n"
        ),
        "raw": {
            "display_type": "edit_sandbox_file",
            "session_id": session_id,
            "path": clean,
            "root": root,
            "name": posixpath.basename(clean),
            "size": len(content),
            "replacements": replacements,
        },
    }


def _image_size(data: bytes, image_format: str) -> tuple[int, int]:
    if image_format == "png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise ValueError("invalid PNG")
        return struct.unpack(">II", data[16:24])
    if image_format != "jpeg" or len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("invalid image")
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            raise ValueError("invalid JPEG")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            break
        size = struct.unpack(">H", data[offset:offset + 2])[0]
        if size < 2 or offset + size > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if size < 7:
                break
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height
        offset += size
    raise ValueError("invalid JPEG dimensions")


def _browser_result(method: str, result: dict[str, Any]) -> tuple[bool, str, dict[str, Any], list[str]]:
    if method in {"navigate", "navigate_back", "navigate_forward", "reload", "wait_for_navigation"}:
        if result.get("reached") == "timeout":
            message = (
                "Navigation did not reach the requested loading phase. The page may already have changed. "
                "Observe current state before deciding what remains; do not automatically reload or repeat navigation."
            )
            return False, message, result, []

    if method == "evaluate":
        if result.get("ok") is not True:
            failure = result.get("error")
            detail = str(failure.get("text") or "") if isinstance(failure, dict) else ""
            if "Illegal return statement" in detail:
                message = (
                    "evaluate received a top-level return. Use an expression or an IIFE, for example "
                    "(() => { return document.title; })(). Correct the syntax and inspect page state before "
                    "repeating mutations. For scrolling, use wheel with delta_y or scroll_to with an observed ref."
                )
            else:
                message = (
                    "Browser evaluate failed or returned an invalid result. Inspect ok/error; return bounded "
                    "serializable data, not DOM nodes. Observe before retrying."
                )
            return False, message, result, []

    if method == "request_help":
        outcome = result.get("outcome")
        if outcome not in {"continued", "completed"}:
            message = (
                f"Browser human step did not complete (outcome={json.dumps(outcome, ensure_ascii=False)}). "
                "Stop browser actions until the user resumes in a new turn."
            )
            return False, message, result, []

    if method != "screenshot":
        return True, _truncate_browser_output(json.dumps(result, ensure_ascii=False)), result, []

    image = result.get("image_base64")
    image_format = str(result.get("format") or "").lower()
    if not isinstance(image, str) or image_format not in {"png", "jpeg"} or len(image) > MAX_BROWSER_SCREENSHOT_BYTES:
        return False, "Browser screenshot is invalid or too large; retry with a fresh element ref for a smaller capture.", result, []
    try:
        decoded = base64.b64decode(image, validate=True)
        if not decoded or len(decoded) > MAX_BROWSER_SCREENSHOT_BYTES:
            raise ValueError("invalid screenshot size")
        width, height = _image_size(decoded, image_format)
        if width <= 0 or height <= 0 or width * height > MAX_BROWSER_SCREENSHOT_PIXELS:
            raise ValueError("invalid screenshot dimensions")
    except (ValueError, binascii.Error):
        return False, "Browser screenshot is invalid or too large; retry with a fresh element ref for a smaller capture.", result, []

    metadata: dict[str, Any] = {
        "tab_id": result.get("tab_id") or 0,
        "format": image_format,
        "width": width,
        "height": height,
    }
    if result.get("dialogs"):
        metadata["dialogs"] = result["dialogs"]
    raw = {**result, "width": width, "height": height}
    return True, json.dumps(metadata, ensure_ascii=False), raw, [
        f"data:image/{image_format};base64,{image}"
    ]


def _browser_error_data(data: Any) -> Any:
    return browser_skill.bounded_error_data(data)


def _browser_recovery_hint(method: str, error: browser_skill.BrowserSkillError) -> str:
    data = error.data if isinstance(error.data, dict) else {}
    reason = str(data.get("reason") or "")
    effect = str(data.get("effect_state") or "")
    message = str(error)
    if method == "tab_borrow" and reason == "confirmation_ui_unavailable":
        return (
            "Ask the user to open an HTTP(S) page in a regular browser window so BrowserSkill can "
            "display the borrow confirmation, then request tab_borrow again. Task windows, separate "
            "popups and extension settings cannot host that confirmation. Do not retry until a page is available."
        )
    if method == "tab_borrow" and error.code == "invalid_params" and "not authorized and already lives in the Agent Window" in str(error):
        return "Ask the user to move this unowned tab to a regular browser window before calling tab_borrow again. If its live state is unnecessary, navigate in an owned task tab instead. Being inside the Agent Window does not authorize access."
    if method == "tab_borrow" and (
        reason == "confirmation_timeout"
        or error.code in {"timeout", "cancelled", "user_aborted"}
    ):
        return (
            "Stop browser actions: tab borrowing was not approved. The confirmation is no longer pending. "
            "Ask the user to click Continue operation in the conversation preview before continuing in this "
            "same conversation. Continue operation only resumes the task; it does not grant tab access. After "
            "explicit resume, request tab_borrow once again and wait for Allow in the browser's BrowserSkill "
            "confirmation. Do not call request_help on the unapproved tab or reconnect/pair again."
        )
    if error.code == "permission_denied" and (
        "borrow this tab before" in message
        or "Authorize this tab with tab_borrow" in message
    ):
        return (
            "This tab is not authorized for the current task. If its existing page state is needed, "
            "call tab_borrow once and follow the extension's browser-controlled approval flow. "
            "Do not retry tab_select/read/close or call request_help before borrowing succeeds. "
            "For an ordinary website lookup that does not need this tab's state, navigate in the task's own tab."
        )
    if error.code in {"user_aborted", "cancelled", "task_paused", "timeout"}:
        return (
            "Stop browser actions. Ask the user to complete any manual step and click Continue operation "
            "in the conversation browser preview, then continue in the same conversation. A paused task "
            "does not require reconnection or pairing. The action may already have taken effect. Observe "
            "before acting; do not replay the interrupted action."
        )
    if effect in {"unknown", "committed"}:
        return "The action may already have taken effect. Observe the current state before deciding what remains; do not repeat the action blindly."
    if reason == "ref_not_found":
        return "Observe the current tab and retry once using a fresh ref. Do not guess another ref."
    if reason == "element_not_visible":
        return (
            "Observe the active dialog/menu and target visibility. Reveal the intended control before "
            "acting; do not click adjacent refs or hidden elements."
        )
    if reason == "selector_not_found":
        return (
            "Observe the current page and locate the intended target. Do not guess selectors; refs can "
            "address iframe/shadow-root controls."
        )
    if reason in {
        "target_not_fillable", "fill_value_invalid", "fill_target_changed", "fill_focus_lost",
        "fill_value_mismatch", "fill_failed",
    }:
        return (
            "Observe the current editable field and its value first. The page may have changed or "
            "formatted it. Correct only the remaining difference; do not blindly repeat fill."
        )
    if reason in {"target_not_select", "option_not_found", "single_select_value_count"}:
        return (
            "select requires a native select and its option values. Inspect current options; use "
            "click/observe for a custom dropdown."
        )
    if reason == "renderer_read_timeout":
        return "A browser read is unavailable and further reads may be refused while it is still pending. Do not repeat snapshot/observe on this tab. Switch to another tab, or finish the turn and report the unavailable read."
    if reason in {"agent_window_scope", "borrow_conflict"}:
        return "List actual user tabs and respect tab ownership. Borrow the intended available tab through extension confirmation; never guess its ID."
    if error.code == "invalid_params":
        if method == "press":
            return "press accepts a key or shortcut such as Enter or Ctrl+A. For text input use fill with a fresh ref and value."
        return (
            "Use the method's documented fields at the top level. Correct the reported argument "
            "instead of guessing alternative parameter names."
        )
    if error.code in {"timeout", "cdp_failed"}:
        return "Inspect current page state before retrying: the action may already have occurred. Stop repeated attempts without new evidence; do not automatically reload or replay mutations."
    if error.code == "not_found":
        return "Refresh the current tab/page state and use returned identifiers. Do not invent tab, session or element IDs."
    return "Use the error details to identify the blocker. Do not repeat an unchanged failed action or bypass browser permissions."


def _browser_error_output(method: str, error: browser_skill.BrowserSkillError) -> str:
    details = {"code": error.code or "browser_error", "message": str(error)}
    bounded = _browser_error_data(error.data)
    if bounded is not None:
        details["data"] = bounded
    return json.dumps(
        {"method": method, "error": details, "recovery_hint": _browser_recovery_hint(method, error)},
        ensure_ascii=False,
    )


def _service(service_id: str, tenant_id: int) -> dict[str, Any]:
    row = db.fetch_one(
        "SELECT * FROM mcp_services WHERE id=? AND tenant_id=? AND deleted_at IS NULL AND enabled=1",
        (service_id, tenant_id),
    )
    return row or {}


def _directory(service: dict[str, Any], tenant_id: int, user_id: str) -> list[dict[str, Any]]:
    row = db.fetch_one(
        "SELECT * FROM mcp_metadata WHERE tenant_id=? AND service_id=? AND principal=''",
        (tenant_id, service["id"]),
    )
    if row is not None:
        tools = loads(row.get("tools"), [])
        if isinstance(tools, list) and str(row.get("config_fingerprint") or "") == mcp_client.configuration_fingerprint(service):
            return [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]
    client = mcp_client.MCPClient(service, tenant_id=tenant_id, user_id=user_id)
    try:
        client.connect()
        client.initialize()
        payload = mcp_client.metadata_payload(client)
    finally:
        client.close()
    now = db.utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mcp_metadata(tenant_id, service_id, principal, config_fingerprint, tools, instructions, server_name, server_version, server_description, synced_at) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(tenant_id, service_id, principal) DO UPDATE SET config_fingerprint=excluded.config_fingerprint, tools=excluded.tools, instructions=excluded.instructions, server_name=excluded.server_name, server_version=excluded.server_version, server_description=excluded.server_description, synced_at=excluded.synced_at",
            (
                tenant_id, service["id"], mcp_client.configuration_fingerprint(service), dumps(payload["tools"]),
                payload["instructions"], payload["server_name"], payload["server_version"],
                payload["server_description"], now,
            ),
        )
    return payload["tools"]


def load_mcp_tools(tenant_id: int, user_id: str, service_ids: list[str]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    used_names: set[str] = set()
    for service_id in service_ids:
        service = _service(service_id, tenant_id)
        if not service:
            continue
        policies = {
            row["tool_name"]: row
            for row in db.fetch_all(
                "SELECT * FROM mcp_tool_approvals WHERE tenant_id=? AND service_id=?",
                (tenant_id, service_id),
            )
        }
        try:
            directory = _directory(service, tenant_id, user_id)
        except mcp_client.MCPError:
            continue
        for tool in directory:
            tool_name = str(tool.get("name") or "")
            policy = policies.get(tool_name) or {}
            if not bool(policy.get("enabled", 1)):
                continue
            name = qualified_name(str(service.get("name") or ""), tool_name)
            if not name or name in used_names:
                continue
            used_names.add(name)
            schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"}
            if not isinstance(schema, dict):
                schema = {"type": "object"}
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": str(tool.get("description") or f"MCP tool {tool_name} from {service.get('name')}"),
                        "parameters": schema,
                    },
                    "service_id": service_id,
                    "tool_name": tool_name,
                    "require_approval": bool(policy.get("require_approval", 0)),
                }
            )
    return tools


def builtin_knowledge_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": "Search the selected knowledge bases and return matching source chunks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
                },
                "required": ["query"],
            },
        },
        "service_id": "",
        "tool_name": "knowledge_search",
        "require_approval": False,
    }


def builtin_read_file_tool(
    available_skills: list[str], *, workspace: bool = False
) -> dict[str, Any]:
    scopes: list[str] = []
    if workspace:
        scopes.append(
            "Sandbox files: absolute paths inside the current session's sandbox, including /tmp; "
            "relative paths resolve from /workspace. This does not read host files or publish "
            "files as user-visible artifacts."
        )
    if available_skills:
        scopes.append(
            "Skill resources: skill://<name>/SKILL.md loads an allowed skill's instructions; "
            "skill://<name>/<relative-file> reads a bundled resource. These are package resources, "
            "not shell paths or arbitrary host files."
        )
    description = "Read text from the available file sources.\n" + "\n".join(scopes)
    if description.endswith("."):
        description += "\n"
    description += (
        "offset is a 1-based line number; limit defaults to 2000 lines. max_bytes is capped "
        "at 65536; the tool output budget also applies. Continue at the returned next_offset "
        "when truncated. Binary content is suppressed."
    )
    return {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path inside the current session sandbox (relative to /workspace), "
                            "or a skill:// resource"
                        ),
                    },
                    "line_offset": {
                        "type": "integer",
                        "description": "Web only: character offset within a long line",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1-based line number; continue at next_offset",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum lines to return; defaults to 2000",
                    },
                    "max_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": DEFAULT_READ_FILE_MAX_BYTES,
                        "description": "Text byte budget; at most 65536",
                    },
                },
                "required": ["path"],
            },
        },
        "service_id": "",
        "tool_name": "read_file",
        "require_approval": False,
        "skill_names": available_skills,
    }


def builtin_list_sandbox_files_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "list_sandbox_files",
            "description": (
                "List files inside the current session's sandbox when no shell executor is "
                "available.\nRelative paths resolve from /workspace; omitted path lists "
                "/workspace/output.\nUse known paths directly with read_file; list only to "
                "discover unknown files.\nResults are bounded by max_entries. An "
                "unprovisioned session returns an empty listing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Optional absolute or /workspace-relative sandbox path to list. "
                            "Defaults to /workspace/output."
                        ),
                    },
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIST_SANDBOX_MAX_ENTRIES,
                        "description": (
                            "Optional cap on entries returned. Defaults to 200, hard-capped "
                            "at 500."
                        ),
                    },
                },
            },
        },
        "service_id": "",
        "tool_name": "list_sandbox_files",
        "require_approval": False,
    }


def builtin_write_sandbox_file_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "write_sandbox_file",
            "description": (
                "Create, overwrite, or append a text file inside the current session's "
                "sandbox, excluding /workspace/input.\n/workspace/output is collected for "
                "download, so it holds finished deliverables only; put drafts, scratch and "
                "intermediate files in any other directory under /workspace. Relative paths "
                "resolve from /workspace. Send both path and content (path first). Use "
                "edit_sandbox_file for small changes to an existing file. File content does "
                "not pass through shell quoting.\nLarge files: first call uses mode=overwrite "
                "(default), subsequent calls use mode=append with only the next chunk. Keep "
                "calls in order and inspect the reported running byte count. A refused call "
                "wrote nothing; retry that chunk with complete JSON, never duplicate successful "
                "chunks.\nKeep content under about 262144 bytes per call; the whole file is "
                "capped at 8388608 bytes.\nBinary content is not accepted. The result reports "
                "the absolute path and total size without echoing content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or /workspace-relative sandbox path to write, "
                            "excluding /workspace/input."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Text to write. In overwrite mode this is the full file; in "
                            "append mode it is only the next chunk. Do not send binary bytes."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "default": "overwrite",
                        "description": (
                            "overwrite (default) replaces the file; append adds to an "
                            "existing file."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
        "service_id": "",
        "tool_name": "write_sandbox_file",
        "require_approval": False,
    }


def builtin_edit_sandbox_file_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "edit_sandbox_file",
            "description": (
                "Apply exact text replacements to an existing text file inside the session "
                "sandbox, excluding /workspace/input.\nRead the relevant content first. Send "
                "edits as an array, even for one replacement. Every old_string matches the "
                "original file, must be unique unless replace_all=true, and must not overlap "
                "another edit. Include enough surrounding text to identify the intended "
                "occurrence.\nAll replacements are validated before writing; a failed match "
                "leaves the file unchanged. The result reports the path, replacement count "
                "and size without echoing content. Use write_sandbox_file for new files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Absolute or /workspace-relative sandbox path of an existing "
                            "text file (not /workspace/input)."
                        ),
                    },
                    "edits": {
                        "type": "array",
                        "description": (
                            "Replacements to apply, as an array even for a single change. "
                            "Each old_string is matched against the original file."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_string": {
                                    "type": "string",
                                    "description": "Exact text to find.",
                                },
                                "new_string": {
                                    "type": "string",
                                    "description": "Replacement text; may be empty.",
                                },
                                "replace_all": {
                                    "type": "boolean",
                                    "default": False,
                                    "description": "Replace every occurrence instead of requiring uniqueness.",
                                },
                            },
                            "required": ["old_string", "new_string"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
        },
        "service_id": "",
        "tool_name": "edit_sandbox_file",
        "require_approval": False,
    }


def builtin_web_search_tool(max_results: Any = 5) -> dict[str, Any]:
    try:
        effective = int(max_results or 0)
    except (TypeError, ValueError):
        effective = 0
    if effective <= 0:
        effective = 5
    effective = min(effective, 20)
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the public web for current information and return titles, URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": effective,
                        "default": effective,
                        "description": (
                            f"Number of results to return, 1 to {effective} "
                            "(the configured maximum). Omit to use the maximum."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
        "service_id": "",
        "tool_name": "web_search",
        "require_approval": False,
        "_max_results": effective,
    }


def builtin_local_browser_tool() -> dict[str, Any]:
    return browser_skill.local_browser_tool()


def builtin_shell_exec_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "shell_exec",
            "description": (
                "Run one Bash command inside this conversation's Docker/Cube/E2B sandbox. Commands start "
                "in /workspace and the sandbox persists across turns. Non-zero exits are returned "
                "as normal results so you can inspect stderr and iterate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to execute"},
                    "work_dir": {"type": "string", "description": "Working directory; defaults to /workspace"},
                    "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 600},
                    "max_output_bytes": {"type": "integer", "minimum": 1, "maximum": 65536},
                    "max_stderr_bytes": {"type": "integer", "minimum": 1, "maximum": 16384},
                    "env": {"type": "object", "additionalProperties": {"type": "string"}},
                },
                "required": ["command"],
            },
        },
        "service_id": "",
        "tool_name": "shell_exec",
        "require_approval": False,
    }


def execute_tool(
    tool: dict[str, Any],
    arguments: dict[str, Any],
    *,
    tenant_id: int,
    user_id: str,
    knowledge_base_ids: list[str] | None = None,
    allowed_skills: list[str] | None = None,
    skip_approval: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    service_id = str(tool.get("service_id") or "")
    tool_name = str(tool.get("tool_name") or "")
    name = str(tool.get("function", {}).get("name") or tool_name)
    if service_id:
        policy = db.fetch_one(
            "SELECT * FROM mcp_tool_approvals WHERE tenant_id=? AND service_id=? AND tool_name=?",
            (tenant_id, service_id, tool_name),
        ) or {}
        if bool(policy.get("require_approval", 0)) and not skip_approval:
            raise ToolApprovalRequired(name, service_id, tool_name, arguments)
        service = _service(service_id, tenant_id)
        if not service:
            raise mcp_client.MCPError("MCP service not found or disabled")
        client = mcp_client.MCPClient(service, tenant_id=tenant_id, user_id=user_id)
        try:
            client.connect()
            client.initialize()
            result = client.call_tool(tool_name, arguments)
        except mcp_client.MCPError as exc:
            if exc.oauth_required:
                raise MCPOAuthRequired(name, service_id, tool_name, arguments) from exc
            raise
        finally:
            client.close()
        text = mcp_client.tool_text(result)
        return {
            "success": not bool(result.get("isError")),
            "output": text,
            "images": mcp_client.tool_images(result),
            "raw": result,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    if tool_name in wiki_agent_tools.TOOL_NAMES:
        rendered = wiki_agent_tools.execute(
            tool_name,
            arguments,
            tenant_id=tenant_id,
            user_id=user_id,
            knowledge_base_ids=knowledge_base_ids,
        )
        rendered["duration_ms"] = int((time.monotonic() - started) * 1000)
        return rendered
    if tool_name == "knowledge_search":
        query = str(arguments.get("query") or "").strip()
        limit = min(20, max(1, int(arguments.get("limit") or 8)))
        rows = retrieve(tenant_id, query, knowledge_base_ids=knowledge_base_ids, limit=limit) if query else []
        payload = [
            {
                "title": row.get("title") or row.get("file_name") or "",
                "knowledge_base_id": row.get("knowledge_base_id") or "",
                "content": row.get("content") or "",
            }
            for row in rows
        ]
        return {
            "success": True,
            "output": json.dumps(payload, ensure_ascii=False),
            "raw": payload,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    if tool_name == "list_sandbox_files":
        session_id = str(tool.get("_session_id") or "")
        if not session_id:
            return {
                "success": False,
                "output": "list_sandbox_files requires an active conversation",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        session = db.fetch_one(
            "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (session_id, tenant_id),
        )
        if not session:
            return {
                "success": False,
                "output": "list_sandbox_files session not found",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        rendered = _list_workspace_files(
            tenant_id, session, session_id, arguments
        )
        rendered["duration_ms"] = int((time.monotonic() - started) * 1000)
        return rendered
    if tool_name in {"write_sandbox_file", "edit_sandbox_file"}:
        session_id = str(tool.get("_session_id") or "")
        if not session_id:
            return {
                "success": False,
                "output": f"{tool_name} requires an active conversation",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        session = db.fetch_one(
            "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (session_id, tenant_id),
        )
        if not session:
            return {
                "success": False,
                "output": f"{tool_name} session not found",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        handler = (
            _write_workspace_file
            if tool_name == "write_sandbox_file"
            else _edit_workspace_file
        )
        rendered = handler(tenant_id, session, session_id, arguments)
        rendered["duration_ms"] = int((time.monotonic() - started) * 1000)
        return rendered
    if tool_name == "read_file":
        requested_path = str(arguments.get("path") or "").strip()
        session_id = str(tool.get("_session_id") or "")
        if not requested_path:
            return {
                "success": False,
                "output": (
                    "path is required; use a known sandbox path or a skill resource "
                    "from the available skills list"
                ),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        try:
            line_offset = int(arguments.get("line_offset") or 0)
        except (TypeError, ValueError):
            line_offset = 0
        if line_offset:
            return {
                "success": False,
                "output": "line_offset is supported only for saved web:// pages",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        if requested_path.startswith("skill://"):
            try:
                skill_name, relative_path = skill_display_name(requested_path)
                segments = relative_path.split("/")
                if (
                    any(segment in {"", ".", ".."} for segment in segments)
                    or "\\" in skill_name + relative_path
                    or "\x00" in skill_name + relative_path
                ):
                    raise ValueError(
                        "skill resource must have a canonical relative file path without traversal"
                    )
                allowed = set(tool.get("skill_names") or allowed_skills or [])
                if allowed and skill_name not in allowed:
                    raise ValueError(f"skill {skill_name!r} is not available to this agent")
                content = read_skill_file(tenant_id, skill_name, relative_path)
            except ValueError as exc:
                return {
                    "success": False,
                    "output": str(exc),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            data = content.encode("utf-8")
            if len(data) > MAX_READ_FILE_DOWNLOAD_BYTES:
                return {
                    "success": False,
                    "output": (
                        f"skill resource exceeds the {MAX_READ_FILE_DOWNLOAD_BYTES}-byte "
                        "read limit; split the package resource into smaller files"
                    ),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            try:
                offset = int(arguments.get("offset") or 0)
                limit = int(arguments.get("limit") or 0)
                max_bytes = int(arguments.get("max_bytes") or 0)
            except (TypeError, ValueError):
                offset, limit, max_bytes = 0, 0, 0
            rendered = _render_file_page(
                data,
                requested_path,
                f"skill://{skill_name}",
                session_id,
                offset=offset,
                limit=limit,
                max_bytes=max_bytes,
            )
            raw = rendered.get("raw")
            if isinstance(raw, dict):
                raw["skill_name"] = skill_name
                raw["file_path"] = relative_path
            rendered["duration_ms"] = int((time.monotonic() - started) * 1000)
            return rendered

        if not session_id:
            return {
                "success": False,
                "output": "read_file requires an active conversation for sandbox file access",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        session = db.fetch_one(
            "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (session_id, tenant_id),
        )
        if not session:
            return {
                "success": False,
                "output": "read_file session not found",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        rendered = _read_workspace_file(
            tenant_id, session, session_id, requested_path, arguments
        )
        rendered["duration_ms"] = int((time.monotonic() - started) * 1000)
        return rendered
    if tool_name == "web_search":
        provider = web_search_service.default_provider(tenant_id)
        if not provider:
            return {
                "success": False,
                "output": "No web search provider is configured.",
                "raw": [],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        query = str(arguments.get("query") or "").strip()
        raw_limit = arguments.get("limit")
        try:
            maximum = int(tool.get("_max_results") or 0)
        except (TypeError, ValueError):
            maximum = 0
        if maximum <= 0:
            schema_limit = (
                tool.get("function", {})
                .get("parameters", {})
                .get("properties", {})
                .get("limit", {})
            )
            try:
                maximum = int(schema_limit.get("maximum") or 0)
            except (TypeError, ValueError, AttributeError):
                maximum = 0
        if maximum <= 0:
            maximum = 5
        maximum = min(maximum, 20)
        requested = maximum
        if raw_limit is not None:
            try:
                requested = int(raw_limit)
            except (TypeError, ValueError):
                return {
                    "success": False,
                    "output": f"limit must be between 1 and {maximum}",
                    "raw": [],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
            if requested < 1:
                return {
                    "success": False,
                    "output": f"limit must be between 1 and {maximum}",
                    "raw": [],
                    "duration_ms": int((time.monotonic() - started) * 1000),
                }
        clamped = requested > maximum
        limit = min(requested, maximum)
        try:
            results = web_search_service.search(str(provider.get("provider") or ""), provider.get("parameters"), query, limit)
        except web_search_service.WebSearchError as exc:
            return {
                "success": False,
                "output": str(exc),
                "raw": [],
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        output = json.dumps(results, ensure_ascii=False)
        if clamped:
            output += (
                f"\nNote: requested limit {requested} exceeds the configured maximum "
                f"{maximum}; clamped to {maximum}."
            )
        return {
            "success": True,
            "output": output,
            "raw": results,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    if tool_name == "shell_exec":
        session_id = str(tool.get("_session_id") or "")
        command = str(arguments.get("command") or "")
        if not session_id:
            return {
                "success": False,
                "output": "shell_exec requires an active conversation",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        if not command.strip():
            return {
                "success": False,
                "output": "shell_exec command is required",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        if len(command.encode("utf-8")) > MAX_SHELL_COMMAND_BYTES:
            return {
                "success": False,
                "output": "shell_exec command is too large; write a workspace file and execute that instead",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        session = db.fetch_one(
            "SELECT * FROM sessions WHERE id=? AND tenant_id=? AND deleted_at IS NULL",
            (session_id, tenant_id),
        )
        if not session:
            return {
                "success": False,
                "output": "shell_exec session not found",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        try:
            timeout = min(600, max(1, int(arguments.get("timeout_sec") or 120)))
            stdout_limit = min(
                MAX_SHELL_OUTPUT_BYTES, max(1, int(arguments.get("max_output_bytes") or 16384))
            )
            stderr_limit = min(
                MAX_SHELL_STDERR_BYTES, max(1, int(arguments.get("max_stderr_bytes") or 8192))
            )
        except (TypeError, ValueError):
            timeout, stdout_limit, stderr_limit = 120, 16384, 8192
        raw_env = arguments.get("env")
        env = (
            {str(key): str(value) for key, value in raw_env.items()}
            if isinstance(raw_env, dict)
            else {}
        )
        try:
            result = session_sandbox.exec_command(
                tenant_id,
                session,
                command,
                work_dir=str(arguments.get("work_dir") or "/workspace"),
                timeout=timeout,
                env=env,
            )
        except session_sandbox.SessionSandboxError as exc:
            return {
                "success": False,
                "output": json.dumps(
                    {"code": exc.code, "message": str(exc)}, ensure_ascii=False
                ),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        stdout, stdout_truncated = _truncate_utf8(result.stdout, stdout_limit)
        stderr, stderr_truncated = _truncate_utf8(result.stderr, stderr_limit)
        payload = {
            "session_id": session_id,
            "command": command,
            "exit_code": result.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "latency_ms": result.latency_ms,
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
        }
        raw = dict(payload)
        visible = _truncate_utf8(json.dumps(payload, ensure_ascii=False), MAX_SHELL_OUTPUT_BYTES)[0]
        return {
            "success": True,
            "output": visible,
            "raw": raw,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    if tool_name == "local_browser":
        session_id = str(tool.get("_session_id") or "")
        if not session_id:
            return {
                "success": False,
                "output": "local_browser requires an active conversation",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        scope = browser_skill.scope_key(tenant_id, user_id)
        try:
            browser_skill.validate_browser_arguments(arguments)
        except ValueError as exc:
            browser_skill.mark_turn_failed(scope, session_id)
            return {
                "success": False,
                "output": f"Invalid browser arguments: {exc}",
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        method = str(arguments["method"]).strip()
        params = browser_skill.browser_call_params(method, arguments)
        keep_open = arguments.get("keep_open")
        try:
            result = browser_skill.call_tool(
                scope,
                session_id,
                method,
                params,
                keep_open=keep_open if isinstance(keep_open, bool) else None,
            )
        except browser_skill.BrowserSkillError as exc:
            browser_skill.mark_turn_failed(scope, session_id)
            return {
                "success": False,
                "output": _browser_error_output(method, exc),
                "duration_ms": int((time.monotonic() - started) * 1000),
            }
        success, output, raw, images = _browser_result(method, result)
        if not success:
            browser_skill.mark_turn_failed(scope, session_id)
        return {
            "success": success,
            "output": output,
            "raw": raw,
            "images": images,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    raise mcp_client.MCPError(f"Unknown tool: {name}")

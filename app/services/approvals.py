"""In-process human approval gates for tool calls and MCP OAuth prompts."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .. import db


class PendingNotFound(RuntimeError):
    pass


class AlreadyResolved(RuntimeError):
    pass


class TenantMismatch(RuntimeError):
    pass


class UserMismatch(RuntimeError):
    pass


@dataclass(slots=True)
class Decision:
    approved: bool
    modified_args: dict[str, Any] | None = None
    reason: str = ""
    timed_out: bool = False
    canceled: bool = False


@dataclass(slots=True)
class Pending:
    id: str
    kind: str
    tenant_id: int
    user_id: str
    session_id: str
    assistant_message_id: str
    request_id: str
    service_id: str
    service_name: str
    mcp_tool_name: str
    registered_tool_name: str
    description: str
    arguments: dict[str, Any]
    tool_call_id: str
    timeout_seconds: int
    requested_at: int
    future: asyncio.Future[Decision] = field(repr=False)

    def public(self) -> dict[str, Any]:
        result = {
            "pending_id": self.id,
            "service_id": self.service_id,
            "service_name": self.service_name,
            "mcp_tool_name": self.mcp_tool_name,
            "timeout_seconds": self.timeout_seconds,
            "requested_at": self.requested_at,
            "tool_call_id": self.tool_call_id,
        }
        if self.kind == "tool":
            result.update(
                {
                    "registered_tool_name": self.registered_tool_name,
                    "description": self.description,
                    "args_json": json.dumps(self.arguments, ensure_ascii=False),
                }
            )
        return result


_pending: dict[str, Pending] = {}


def _timeout_seconds(timeout_seconds: int | None) -> int:
    if timeout_seconds and timeout_seconds > 0:
        return timeout_seconds
    try:
        return max(1, int(os.getenv("WEKNORA_AGENT_TOOL_APPROVAL_TIMEOUT_SECONDS", "600")))
    except ValueError:
        return 600


def _prune() -> None:
    cutoff = time.time()
    for pending_id, pending in list(_pending.items()):
        if pending.requested_at + pending.timeout_seconds < cutoff and pending.future.done():
            _pending.pop(pending_id, None)
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM py_pending_approvals WHERE requested_at + timeout_seconds < ?",
            (int(cutoff) - 3600,),
        )


def _decision_from_row(row: dict[str, Any]) -> Decision:
    try:
        value = json.loads(str(row.get("decision") or "{}"))
    except ValueError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    modified = value.get("modified_args")
    return Decision(
        approved=bool(value.get("approved")),
        modified_args=modified if isinstance(modified, dict) else None,
        reason=str(value.get("reason") or ""),
        timed_out=bool(value.get("timed_out")),
        canceled=bool(value.get("canceled")),
    )


def _decision_json(decision: Decision) -> str:
    return json.dumps(
        {
            "approved": decision.approved,
            "modified_args": decision.modified_args,
            "reason": decision.reason,
            "timed_out": decision.timed_out,
            "canceled": decision.canceled,
        },
        ensure_ascii=False,
    )


def _pending_from_row(row: dict[str, Any]) -> Pending:
    arguments = json.loads(str(row.get("arguments") or "{}"))
    if not isinstance(arguments, dict):
        arguments = {}
    return Pending(
        id=str(row["id"]),
        kind=str(row["kind"]),
        tenant_id=int(row["tenant_id"]),
        user_id=str(row["user_id"]),
        session_id=str(row["session_id"]),
        assistant_message_id=str(row["assistant_message_id"]),
        request_id=str(row["request_id"]),
        service_id=str(row["service_id"]),
        service_name=str(row["service_name"]),
        mcp_tool_name=str(row["mcp_tool_name"]),
        registered_tool_name=str(row["registered_tool_name"]),
        description=str(row["description"]),
        arguments=arguments,
        tool_call_id=str(row["tool_call_id"]),
        timeout_seconds=int(row["timeout_seconds"]),
        requested_at=int(row["requested_at"]),
        future=asyncio.get_running_loop().create_future(),
    )


def _request(
    kind: str,
    *,
    tenant_id: int,
    user_id: str,
    session_id: str,
    assistant_message_id: str,
    request_id: str,
    service_id: str,
    service_name: str,
    mcp_tool_name: str,
    registered_tool_name: str = "",
    description: str = "",
    arguments: dict[str, Any] | None = None,
    tool_call_id: str = "",
    timeout_seconds: int | None = None,
) -> Pending:
    _prune()
    loop = asyncio.get_running_loop()
    pending = Pending(
        id=db.new_id(),
        kind=kind,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        request_id=request_id,
        service_id=service_id,
        service_name=service_name,
        mcp_tool_name=mcp_tool_name,
        registered_tool_name=registered_tool_name,
        description=description,
        arguments=dict(arguments or {}),
        tool_call_id=tool_call_id,
        timeout_seconds=_timeout_seconds(timeout_seconds),
        requested_at=int(time.time()),
        future=loop.create_future(),
    )
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_pending_approvals(id, kind, tenant_id, user_id, session_id, assistant_message_id, "
            "request_id, service_id, service_name, mcp_tool_name, registered_tool_name, description, arguments, "
            "tool_call_id, timeout_seconds, requested_at, status, decision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}')",
            (
                pending.id,
                pending.kind,
                pending.tenant_id,
                pending.user_id,
                pending.session_id,
                pending.assistant_message_id,
                pending.request_id,
                pending.service_id,
                pending.service_name,
                pending.mcp_tool_name,
                pending.registered_tool_name,
                pending.description,
                json.dumps(pending.arguments, ensure_ascii=False),
                pending.tool_call_id,
                pending.timeout_seconds,
                pending.requested_at,
            ),
        )
    _pending[pending.id] = pending
    return pending


def request_tool(
    *,
    tenant_id: int,
    user_id: str,
    session_id: str,
    assistant_message_id: str,
    request_id: str,
    service_id: str,
    service_name: str,
    mcp_tool_name: str,
    registered_tool_name: str,
    description: str,
    arguments: dict[str, Any],
    tool_call_id: str,
    timeout_seconds: int | None = None,
) -> Pending:
    return _request(
        "tool",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        request_id=request_id,
        service_id=service_id,
        service_name=service_name,
        mcp_tool_name=mcp_tool_name,
        registered_tool_name=registered_tool_name,
        description=description,
        arguments=arguments,
        tool_call_id=tool_call_id,
        timeout_seconds=timeout_seconds,
    )


def request_oauth(
    *,
    tenant_id: int,
    user_id: str,
    session_id: str,
    assistant_message_id: str,
    request_id: str,
    service_id: str,
    service_name: str,
    mcp_tool_name: str,
    tool_call_id: str,
    timeout_seconds: int | None = None,
) -> Pending:
    return _request(
        "oauth",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        assistant_message_id=assistant_message_id,
        request_id=request_id,
        service_id=service_id,
        service_name=service_name,
        mcp_tool_name=mcp_tool_name,
        tool_call_id=tool_call_id,
        timeout_seconds=timeout_seconds,
    )


async def wait(pending: Pending) -> Decision:
    try:
        deadline = pending.requested_at + pending.timeout_seconds
        while True:
            if pending.future.done():
                return pending.future.result()
            row = db.fetch_one("SELECT * FROM py_pending_approvals WHERE id=?", (pending.id,))
            if row is not None and str(row.get("status") or "") == "resolved":
                decision = _decision_from_row(row)
                if not pending.future.done():
                    pending.future.set_result(decision)
                return decision
            if row is None or time.time() >= deadline:
                decision = Decision(approved=False, reason="approval timed out", timed_out=True)
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE py_pending_approvals SET status='timed_out', decision=?, resolved_at=? "
                        "WHERE id=? AND status='pending'",
                        (_decision_json(decision), db.utcnow(), pending.id),
                    )
                if not pending.future.done():
                    pending.future.set_result(decision)
                return decision
            try:
                await asyncio.wait_for(
                    asyncio.shield(pending.future),
                    min(0.1, max(0.01, deadline - time.time())),
                )
            except asyncio.TimeoutError:
                continue
    finally:
        _pending.pop(pending.id, None)


def resolve(
    pending_id: str,
    decision: Decision,
    *,
    kind: str,
    tenant_id: int,
    user_id: str,
    session_id: str = "",
    service_id: str = "",
) -> Pending:
    row = db.fetch_one("SELECT * FROM py_pending_approvals WHERE id=?", (pending_id,))
    if row is None or str(row.get("kind") or "") != kind:
        raise PendingNotFound("pending approval not found or already completed")
    if int(row.get("tenant_id") or 0) != tenant_id:
        raise TenantMismatch("workspace mismatch")
    if str(row.get("user_id") or "") != user_id:
        raise UserMismatch("user mismatch: only the session owner may resolve this prompt")
    if session_id and str(row.get("session_id") or "") != session_id:
        raise UserMismatch("session mismatch: only the session owner may resolve this prompt")
    if service_id and str(row.get("service_id") or "") != service_id:
        raise PendingNotFound("pending approval not found for this service")
    if str(row.get("status") or "") != "pending":
        raise AlreadyResolved("pending approval already resolved")
    with db.transaction() as conn:
        cursor = conn.execute(
            "UPDATE py_pending_approvals SET status='resolved', decision=?, resolved_at=? "
            "WHERE id=? AND status='pending'",
            (_decision_json(decision), db.utcnow(), pending_id),
        )
        if int(cursor.rowcount or 0) == 0:
            raise AlreadyResolved("pending approval already resolved")
    pending = _pending.get(pending_id) or _pending_from_row(row)
    if not pending.future.done():
        pending.future.set_result(decision)
    return pending

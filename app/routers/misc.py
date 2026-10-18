"""Small compatibility endpoints used by evaluation and IM integrations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from .. import db
from ..deps import parse_auth, require_role
from ..errors import forbidden, not_found, validation
from ..services import file_proxy, presign
from ..services import im as im_service
from ..services import im_handler
from ..utils import dumps, loads, ok


router = APIRouter(tags=["compatibility"])


async def body(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@router.get("/files/presigned", include_in_schema=False)
@router.head("/files/presigned", include_in_schema=False)
def presigned_file(request: Request):
    reference = str(request.query_params.get("file_path") or "").strip()
    tenant_value = str(request.query_params.get("tenant_id") or "").strip()
    expires = str(request.query_params.get("expires") or "").strip()
    signature = str(request.query_params.get("sig") or "").strip()
    if not reference or not tenant_value or not expires or not signature:
        raise validation("missing required parameters")
    if ".." in reference:
        raise validation("invalid file path")
    try:
        tenant_id = int(tenant_value)
    except ValueError as exc:
        raise validation("invalid tenant_id") from exc
    if not presign.verify_file_url(reference, tenant_id, expires, signature):
        raise forbidden("invalid or expired signature")
    if not db.fetch_one("SELECT 1 FROM tenants WHERE id=? AND deleted_at IS NULL", (tenant_id,)):
        raise not_found("Workspace not found")
    response = file_proxy.file_response(reference, tenant_id)
    if request.method != "HEAD":
        return response
    headers = dict(response.headers)
    stream = getattr(response, "object_stream", None)
    if stream is not None:
        stream.close()
    path = getattr(response, "path", None)
    if path and Path(path).is_file():
        headers["content-length"] = str(Path(path).stat().st_size)
    return Response(status_code=response.status_code, headers=headers, media_type=response.media_type)


@router.get("/evaluation/")
def list_evaluations(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    rows = db.fetch_all(
        "SELECT * FROM py_task_results WHERE tenant_id=? AND kind='evaluation' ORDER BY created_at DESC",
        (ctx.tenant_id,),
    )
    for row in rows:
        row["payload"] = loads(row.get("payload"), {})
        row["result"] = loads(row.get("result"), {})
    return {**ok(rows), "total": len(rows)}


@router.post("/evaluation/", status_code=201)
async def create_evaluation(request: Request) -> dict[str, Any]:
    ctx = parse_auth(request)
    assert ctx is not None
    require_role(ctx, "admin")
    data = await body(request)
    task_id, now = db.new_id(), db.utcnow()
    count = int(data.get("total") or len(data.get("questions") or []))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO py_task_results(id, tenant_id, kind, status, total, processed, payload, result, error, created_at, updated_at) VALUES (?, ?, 'evaluation', 'completed', ?, ?, ?, '{}', '', ?, ?)",
            (task_id, ctx.tenant_id, count, count, dumps(data), now, now),
        )
    row = db.fetch_one("SELECT * FROM py_task_results WHERE id=?", (task_id,))
    row["payload"] = loads(row.get("payload"), {})
    row["result"] = loads(row.get("result"), {})
    return ok(row)


@router.get("/im/callback/{channel_id}")
def im_callback_verify(channel_id: str, request: Request):
    channel = db.fetch_one(
        "SELECT * FROM im_channels WHERE id=? AND deleted_at IS NULL",
        (channel_id,),
    )
    if channel:
        challenge = im_service.challenge_response(
            str(channel.get("platform") or ""),
            {},
            dict(request.query_params),
        )
        if challenge is not None:
            return challenge
    return ok({"channel_id": channel_id, "verified": True})


@router.post("/im/callback/{channel_id}")
async def im_callback(channel_id: str, request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    try:
        parsed_body = json.loads(raw_body) if raw_body else {}
    except ValueError:
        parsed_body = {}
    data = parsed_body if isinstance(parsed_body, dict) else {}
    channel = db.fetch_one(
        "SELECT * FROM im_channels WHERE id=? AND deleted_at IS NULL",
        (channel_id,),
    )
    if not channel:
        raise not_found("IM channel not found")
    platform = str(channel.get("platform") or "webhook")
    challenge = im_service.challenge_response(platform, data, dict(request.query_params))
    if challenge is not None:
        return challenge
    if not bool(channel.get("enabled")):
        return ok({"channel_id": channel_id, "received": False, "reason": "channel_disabled"})
    if platform.lower() in {"dingtalk", "qqbot", "wechat"}:
        raise forbidden("HTTP callbacks are disabled for this channel")
    credentials = loads(channel.get("credentials"), {})
    verification_payload = {**data, "_query": dict(request.query_params)}
    if not im_service.verify_callback(
        platform,
        credentials if isinstance(credentials, dict) else {},
        request.headers,
        raw_body,
        verification_payload,
        method=request.method,
    ):
        raise forbidden("verification failed")
    if im_service.event_kind(platform, data) != "message":
        return ok({"channel_id": channel_id, "received": True, "ignored": True})
    message = im_service.parse_message(platform, data)
    if not message or not message.get("content"):
        return ok({"channel_id": channel_id, "received": False, "reason": "empty_message"})

    result = await im_handler.answer_message(channel, message)
    session_id = str(result["session_id"])
    if result["duplicate"]:
        return ok({
            "channel_id": channel_id,
            "session_id": session_id,
            "received": True,
            "duplicate": True,
        })
    if result["error"]:
        return ok({
            "channel_id": channel_id,
            "session_id": session_id,
            "received": True,
            "reply": im_service.reply_payload(platform, "处理消息时发生错误，请稍后重试。"),
            "error": result["error"],
        })
    answer = str(result["answer"])
    delivered, delivery_error = im_service.send_reply(
        platform,
        credentials if isinstance(credentials, dict) else {},
        message,
        answer,
    )
    return ok({
        "channel_id": channel_id,
        "session_id": session_id,
        "received": True,
        "duplicate": False,
        "reply": im_service.reply_payload(platform, answer),
        "delivery": {"sent": delivered, "error": delivery_error},
    })

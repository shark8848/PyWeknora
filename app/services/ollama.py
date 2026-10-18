"""Ollama discovery and model pull support."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any


class OllamaError(RuntimeError):
    pass


_tasks: dict[str, dict[str, Any]] = {}
_tasks_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def base_url() -> str:
    return str(os.getenv("OLLAMA_BASE_URL") or "http://host.docker.internal:11434").rstrip("/")


def _request_json(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 5) -> Any:
    url = f"{base_url()}{path}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OllamaError("OLLAMA_BASE_URL must be an http or https URL")
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise OllamaError(f"Ollama HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OllamaError(f"Ollama is unavailable: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError) as exc:
        raise OllamaError("Ollama returned an invalid JSON response") from exc


def status() -> dict[str, Any]:
    try:
        payload = _request_json("/api/version", timeout=3)
    except OllamaError as exc:
        return {"available": False, "error": str(exc), "baseUrl": base_url()}
    return {
        "available": True,
        "version": str(payload.get("version") or "unknown") if isinstance(payload, dict) else "unknown",
        "baseUrl": base_url(),
    }


def list_models() -> list[dict[str, Any]]:
    payload = _request_json("/api/tags", timeout=5)
    raw_models = payload.get("models") if isinstance(payload, dict) else []
    models: list[dict[str, Any]] = []
    for item in raw_models or []:
        if not isinstance(item, dict):
            continue
        models.append(
            {
                "name": str(item.get("name") or item.get("model") or ""),
                "model": str(item.get("model") or item.get("name") or ""),
                "size": int(item.get("size") or 0),
                "digest": str(item.get("digest") or ""),
                "modified_at": str(item.get("modified_at") or ""),
                "details": item.get("details") if isinstance(item.get("details"), dict) else {},
            }
        )
    return models


def check_models(names: list[str]) -> dict[str, bool]:
    models = list_models()
    installed = {item["name"] for item in models}
    installed.update(item["model"] for item in models)
    return {name: name in installed for name in names}


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return dict(task)


def list_tasks() -> list[dict[str, Any]]:
    with _tasks_lock:
        return [_public_task(task) for task in _tasks.values()]


def get_task(task_id: str) -> dict[str, Any] | None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        return _public_task(task) if task else None


def _update_task(task_id: str, **updates: Any) -> None:
    with _tasks_lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.update(updates)
        if task.get("status") in {"completed", "failed"} and not task.get("endTime"):
            task["endTime"] = _now()


def _pull(task_id: str, model_name: str) -> None:
    _update_task(task_id, status="downloading", progress=0.0, message="开始下载模型")
    url = f"{base_url()}/api/pull"
    request = urllib.request.Request(
        url,
        data=json.dumps({"name": model_name, "stream": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12 * 60 * 60) as response:
            for raw_line in response:
                if not raw_line.strip():
                    continue
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    raise OllamaError(str(event["error"]))
                total = int(event.get("total") or 0)
                completed = int(event.get("completed") or 0)
                progress = completed / total * 100 if total > 0 else 0.0
                status_text = str(event.get("status") or "下载中")
                message = f"{status_text}: {progress:.1f}%" if total > 0 else status_text
                _update_task(task_id, progress=progress, message=message)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, OllamaError) as exc:
        _update_task(task_id, status="failed", progress=0.0, message=f"下载失败: {exc}")
        return
    _update_task(task_id, status="completed", progress=100.0, message="下载完成")


def download_model(model_name: str) -> tuple[dict[str, Any], str]:
    installed = {item["name"] for item in list_models()}
    if model_name in installed:
        return {"taskId": "", "modelName": model_name, "status": "completed", "progress": 100.0}, "模型已存在"
    with _tasks_lock:
        for task in _tasks.values():
            if task["modelName"] == model_name and task["status"] in {"pending", "downloading"}:
                return {
                    "taskId": task["id"],
                    "modelName": task["modelName"],
                    "status": task["status"],
                    "progress": task["progress"],
                }, "模型下载任务已存在"
    task_id = str(uuid.uuid4())
    task = {
        "id": task_id,
        "modelName": model_name,
        "status": "pending",
        "progress": 0.0,
        "message": "准备下载",
        "startTime": _now(),
    }
    with _tasks_lock:
        _tasks[task_id] = task
    threading.Thread(target=_pull, args=(task_id, model_name), daemon=True, name=f"ollama-pull-{model_name}").start()
    return {
        "taskId": task_id,
        "modelName": model_name,
        "status": "pending",
        "progress": 0.0,
    }, "模型下载任务已创建"

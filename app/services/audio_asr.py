"""Audio transcription using the knowledge base ASR configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..utils import loads
from . import model_runtime


AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".flv"}


def is_audio_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in AUDIO_EXTENSIONS


def is_video_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in VIDEO_EXTENSIONS


def _config(kb: dict[str, Any]) -> dict[str, Any]:
    value = loads(kb.get("asr_config"), {})
    return value if isinstance(value, dict) else {}


def asr_configured(kb: dict[str, Any]) -> bool:
    config = _config(kb)
    return bool(config.get("enabled")) and bool(
        str(config.get("model_id") or config.get("modelId") or "").strip()
    )


def asr_runtime(kb: dict[str, Any], knowledge: dict[str, Any]) -> dict[str, Any] | None:
    if not asr_configured(kb):
        return None
    config = _config(kb)
    model_id = str(config.get("model_id") or config.get("modelId") or "").strip()
    tenant_id = int(knowledge.get("tenant_id") or kb.get("tenant_id") or 0)
    row = model_runtime.visible_model(tenant_id, model_id)
    return model_runtime.runtime_config(row, "ASR") if row else None


def transcribe_audio(
    kb: dict[str, Any],
    knowledge: dict[str, Any],
    data: bytes,
    filename: str,
) -> str:
    runtime = asr_runtime(kb, knowledge)
    if not runtime or not model_runtime.available(runtime):
        raise RuntimeError("ASR model is not configured for audio transcription")
    language = str(_config(kb).get("language") or "").strip()
    result = model_runtime.transcribe(runtime, data, filename, language=language)
    if isinstance(result, dict):
        return str(result.get("text") or "").strip()
    return str(result or "").strip()

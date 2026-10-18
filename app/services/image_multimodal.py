"""Best-effort VLM caption and OCR for knowledge images."""

from __future__ import annotations

import base64
import hashlib
import html
import re
import unicodedata
from typing import Any

from .. import db
from ..utils import dumps, loads
from . import image_vectors, model_runtime


CAPTION_PROMPT = "Provide a brief and concise description of the main content of the image in {language}."
OCR_PROMPT = (
    "You are an OCR assistant. Transcribe only text that is visibly present in the image, "
    "preserving its original language.\n"
    "The image may be a screenshot, photograph, diagram, or document. For scattered labels, "
    "buttons, scores, and other UI text, output plain text in reading order, one item per line. "
    "Do not force this content into a table.\n"
    "Use Markdown tables only when the image contains a real table with readable cells. Never "
    "output empty table cells, invented layout, repeated filler, or descriptions of the image.\n"
    "Preserve readable numbers and symbols. Do not guess unreadable text. Stop after all visible "
    "text has been transcribed; repeat text only when it visibly appears more than once.\n"
    "Output only the extracted text, without explanations, HTML tags, or code fences. If no text "
    "is recognizable, reply only: No text content."
)
VLM_MAX_TOKENS = 5000

_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_DOC = re.compile(r"^\s*(?:<!DOCTYPE|<html|<body|<div|<p[\s>]|<table|<h[1-6][\s>])", re.IGNORECASE)
_CODE_BLOCK = re.compile(
    r"^\s*```[A-Za-z0-9_-]*\s*\r?\n(.*?)\r?\n\s*```\s*$", re.DOTALL
)
_KNOWN_EMPTY_REPLIES = {
    "无文字内容",
    "无法识别",
    "no text",
    "no text content",
    "no content",
    "empty",
    "图片中没有文字",
    "图片中没有可识别的文字",
}


class OCRValidationError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid OCR output: {reason}")
        self.reason = reason


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = loads(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _config_value(config: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = config.get(name)
        if value not in (None, ""):
            return value
    return ""


def vlm_configured(kb: dict[str, Any]) -> bool:
    config = _json_dict(kb.get("vlm_config"))
    model_id = str(_config_value(config, "model_id", "modelId") or "").strip()
    if bool(config.get("enabled")) and model_id:
        return True
    model_name = str(_config_value(config, "model_name", "modelName") or "").strip()
    base_url = str(_config_value(config, "base_url", "baseUrl") or "").strip()
    return bool(model_name and base_url)


def vlm_runtime(kb: dict[str, Any], knowledge: dict[str, Any]) -> dict[str, Any] | None:
    if not vlm_configured(kb):
        return None
    config = _json_dict(kb.get("vlm_config"))
    tenant_id = int(knowledge.get("tenant_id") or kb.get("tenant_id") or 0)
    model_id = str(_config_value(config, "model_id", "modelId") or "").strip()
    if bool(config.get("enabled")) and model_id:
        row = model_runtime.visible_model(tenant_id, model_id)
        return model_runtime.runtime_config(row, "VLLM") if row else None

    model_name = str(_config_value(config, "model_name", "modelName") or "").strip()
    base_url = str(_config_value(config, "base_url", "baseUrl") or "").strip()
    if not model_name or not base_url:
        return None
    interface_type = str(_config_value(config, "interface_type", "interfaceType") or "").strip().lower()
    row = {
        "name": model_name,
        "source": "local" if interface_type == "ollama" else "remote",
        "parameters": {
            "base_url": base_url,
            "api_key": str(_config_value(config, "api_key", "apiKey") or ""),
            "provider": "ollama" if interface_type == "ollama" else "openai-compatible",
        },
    }
    return model_runtime.runtime_config(row, "VLLM")


def caption_prompt(kb: dict[str, Any]) -> str:
    config = _json_dict(kb.get("vlm_config"))
    language = str(_config_value(config, "description_language", "descriptionLanguage") or "English").strip()
    prompt = CAPTION_PROMPT.format(language=language)
    instructions = str(_config_value(config, "custom_instructions", "customInstructions") or "").strip()
    if not instructions:
        return prompt
    return (
        f"{prompt}\n\n<image_description_business_instructions>\n{instructions}\n"
        "</image_description_business_instructions>\n"
        "Apply these business instructions only when they do not conflict with the system-owned "
        "output format, citation, safety, or factuality rules."
    )


def _chat_image(runtime: dict[str, Any], prompt: str, data_uri: str) -> str:
    response = model_runtime.chat_completion(
        runtime,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "auto"}},
                ],
            }
        ],
        max_tokens=VLM_MAX_TOKENS,
    )
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    if choice.get("finish_reason") == "length":
        raise RuntimeError(f"VLM response was truncated at {VLM_MAX_TOKENS} tokens")
    text, _message = model_runtime.text_from_chat_response(response)
    return text.strip()


def _is_known_empty_reply(text: str) -> bool:
    value = text.strip().lower().rstrip(".!?。！？")
    return value in _KNOWN_EMPTY_REPLIES


def _looks_like_html(text: str) -> bool:
    if _HTML_DOC.match(text):
        return True
    tags = _HTML_TAG.findall(text)
    if not tags:
        return False
    return sum(len(tag) for tag in tags) / max(1, len(text)) > 0.3


def _html_to_text(text: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", text)
    value = re.sub(r"(?i)</(?:p|div|li|tr|h[1-6])>", "\n", value)
    value = _HTML_TAG.sub("", value)
    return html.unescape(value)


def normalize_ocr_text(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    match = _CODE_BLOCK.match(text)
    if match:
        text = match.group(1).strip()
    plain = html.unescape(_HTML_TAG.sub("", text)).strip()
    if _HTML_TAG.search(text):
        if _is_known_empty_reply(plain):
            return plain
        if len(plain) < 10:
            return ""
    if _looks_like_html(text):
        text = _html_to_text(text).strip()
        if not text:
            return ""
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def has_ocr_repetition(text: str) -> bool:
    runes = list(" ".join(text.split()))
    if len(runes) < 512:
        return False
    coverage = [0] * (len(runes) + 1)
    for period in range(1, min(64, len(runes) // 32) + 1):
        matched = 0

        def record(end: int) -> None:
            span = matched + period
            if span >= 512 and span >= period * 32:
                coverage[end - span] += 1
                coverage[end] -= 1

        for index in range(period, len(runes)):
            if runes[index] == runes[index - period]:
                matched += 1
            else:
                record(index)
                matched = 0
        record(len(runes))
    active = 0
    repeated = 0
    for index in range(len(runes)):
        active += coverage[index]
        if active > 0:
            repeated += 1
    return repeated * 5 >= len(runes) * 4


def validate_ocr_text(raw: str) -> str:
    text = normalize_ocr_text(raw)
    if _is_known_empty_reply(text):
        return ""
    if not text:
        raise OCRValidationError("empty_content")
    readable = any(
        char.isalnum()
        or (unicodedata.category(char).startswith("S") and char not in "|~`")
        for char in text
    )
    if not readable:
        raise OCRValidationError("no_readable_content")
    if has_ocr_repetition(text):
        raise OCRValidationError("repetitive_content")
    return text


def _normalize_ocr(text: str) -> str:
    try:
        return validate_ocr_text(text)
    except OCRValidationError:
        return ""


def analyze_image(runtime: dict[str, Any], kb: dict[str, Any], data: bytes) -> dict[str, Any]:
    mime_type = image_vectors.detect_image_mime(data)
    if not mime_type:
        raise ValueError("not an image")
    data_uri = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
    result: dict[str, Any] = {
        "caption": None,
        "ocr_text": None,
        "errors": [],
        "ocr_status": "skipped",
        "ocr_error_code": "",
        "ocr_rejection_reason": "",
        "ocr_raw_chars": 0,
    }
    try:
        caption = _chat_image(runtime, caption_prompt(kb), data_uri)
        if caption:
            result["caption"] = caption
    except Exception as exc:  # noqa: BLE001 - caption is best effort
        result["errors"].append(f"caption: {exc}")
    try:
        raw_ocr = _chat_image(runtime, OCR_PROMPT, data_uri)
        result["ocr_raw_chars"] = len(raw_ocr)
        result["ocr_text"] = validate_ocr_text(raw_ocr)
        result["ocr_status"] = "succeeded" if result["ocr_text"] else "no_text"
    except OCRValidationError as exc:
        result["ocr_text"] = ""
        result["ocr_status"] = "failed"
        result["ocr_error_code"] = "OCR_INVALID_OUTPUT"
        result["ocr_rejection_reason"] = exc.reason
        result["errors"].append(f"ocr: {exc}")
    except Exception as exc:  # noqa: BLE001 - OCR is best effort
        result["ocr_text"] = ""
        result["ocr_status"] = "failed"
        result["ocr_error_code"] = (
            "OCR_TRUNCATED"
            if "truncated" in str(exc).lower()
            else "OCR_REQUEST_FAILED"
        )
        result["errors"].append(f"ocr: {exc}")
    return result


def _image_url(image: dict[str, Any]) -> str:
    return str(image.get("url") or image.get("original_url") or "").strip()


def _delete_child(
    conn: Any,
    *,
    tenant_id: int,
    knowledge_id: str,
    source_id: str,
    chunk_type: str,
    url: str,
) -> None:
    conn.execute(
        "DELETE FROM chunks WHERE tenant_id=? AND knowledge_id=? AND parent_chunk_id=? "
        "AND chunk_type=? AND deleted_at IS NULL "
        "AND COALESCE(json_extract(image_info, '$[0].url'), json_extract(image_info, '$[0].original_url'), '')=?",
        (tenant_id, knowledge_id, source_id, chunk_type, url),
    )


def _upsert_child(
    conn: Any,
    *,
    tenant_id: int,
    source: dict[str, Any],
    chunk_type: str,
    content: str,
    image: dict[str, Any],
) -> str:
    now = db.utcnow()
    image_info = dumps([image])
    url = _image_url(image)
    existing = conn.execute(
        "SELECT id FROM chunks WHERE tenant_id=? AND knowledge_id=? AND parent_chunk_id=? "
        "AND chunk_type=? AND deleted_at IS NULL "
        "AND COALESCE(json_extract(image_info, '$[0].url'), json_extract(image_info, '$[0].original_url'), '')=? "
        "ORDER BY chunk_index LIMIT 1",
        (tenant_id, source["knowledge_id"], source["id"], chunk_type, url),
    ).fetchone()
    if existing:
        chunk_id = str(existing["id"])
        conn.execute(
            "UPDATE chunks SET content=?, source_content=?, image_info=?, content_hash=?, "
            "index_status='indexed', status=2, is_enabled=1, updated_at=? WHERE id=? AND tenant_id=?",
            (
                content,
                content,
                image_info,
                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                now,
                chunk_id,
                tenant_id,
            ),
        )
        return chunk_id
    next_index = conn.execute(
        "SELECT COALESCE(MAX(chunk_index), -1) + 1 AS next_index FROM chunks "
        "WHERE tenant_id=? AND knowledge_id=?",
        (tenant_id, source["knowledge_id"]),
    ).fetchone()["next_index"]
    chunk_id = db.new_id()
    conn.execute(
        """
        INSERT INTO chunks
        (id, tenant_id, knowledge_base_id, knowledge_id, content, source_content,
         content_revision, index_status, last_editor_id, context_header,
         chunk_index, is_enabled, start_at, end_at, chunk_type, parent_chunk_id,
         image_info, status, content_hash, flags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, 'indexed', '', '', ?, 1, ?, ?, ?, ?, ?, 2, ?, 1, ?, ?)
        """,
        (
            chunk_id,
            tenant_id,
            source["knowledge_base_id"],
            source["knowledge_id"],
            content,
            content,
            int(next_index or 0),
            int(source.get("start_at") or 0),
            int(source.get("end_at") or 0),
            chunk_type,
            source["id"],
            image_info,
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
            now,
            now,
        ),
    )
    return chunk_id


def process_knowledge_images(
    tenant_id: int,
    kb: dict[str, Any],
    knowledge: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {"enabled": False, "processed": 0, "skipped": 0, "failed": 0, "items": []}
    runtime = vlm_runtime(kb, knowledge)
    if not runtime:
        return result
    if not model_runtime.available(runtime):
        result.update({"enabled": True, "skipped": 1, "items": [{"status": "skipped: VLM is not configured"}]})
        return result
    result["enabled"] = True
    rows = db.fetch_all(
        "SELECT * FROM chunks WHERE tenant_id=? AND knowledge_id=? AND chunk_type='image' "
        "AND deleted_at IS NULL ORDER BY chunk_index",
        (tenant_id, knowledge["id"]),
    )
    for source in rows:
        images = loads(source.get("image_info"), [])
        if not isinstance(images, list):
            continue
        analyzed: list[tuple[dict[str, Any], dict[str, Any]]] = []
        changed = False
        for image in images:
            if not isinstance(image, dict):
                continue
            url = _image_url(image)
            if not url:
                continue
            try:
                data = image_vectors._read_image(url, tenant_id)
                analysis = analyze_image(runtime, kb, data)
            except Exception as exc:  # noqa: BLE001 - image analysis is best effort
                result["failed"] += 1
                result["items"].append({"url": url, "status": f"failed: {exc}"})
                continue
            if analysis["caption"] is not None:
                image["caption"] = analysis["caption"]
                changed = True
            if analysis["ocr_text"] is not None:
                if analysis["ocr_text"]:
                    image["ocr_text"] = analysis["ocr_text"]
                else:
                    image.pop("ocr_text", None)
                changed = True
            analyzed.append((image, analysis))
            if analysis["errors"]:
                result["failed"] += 1
                result["items"].append(
                    {"url": url, "status": "partial: " + "; ".join(analysis["errors"])}
                )
            else:
                result["processed"] += 1
                result["items"].append({"url": url, "status": "processed"})
        if not analyzed:
            continue
        try:
            with db.transaction() as conn:
                if changed:
                    conn.execute(
                        "UPDATE chunks SET image_info=?, updated_at=? WHERE id=? AND tenant_id=?",
                        (dumps(images), db.utcnow(), source["id"], tenant_id),
                    )
                for image, analysis in analyzed:
                    url = _image_url(image)
                    if analysis["caption"] is not None:
                        _upsert_child(
                            conn,
                            tenant_id=tenant_id,
                            source=source,
                            chunk_type="image_caption",
                            content=str(image.get("caption") or ""),
                            image=image,
                        )
                    if analysis["ocr_text"] is not None:
                        if analysis["ocr_text"]:
                            _upsert_child(
                                conn,
                                tenant_id=tenant_id,
                                source=source,
                                chunk_type="image_ocr",
                                content=analysis["ocr_text"],
                                image=image,
                            )
                        else:
                            _delete_child(
                                conn,
                                tenant_id=tenant_id,
                                knowledge_id=str(source["knowledge_id"]),
                                source_id=str(source["id"]),
                                chunk_type="image_ocr",
                                url=url,
                            )
        except Exception as exc:  # noqa: BLE001 - persistence is best effort
            result["failed"] += len(analyzed)
            result["items"].append({"status": f"failed to persist image metadata: {exc}"})
    return result

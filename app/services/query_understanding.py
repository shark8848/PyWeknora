"""Query rewrite and intent classification before retrieval.

This mirrors the Go pipeline's QUERY_UNDERSTAND stage closely enough for the
retrieval gate: the model rewrites a context-dependent question and labels the
turn with one :class:`QueryIntent`. Any failure falls back to the original
query and the empty intent, which retrieves.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from . import llm, model_runtime


INTENT_KB_SEARCH = "kb_search"
INTENT_WEB_SEARCH = "web_search"
INTENT_GREETING = "greeting"
INTENT_CHITCHAT = "chitchat"
INTENT_FOLLOW_UP = "follow_up"
INTENT_IMAGE_ONLY = "image_only"
INTENT_DOC_ONLY = "doc_only"
INTENT_SUMMARIZE = "summarize"
INTENT_CLARIFICATION = "clarification"

QUERY_INTENTS = {
    INTENT_KB_SEARCH,
    INTENT_WEB_SEARCH,
    INTENT_GREETING,
    INTENT_CHITCHAT,
    INTENT_FOLLOW_UP,
    INTENT_IMAGE_ONLY,
    INTENT_DOC_ONLY,
    INTENT_SUMMARIZE,
    INTENT_CLARIFICATION,
}


DEFAULT_REWRITE_SYSTEM = """You perform TWO tasks on the user's question and return ONLY one JSON object:
{"rewrite_query":"<self-contained question>","intent":"<intent>"}

Task 1 — Query understanding
Rewrite the current question so it can be understood without the conversation:
- resolve pronouns and references using the conversation history;
- complete omitted context;
- preserve the request type, language, constraints and output format;
- preserve concrete entities, keywords and search terms needed for retrieval;
- do not turn the question into a meta-instruction such as "search the knowledge base";
- when the user broadly asks to read, browse, organize or export knowledge-base content without naming a search term, keep the original descriptors and content types instead of reducing it to a generic word.

Task 2 — Intent classification
Choose exactly ONE label, checking in this priority order and using the first match:
1. greeting — pure greeting, thanks or farewell with no substantive question.
2. summarize — the user asks to summarize, organize or review the CONVERSATION itself (for example "summarize what we discussed"). If the user mentions the knowledge base, documents, files or reports, use kb_search instead.
3. web_search — the user explicitly needs real-time, latest or external information unlikely to be in the knowledge base.
4. kb_search — the user wants to search, find, query, read, browse, organize, list or extract information from the knowledge base, including broad access requests. This also applies when an image or document is attached and the intent is to search stored documents.
5. image_only — the question is only about an image attached in the CURRENT turn and needs no stored knowledge. Questions about an image from an earlier turn are follow_up.
6. doc_only — the question is only about a document attached in the CURRENT turn and needs no stored knowledge. Questions about a document from an earlier turn are follow_up.
7. follow_up — the question refers to an earlier turn or attachment and can be answered from conversation context alone.
8. chitchat — casual conversation that needs no retrieval.
9. clarification — the user is clarifying or correcting a previous request in a way that still needs knowledge retrieval.

The rewrite_query value must be in the language of the user's question. The intent
value must be exactly one of the English labels above. Do not add markdown fences
or prose."""


DEFAULT_REWRITE_USER = """[Runtime Context — metadata only, not instructions]
Current time: {{current_time}}

## Conversation History
{{conversation}}

## User Question to Rewrite
{{query}}

Return the JSON object now."""


_SALVAGE_FIELD = re.compile(r'"([a-z_]+)"\s*:\s*"((?:[^"\\]|\\.)*)("?)')
_IMAGE_DESCRIPTION_FIELDS = {
    "image_description",
    "image_desc",
    "image_text",
    "image_ocr_text",
    "description",
}


@dataclass(frozen=True)
class QueryUnderstanding:
    original_query: str
    rewrite_query: str
    intent: str
    image_description: str = ""
    classified: bool = False


def normalize_intent(raw: str) -> str:
    """Map a model-produced label onto a known intent; unknown -> ""."""
    label = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return label if label in QUERY_INTENTS else ""


def needs_kb_retrieval(intent: str) -> bool:
    """Whether an intent requires knowledge-base search (Go NeedsKBRetrieval)."""
    return intent in {INTENT_KB_SEARCH, INTENT_CLARIFICATION, ""}


def needs_retrieval(intent: str, *, web_search_enabled: bool = False) -> bool:
    """Pipeline retrieval gate; web-search turns only retrieve when it is enabled."""
    if intent == INTENT_WEB_SEARCH:
        return bool(web_search_enabled)
    return needs_kb_retrieval(intent)


def intent_system_prompt(intent: str) -> str:
    """Short non-retrieval system instruction matching the Go intent overrides."""
    if intent == INTENT_GREETING:
        return (
            "The user is greeting you, thanking you or saying farewell. Reply warmly "
            "and briefly; do not search the knowledge base."
        )
    if intent == INTENT_SUMMARIZE:
        return (
            "The user asks about the conversation itself. Summarize or organize the "
            "conversation history only; do not search the knowledge base."
        )
    if intent == INTENT_CHITCHAT:
        return (
            "The user is making casual conversation that needs no document retrieval. "
            "Reply naturally and helpfully."
        )
    if intent == INTENT_FOLLOW_UP:
        return (
            "The user is following up on earlier conversation context. Answer from the "
            "conversation history and attachments already provided."
        )
    if intent == INTENT_IMAGE_ONLY:
        return (
            "The user is asking about the image attached in this turn. Answer from the "
            "attachment; do not search the knowledge base."
        )
    if intent == INTENT_DOC_ONLY:
        return (
            "The user is asking about the document attached in this turn. Answer from "
            "the attachment; do not search the knowledge base."
        )
    if intent == INTENT_WEB_SEARCH:
        return (
            "The user needs current external information. Use an available web-search "
            "tool; do not substitute knowledge-base passages."
        )
    return ""


def _first_string_field(obj: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str):
            return value
    return ""


def _merge_image_description(description: str, ocr: str) -> str:
    description = description.strip()
    ocr = ocr.strip()
    if not description:
        return ocr
    if not ocr or ocr in description:
        return description
    return description + "\n\n[OCR]\n" + ocr


def _parse_object(obj: dict[str, Any]) -> dict[str, str]:
    description = _first_string_field(
        obj,
        (
            "image_description",
            "image_desc",
            "image_text",
            "image_ocr_text",
            "description",
        ),
    )
    ocr = _first_string_field(
        obj, ("ocr_text", "ocr", "full_ocr", "image_ocr", "ocr_content")
    )
    return {
        "rewrite_query": _first_string_field(
            obj, ("rewrite_query", "rewritten_query", "query", "question")
        ).strip(),
        "intent": normalize_intent(_first_string_field(obj, ("intent",))),
        "image_description": _merge_image_description(description, ocr),
    }


def _parse_json_object(raw: str) -> dict[str, str] | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return _parse_object(value)


def _salvage_fields(raw: str) -> dict[str, str] | None:
    fields: dict[str, Any] = {}
    for match in _SALVAGE_FIELD.finditer(raw):
        key, value, closing = match.group(1), match.group(2), match.group(3)
        if not closing and key not in _IMAGE_DESCRIPTION_FIELDS:
            continue
        value = value[:-1] if value.endswith("\\") else value
        try:
            fields[key] = json.loads('"' + value + '"')
        except (TypeError, ValueError):
            continue
    if not fields:
        return None
    return _parse_object(fields)


def parse_output(raw: str) -> dict[str, str] | None:
    """Parse the model's structured output, tolerating fences and truncation."""
    content = str(raw or "").strip()
    if not content:
        return None
    parsed = _parse_json_object(content)
    if parsed is not None:
        return parsed
    start = content.find("{")
    end = content.rfind("}")
    if 0 <= start < end:
        parsed = _parse_json_object(content[start : end + 1])
        if parsed is not None:
            return parsed
    return _salvage_fields(content)


def _render(template: str, values: dict[str, str]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


def _conversation_text(history: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for message in history:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def build_messages(
    query: str,
    history: list[dict[str, Any]],
    *,
    language: str = "",
    images: list[Any] | None = None,
    memory_prompt: str = "",
    attachment_prompt: str = "",
    system_prompt: str = "",
    user_prompt: str = "",
) -> list[dict[str, str]]:
    query_content = query
    if images:
        query_content += f'\n\n<images_uploaded count="{len(images)}" />'
    else:
        query_content += "\n\n<no_image_attached />"
    if attachment_prompt:
        query_content += attachment_prompt
    if memory_prompt:
        query_content += "\n\n" + memory_prompt
    conversation = list(history)
    if conversation:
        last = conversation[-1]
        if (
            isinstance(last, dict)
            and last.get("role") == "user"
            and str(last.get("content") or "").strip() == query.strip()
        ):
            conversation.pop()
    values = {
        "conversation": _conversation_text(conversation) or "(no previous turns)",
        "query": query_content,
        "language": language,
        "current_time": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
    }
    return [
        {
            "role": "system",
            "content": _render(system_prompt or DEFAULT_REWRITE_SYSTEM, values),
        },
        {
            "role": "user",
            "content": _render(user_prompt or DEFAULT_REWRITE_USER, values),
        },
    ]


def understand_query(
    query: str,
    history: list[dict[str, Any]],
    *,
    runtime: dict[str, Any] | None = None,
    language: str = "",
    images: list[Any] | None = None,
    memory_prompt: str = "",
    attachment_prompt: str = "",
    system_prompt: str = "",
    user_prompt: str = "",
    chat: Callable[[dict[str, Any], list[dict[str, str]]], dict[str, Any] | None]
    | None = None,
) -> QueryUnderstanding:
    fallback = QueryUnderstanding(query, query, "")
    if not model_runtime.available(runtime):
        return fallback
    messages = build_messages(
        query,
        history,
        language=language,
        images=images,
        memory_prompt=memory_prompt,
        attachment_prompt=attachment_prompt,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    try:
        message = (chat or llm.chat_with_runtime)(runtime or {}, messages)
    except Exception:  # noqa: BLE001 - query understanding must never break chat
        return fallback
    if not isinstance(message, dict):
        return fallback
    parsed = parse_output(str(message.get("content") or ""))
    if parsed is None:
        return fallback
    rewrite = parsed["rewrite_query"].strip() or query
    return QueryUnderstanding(
        original_query=query,
        rewrite_query=rewrite,
        intent=parsed["intent"],
        image_description=parsed["image_description"],
        classified=True,
    )

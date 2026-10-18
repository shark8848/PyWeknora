import json

from app.services import document_summary
from app.routers.knowledge import _summary_enabled, _summary_source_text


def test_parse_summary_output_keeps_plain_text_and_rejects_bad_json() -> None:
    assert document_summary.parse_summary_output(
        '```json\n{"summary":"Model summary","gist":"Gist"}\n```'
    ) == "Model summary"
    assert document_summary.parse_summary_output('{"gist":"Only gist"}') == "Only gist"
    assert document_summary.parse_summary_output("Legacy plain summary") == "Legacy plain summary"
    assert document_summary.parse_summary_output('{"summary":"cut off') is None
    assert document_summary.parse_summary_output('{"topics":[]}') is None
    assert document_summary.parse_summary_output("\ufeffPlain BOM summary") == "Plain BOM summary"


def test_generate_summary_rejects_truncated_json(monkeypatch) -> None:
    monkeypatch.setattr(document_summary.model_runtime, "available", lambda _runtime: True)
    monkeypatch.setattr(
        document_summary.model_runtime,
        "chat_completion",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"content": '{"summary":"cut off'}, "finish_reason": "length"}]
        },
    )
    assert document_summary.generate_summary({}, "source text") is None


def test_generate_summary_parses_structured_output(monkeypatch) -> None:
    calls: list[list[dict[str, str]]] = []

    def complete(_runtime, messages, **_kwargs):
        calls.append(messages)
        return {
            "choices": [
                {
                    "message": {"content": json.dumps({"summary": "Model summary"})},
                    "finish_reason": "stop",
                }
            ]
        }

    monkeypatch.setattr(document_summary.model_runtime, "available", lambda _runtime: True)
    monkeypatch.setattr(document_summary.model_runtime, "chat_completion", complete)

    assert document_summary.generate_summary({}, "source text", language="Chinese") == "Model summary"
    assert "Chinese" in calls[0][0]["content"]
    assert calls[0][1] == {"role": "user", "content": "source text"}


def test_summary_enabled_defaults_true_and_honors_process_override() -> None:
    assert _summary_enabled({}, {}) is True
    assert _summary_enabled({}, {"process_overrides": {"summary_enabled": False}}) is False
    assert _summary_enabled({}, {"process_overrides": {"summary_enabled": True}}) is True


def test_summary_source_rejects_image_only_content() -> None:
    assert _summary_source_text("Release notes explain the migration checks.") is True
    assert _summary_source_text("![scan](images/page-1.png)") is False
    assert _summary_source_text('<image url="page.png"><image_original>![scan](page.png)</image_original></image>') is False

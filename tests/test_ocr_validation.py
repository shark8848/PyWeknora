from __future__ import annotations

import io

import pytest
from PIL import Image

from app.services import image_multimodal


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 34, 56)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize(
    "raw",
    [
        "No text content.",
        "无文字内容",
        "```\nNo text content.\n```",
        "<p>No text content.</p>",
        "<html><body>No text content.</body></html>",
        "<p><strong>No text content.</strong></p>",
        "```html\n<p>No text content.</p>\n```",
        "<p>无文字内容</p>",
        "<p>empty</p>",
        "<p>no text</p>",
        "<p>No text content&#46;</p>",
    ],
)
def test_ocr_no_text_replies_are_preserved_as_empty(raw: str) -> None:
    assert image_multimodal.validate_ocr_text(raw) == ""


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("", "empty_content"),
        ("<html><body><div><img/></div></body></html>", "empty_content"),
        ("| --- | --- |\n| | |", "no_readable_content"),
        ("传球按钮 " * 200, "repetitive_content"),
        ("传球\n" + "|  " * 2500, "repetitive_content"),
        ("|  " * 1250 + "传球\n" + "|  " * 1250, "repetitive_content"),
        ("| " * 600 + "传球\n" + "取消施法 " * 240, "repetitive_content"),
        ("开始\n锁定目标\n" * 100, "repetitive_content"),
    ],
)
def test_ocr_degenerate_output_is_rejected(raw: str, reason: str) -> None:
    with pytest.raises(image_multimodal.OCRValidationError) as excinfo:
        image_multimodal.validate_ocr_text(raw)
    assert excinfo.value.reason == reason


def test_ocr_preserves_real_content_and_normalizes_html_tables() -> None:
    for raw in (
        "A",
        "3",
        "传球",
        "A\nB\nL1\nL2\n传球\n传球",
        "0:0\n58.29\n取消施法",
        "±",
        "$x^2 + y^2 = z^2$",
        "同一个按钮\n" * 5,
    ):
        assert image_multimodal.validate_ocr_text(raw) == raw.strip()

    table = "<table><tr><th>name</th><th>score</th></tr><tr><td>A</td><td>1</td></tr></table>"
    text = image_multimodal.validate_ocr_text(table)
    assert "name" in text and "score" in text and "A" in text
    assert "<table>" not in text

    long_valid = "| 项目 | 数值 |\n| --- | --- |\n" + "".join(
        f"| 项目{index} | {index} |\n" for index in range(100)
    ) + "X" * 600
    assert image_multimodal.validate_ocr_text(long_valid) == long_valid


def test_analyze_image_clears_ocr_on_validation_failure(monkeypatch) -> None:
    responses = iter(["A useful caption", "传球按钮 " * 200])

    def fake_chat(_runtime, _prompt, _data_uri):
        return next(responses)

    monkeypatch.setattr(image_multimodal, "_chat_image", fake_chat)
    analysis = image_multimodal.analyze_image(
        {"base_url": "https://vlm.test/v1"}, {}, _png()
    )

    assert analysis["caption"] == "A useful caption"
    assert analysis["ocr_text"] == ""
    assert analysis["ocr_status"] == "failed"
    assert analysis["ocr_error_code"] == "OCR_INVALID_OUTPUT"
    assert analysis["ocr_rejection_reason"] == "repetitive_content"
    assert analysis["errors"] and "repetitive_content" in analysis["errors"][0]


def test_analyze_image_marks_no_text_and_request_failure(monkeypatch) -> None:
    responses = iter(["caption", "<p>No text content.</p>"])
    monkeypatch.setattr(image_multimodal, "_chat_image", lambda *_args: next(responses))
    no_text = image_multimodal.analyze_image(
        {"base_url": "https://vlm.test/v1"}, {}, _png()
    )
    assert no_text["ocr_text"] == ""
    assert no_text["ocr_status"] == "no_text"
    assert no_text["errors"] == []

    def fail(*_args):
        raise RuntimeError("vlm unavailable")

    monkeypatch.setattr(image_multimodal, "_chat_image", fail)
    failed = image_multimodal.analyze_image(
        {"base_url": "https://vlm.test/v1"}, {}, _png()
    )
    assert failed["ocr_text"] == ""
    assert failed["ocr_status"] == "failed"
    assert failed["ocr_error_code"] == "OCR_REQUEST_FAILED"
    assert failed["errors"]

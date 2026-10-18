from __future__ import annotations

import base64
import io
import json
import os
import re

import pytest
from PIL import Image

from app.errors import AppError
from app.services import image_prep, model_runtime


def _png(size: tuple[int, int] = (8, 8), mode: str = "RGB") -> bytes:
    output = io.BytesIO()
    if mode == "RGBA":
        raw = os.urandom(size[0] * size[1] * 4)
        Image.frombytes("RGBA", size, raw).save(output, format="PNG")
    else:
        Image.new(mode, size, (12, 34, 56)).save(output, format="PNG")
    return output.getvalue()


def _noise_png(size: int = 256) -> bytes:
    output = io.BytesIO()
    raw = os.urandom(size * size * 3)
    Image.frombytes("RGB", (size, size), raw).save(output, format="PNG")
    return output.getvalue()


def test_prepare_passthrough_when_format_and_size_are_accepted() -> None:
    data = _png()
    prepared, mime = image_prep.prepare(data)
    assert prepared == data
    assert mime == "image/png"


def test_prepare_converts_and_scales_to_jpeg_limit() -> None:
    data = _noise_png()
    prepared, mime = image_prep.prepare(data, max_bytes=20_000, mime_types=["image/jpeg"])
    assert mime == "image/jpeg"
    assert prepared.startswith(b"\xff\xd8\xff")
    assert len(prepared) <= 20_000
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.format == "JPEG"
        assert image.size[0] < 256 and image.size[1] < 256


def test_prepare_flattens_transparency_onto_white() -> None:
    source = io.BytesIO()
    Image.new("RGBA", (64, 64), (0, 0, 0, 0)).save(source, format="PNG")
    prepared, mime = image_prep.prepare(source.getvalue(), mime_types=["image/jpeg"])
    assert mime == "image/jpeg"
    with Image.open(io.BytesIO(prepared)) as image:
        red, green, blue = image.convert("RGB").getpixel((32, 32))
    assert red >= 240 and green >= 240 and blue >= 240


def test_prepare_falls_back_to_png() -> None:
    source = io.BytesIO()
    Image.new("RGB", (16, 16), (9, 8, 7)).save(source, format="JPEG")
    prepared, mime = image_prep.prepare(source.getvalue(), mime_types=["image/png"])
    assert mime == "image/png"
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.format == "PNG"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "image is empty"),
        (b"%PDF-1.7", "not an image (application/pdf)"),
        (b"plain text", "not an image (text/plain)"),
    ],
)
def test_prepare_rejects_non_images(data: bytes, message: str) -> None:
    with pytest.raises(AppError, match=re.escape(message)):
        image_prep.prepare(data)


def test_prepare_rejects_formats_it_cannot_produce() -> None:
    with pytest.raises(AppError, match="takes none of the formats"):
        image_prep.prepare(_png(), mime_types=["image/webp"])


def test_prepare_reports_minimum_side_limit() -> None:
    with pytest.raises(AppError, match="does not fit 1 bytes even scaled down"):
        image_prep.prepare(_png((64, 64)), max_bytes=1, mime_types=["image/jpeg"])


def test_image_embedding_sends_prepared_image(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        calls.append(json.loads(body.decode()))
        return 200, {}, json.dumps({"data": [{"embedding": [0.1, 0.2]}]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)
    runtime = {
        "provider": "jina",
        "name": "jina-embeddings-v5-omni-small",
        "base_url": "https://jina.test/v1",
        "headers": {},
        "spec": {"input": ["text", "image"], "max_image_bytes": 20_000},
    }
    vectors = model_runtime.image_embedding(runtime, _noise_png())
    assert vectors == [[0.1, 0.2]]
    data_uri = calls[0]["input"][0]["image"]
    assert data_uri.startswith("data:image/jpeg;base64,")
    prepared = base64.b64decode(data_uri.split(",", 1)[1])
    assert len(prepared) <= 20_000
    assert prepared.startswith(b"\xff\xd8\xff")


def test_image_rerank_sends_prepared_image(monkeypatch) -> None:
    calls: list[dict] = []

    def fake_http(method, url, *, headers=None, body=None, timeout=120):
        calls.append(json.loads(body.decode()))
        return 200, {}, json.dumps({"results": [{"index": 0, "relevance_score": 0.9}]}).encode()

    monkeypatch.setattr(model_runtime, "_http", fake_http)
    runtime = {
        "provider": "jina",
        "name": "jina-reranker-m0",
        "base_url": "https://jina.test/v1",
        "headers": {},
        "spec": {"input": ["text", "image"], "max_image_bytes": 20_000},
    }
    results = model_runtime.image_rerank(runtime, "chart", _noise_png())
    assert results[0]["relevance_score"] == 0.9
    data_uri = calls[0]["documents"][0]["image"]
    assert data_uri.startswith("data:image/jpeg;base64,")
    assert len(base64.b64decode(data_uri.split(",", 1)[1])) <= 20_000

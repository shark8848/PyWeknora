"""Prepare stored image bytes for embedding and rerank endpoints."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from ..errors import validation


JPEG_QUALITY = 85
SCALE_STEP = 0.75
MIN_SIDE = 64


def detect_image_mime(data: bytes) -> str:
    if not data:
        raise validation("image is empty")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if data.startswith(b"%PDF"):
        raise validation("not an image (application/pdf)")
    try:
        text = data[:512].decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    if text and all(char.isprintable() or char in "\r\n\t" for char in text):
        raise validation("not an image (text/plain)")
    raise validation("not an image (application/octet-stream)")


def _accepts(mime_types: list[str], mime: str) -> bool:
    return not mime_types or mime in mime_types


def _fits(max_bytes: int, size: int) -> bool:
    return max_bytes <= 0 or size <= max_bytes


def _encode(image: Image.Image, mime: str) -> bytes:
    output = BytesIO()
    try:
        if mime == "image/jpeg":
            if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGB", rgba.size, (255, 255, 255))
                background.paste(rgba, mask=rgba.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
            image.save(output, format="JPEG", quality=JPEG_QUALITY)
        else:
            image.save(output, format="PNG")
    except Exception as exc:  # noqa: BLE001 - surface the encoding failure
        raise validation(f"encode {mime}: {exc}") from exc
    return output.getvalue()


def prepare(
    data: bytes,
    *,
    max_bytes: int = 0,
    mime_types: list[str] | None = None,
) -> tuple[bytes, str]:
    mime = detect_image_mime(data)
    accepted = list(mime_types or [])
    if _accepts(accepted, mime) and _fits(max_bytes, len(data)):
        return data, mime
    target = "image/jpeg"
    if not _accepts(accepted, target):
        target = "image/png"
        if not _accepts(accepted, target):
            raise validation(f"the model takes none of the formats this can produce: {accepted}")
    try:
        image = Image.open(BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001 - surface the decoding failure
        raise validation(f"decode {mime}: {exc}") from exc
    while True:
        encoded = _encode(image, target)
        if _fits(max_bytes, len(encoded)):
            return encoded, target
        width, height = image.size
        next_width, next_height = int(width * SCALE_STEP), int(height * SCALE_STEP)
        if next_width < MIN_SIDE or next_height < MIN_SIDE:
            raise validation(f"image does not fit {max_bytes} bytes even scaled down")
        image = image.resize((next_width, next_height), Image.Resampling.BILINEAR)

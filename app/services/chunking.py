"""Document text extraction and deterministic chunking."""

from __future__ import annotations

import csv
import html
import io
import json
import posixpath
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from . import image_prep


SUPPORTED_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".xml",
    ".log", ".yaml", ".yml", ".docx", ".xlsx", ".pptx", ".pdf", ".epub",
}

EMBEDDED_IMAGE_SCHEME = "embedded-image://"


def decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _xml_text(data: bytes) -> str:
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return ""
    parts: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] in {"t", "instrText"} and node.text:
            parts.append(node.text)
    return "\n".join(parts)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _relationships(archive: zipfile.ZipFile, name: str, base: str) -> dict[str, str]:
    if name not in archive.namelist():
        return {}
    try:
        root = ElementTree.fromstring(archive.read(name))
    except ElementTree.ParseError:
        return {}
    output: dict[str, str] = {}
    for node in root.iter():
        if _local_name(node.tag) != "Relationship" or node.attrib.get("TargetMode") == "External":
            continue
        relationship_id = str(node.attrib.get("Id") or "")
        target = str(node.attrib.get("Target") or "").strip().lstrip("/")
        if not relationship_id or not target:
            continue
        if target.startswith(("word/", "ppt/", "xl/")):
            output[relationship_id] = posixpath.normpath(target)
        else:
            output[relationship_id] = posixpath.normpath(posixpath.join(base, target))
    return output


def _markdown_caption(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value or "").replace("]", " ").replace("(", " ").replace(")", " ").strip()


def _embedded_marker(archive: zipfile.ZipFile, path: str, caption: str, assets: list[dict[str, Any]], indexes: dict[str, int]) -> str:
    if not path or path not in archive.namelist():
        return ""
    index = indexes.get(path)
    if index is None:
        data = archive.read(path)
        try:
            mime_type = image_prep.detect_image_mime(data)
        except Exception:  # noqa: BLE001 - non-image office assets are ignored
            return ""
        index = len(assets)
        indexes[path] = index
        assets.append(
            {
                "marker": EMBEDDED_IMAGE_SCHEME + str(index),
                "name": Path(path).name,
                "mime_type": mime_type,
                "data": data,
                "caption": _markdown_caption(caption) or Path(path).stem,
            }
        )
    label = _markdown_caption(caption) or Path(path).stem
    return f"![{label}]({EMBEDDED_IMAGE_SCHEME}{index})"


def _docx_document(archive: zipfile.ZipFile) -> tuple[str, list[dict[str, Any]]]:
    if "word/document.xml" not in archive.namelist():
        return "", []
    try:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    except ElementTree.ParseError:
        return "", []
    relationships = _relationships(archive, "word/_rels/document.xml.rels", "word")
    assets: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    paragraphs: list[str] = []
    for paragraph in root.iter():
        if _local_name(paragraph.tag) != "p":
            continue
        captions = []
        for node in paragraph.iter():
            if _local_name(node.tag) == "docPr":
                caption = str(node.attrib.get("descr") or node.attrib.get("title") or node.attrib.get("name") or "")
                if caption:
                    captions.append(caption)
        caption = captions[0] if captions else ""
        parts: list[str] = []
        for node in paragraph.iter():
            local = _local_name(node.tag)
            if local in {"t", "instrText"} and node.text:
                parts.append(node.text)
            elif local == "blip":
                relationship_id = str(node.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed") or "")
                marker = _embedded_marker(archive, relationships.get(relationship_id, ""), caption, assets, indexes)
                if marker:
                    parts.append(marker)
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs), assets


def _pptx_slide_text(
    archive: zipfile.ZipFile,
    node: ElementTree.Element,
    relationships: dict[str, str],
    assets: list[dict[str, Any]],
    indexes: dict[str, int],
) -> str:
    local = _local_name(node.tag)
    if local == "pic":
        caption = ""
        relationship_id = ""
        for child in node.iter():
            child_local = _local_name(child.tag)
            if child_local == "cNvPr" and not caption:
                caption = str(child.attrib.get("descr") or child.attrib.get("title") or child.attrib.get("name") or "")
            elif child_local == "blip" and not relationship_id:
                relationship_id = str(child.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed") or "")
        return _embedded_marker(
            archive,
            relationships.get(relationship_id, ""),
            caption,
            assets,
            indexes,
        )
    if local in {"t", "instrText"}:
        return node.text or ""
    return "".join(_pptx_slide_text(archive, child, relationships, assets, indexes) for child in node)


def _pptx_document(archive: zipfile.ZipFile) -> tuple[str, list[dict[str, Any]]]:
    names = archive.namelist()
    slides = sorted(
        (name for name in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=lambda name: int(re.search(r"(\d+)", name).group(1)),
    )
    assets: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    parts: list[str] = []
    for slide in slides:
        try:
            root = ElementTree.fromstring(archive.read(slide))
        except ElementTree.ParseError:
            continue
        relationships = _relationships(
            archive,
            f"ppt/slides/_rels/{Path(slide).name}.rels",
            "ppt/slides",
        )
        slide_parts = [
            _pptx_slide_text(archive, child, relationships, assets, indexes)
            for child in root
        ]
        text = "".join(slide_parts).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts), assets


def _extract_zip_office(path: str, data: bytes) -> tuple[str, list[dict[str, Any]]]:
    suffix = Path(path).suffix.lower()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            if suffix == ".docx":
                return _docx_document(archive)
            if suffix == ".pptx":
                return _pptx_document(archive)
            if suffix == ".xlsx":
                if "xl/sharedStrings.xml" in names:
                    shared = _xml_text(archive.read("xl/sharedStrings.xml"))
                else:
                    shared = ""
                sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
                body = "\n".join(_xml_text(archive.read(name)) for name in sheets)
                return "\n".join(part for part in (shared, body) if part), []
    except (zipfile.BadZipFile, KeyError):
        return "", []
    return "", []


_EPUB_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}
_EPUB_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
}


def _epub_normalize(path: str) -> str:
    normalized = posixpath.normpath(str(path or "").replace("\\", "/"))
    return "" if normalized == "." else normalized.lstrip("/")


def _epub_uri_path(value: str) -> str:
    return str(value or "").strip().split("#", 1)[0].split("?", 1)[0].replace("\\", "/")


def _epub_join(base: str, path: str) -> str:
    path = str(path or "").replace("\\", "/")
    if path.startswith("/"):
        return _epub_normalize(path)
    return _epub_normalize(posixpath.join(base, path))


def _epub_find_path(archive: zipfile.ZipFile, base: str, href: str) -> str:
    raw = _epub_uri_path(href)
    decoded = unquote(raw).replace("\\", "/")
    names = set(archive.namelist())
    for value in (decoded, raw):
        for candidate in (_epub_join(base, value), _epub_normalize(value)):
            if candidate in names:
                return candidate
    return ""


def _epub_package(archive: zipfile.ZipFile) -> tuple[list[str], list[str]]:
    names = set(archive.namelist())
    package_path = ""
    if "META-INF/container.xml" in names:
        try:
            container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
            for node in container.iter():
                if _local_name(node.tag) == "rootfile":
                    package_path = _epub_find_path(
                        archive, "", str(node.attrib.get("full-path") or "")
                    )
                    if package_path:
                        break
        except ElementTree.ParseError:
            package_path = ""
    if not package_path:
        package_path = next((name for name in sorted(names) if name.lower().endswith(".opf")), "")
    if not package_path:
        return [], sorted(
            name for name in names if name.lower().endswith((".xhtml", ".html", ".htm"))
        )

    try:
        package = ElementTree.fromstring(archive.read(package_path))
    except (ElementTree.ParseError, KeyError):
        return [], []
    base = posixpath.dirname(package_path)
    items: dict[str, tuple[str, str]] = {}
    images: list[str] = []
    for node in package.iter():
        if _local_name(node.tag) != "item":
            continue
        item_id = str(node.attrib.get("id") or "")
        path = _epub_find_path(archive, base, str(node.attrib.get("href") or ""))
        media_type = str(node.attrib.get("media-type") or "").lower()
        if item_id and path:
            items[item_id] = (path, media_type)
        if path and (
            media_type.startswith("image/")
            or Path(path).suffix.lower() in _EPUB_IMAGE_SUFFIXES
        ):
            images.append(path)

    documents = [
        path for path, media_type in items.values()
        if media_type in {"application/xhtml+xml", "text/html"}
        or path.lower().endswith((".xhtml", ".html", ".htm"))
    ]
    chapters: list[str] = []
    for node in package.iter():
        if _local_name(node.tag) != "itemref":
            continue
        item = items.get(str(node.attrib.get("idref") or ""))
        if not item:
            continue
        path, media_type = item
        if media_type in {"application/xhtml+xml", "text/html"} or path.lower().endswith(
            (".xhtml", ".html", ".htm")
        ):
            chapters.append(path)
    if not chapters:
        chapters = documents
    else:
        chapters.extend(path for path in documents if path not in chapters)
    ordered_images = list(dict.fromkeys(images))
    ordered_images.extend(
        name for name in sorted(names)
        if name not in ordered_images and Path(name).suffix.lower() in _EPUB_IMAGE_SUFFIXES
    )
    return list(dict.fromkeys(chapters)), ordered_images


def _epub_image_marker(
    archive: zipfile.ZipFile,
    aliases: dict[str, str],
    assets: list[dict[str, Any]],
    indexes: dict[str, int],
    base: str,
    source: str,
    caption: str,
) -> str:
    raw = _epub_uri_path(source)
    if not raw or raw.startswith(("data:", "http://", "https://", "//")):
        return ""
    candidates: list[str] = []
    for value in (unquote(raw).replace("\\", "/"), raw):
        for candidate in (_epub_join(base, value), _epub_normalize(value)):
            if candidate not in candidates:
                candidates.append(candidate)
    basename = posixpath.basename(_epub_normalize(unquote(raw).replace("\\", "/")))
    if basename and basename not in candidates:
        candidates.append(basename)
    for candidate in candidates:
        path = aliases.get(candidate)
        if path:
            return _embedded_marker(archive, path, caption, assets, indexes)
    return ""


def _epub_html_text(
    archive: zipfile.ZipFile,
    aliases: dict[str, str],
    assets: list[dict[str, Any]],
    indexes: dict[str, int],
    chapter: str,
) -> str:
    try:
        root = ElementTree.fromstring(archive.read(chapter))
    except (ElementTree.ParseError, KeyError):
        return ""
    pieces: list[str] = []

    def walk(node: ElementTree.Element) -> None:
        local = _local_name(node.tag).lower()
        if local in {"head", "script", "style", "template"}:
            return
        block = local in _EPUB_BLOCK_TAGS
        if block and pieces and not pieces[-1].endswith("\n"):
            pieces.append("\n")
        if local == "img":
            source = str(node.attrib.get("src") or node.attrib.get("href") or "")
            caption = str(node.attrib.get("alt") or node.attrib.get("title") or "")
            marker = _epub_image_marker(
                archive, aliases, assets, indexes,
                posixpath.dirname(chapter), source, caption,
            )
            if marker:
                pieces.append(marker)
        elif local == "image":
            source = ""
            for key, value in node.attrib.items():
                if _local_name(key).lower() == "href":
                    source = str(value)
                    break
            marker = _epub_image_marker(
                archive, aliases, assets, indexes,
                posixpath.dirname(chapter), source,
                str(node.attrib.get("title") or ""),
            )
            if marker:
                pieces.append(marker)
        elif node.text:
            pieces.append(node.text)
        for child in node:
            walk(child)
        if block:
            pieces.append("\n")
        if node.tail:
            pieces.append(node.tail)

    walk(root)
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in "".join(pieces).splitlines())
    return "\n".join(line for line in lines if line)


def _extract_epub(data: bytes) -> tuple[str, list[dict[str, Any]]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return "", []
    with archive:
        chapters, image_paths = _epub_package(archive)
        assets: list[dict[str, Any]] = []
        indexes: dict[str, int] = {}
        aliases: dict[str, str] = {}
        for path in image_paths:
            marker = _embedded_marker(archive, path, Path(path).stem, assets, indexes)
            if not marker:
                continue
            normalized = _epub_normalize(path)
            for alias in (path, normalized):
                if alias:
                    aliases[alias] = path
            basename = posixpath.basename(normalized)
            if basename:
                aliases.setdefault(basename, path)
        parts = [
            text for text in (
                _epub_html_text(archive, aliases, assets, indexes, chapter)
                for chapter in chapters
            )
            if text
        ]
        return "\n\n".join(parts), assets


def _pdf_image_asset(
    page_number: int,
    image_number: int,
    name: str,
    data: bytes,
) -> dict[str, Any] | None:
    try:
        mime_type = image_prep.detect_image_mime(data)
    except Exception:  # noqa: BLE001 - unsupported PDF image encodings are ignored
        return None
    asset_name = Path(name).name or f"page-{page_number}-image-{image_number}.bin"
    return {
        "name": asset_name,
        "mime_type": mime_type,
        "data": data,
        "caption": f"Page {page_number} image {image_number}",
    }


def _extract_pdf(data: bytes) -> tuple[str, list[dict[str, Any]]]:
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(data))
        pages: list[str] = []
        assets: list[dict[str, Any]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - one bad page must not drop the rest
                text = ""
            markers: list[str] = []
            try:
                images = list(page.images)
            except Exception:  # noqa: BLE001 - image extraction is best effort
                images = []
            for image_number, image in enumerate(images, start=1):
                try:
                    image_data = bytes(getattr(image, "data", b"") or b"")
                except Exception:  # noqa: BLE001 - skip an unreadable image
                    continue
                if not image_data:
                    continue
                asset = _pdf_image_asset(
                    page_number,
                    image_number,
                    str(getattr(image, "name", "") or ""),
                    image_data,
                )
                if not asset:
                    continue
                index = len(assets)
                asset["marker"] = EMBEDDED_IMAGE_SCHEME + str(index)
                assets.append(asset)
                markers.append(f"![{asset['caption']}]({asset['marker']})")
            parts = [text.strip(), *markers]
            page_text = "\n".join(part for part in parts if part)
            if page_text:
                pages.append(page_text)
        return "\n\n".join(pages), assets
    except Exception:  # noqa: BLE001 - optional parser
        pass
    try:
        import fitz  # type: ignore

        with fitz.open(stream=data, filetype="pdf") as document:
            return "\n\n".join(page.get_text() for page in document), []
    except Exception:  # noqa: BLE001 - optional parser
        return "", []


def _plain_text(path: str, text: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in {".html", ".htm", ".xml"}:
        text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return html.unescape(re.sub(r"[ \t]+", " ", text))
    if suffix == ".json":
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            return text
    if suffix == ".csv":
        try:
            rows = list(csv.reader(io.StringIO(text)))
            return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
        except csv.Error:
            return text
    return text


def strip_embedded_markers(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\(" + re.escape(EMBEDDED_IMAGE_SCHEME) + r"\d+\)", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_document(filename: str, data: bytes) -> tuple[str, list[dict[str, Any]]]:
    suffix = Path(filename).suffix.lower()
    if suffix in {".docx", ".xlsx", ".pptx"}:
        text, assets = _extract_zip_office(filename, data)
        if text.strip() or assets:
            return text, assets
    if suffix == ".epub":
        text, assets = _extract_epub(data)
        if text.strip() or assets:
            return text, assets
        return "", []
    if suffix == ".pdf":
        text, assets = _extract_pdf(data)
        if text.strip() or assets:
            return text, assets
        return "", []
    return _plain_text(filename, decode_bytes(data)), []


def extract_text(filename: str, data: bytes) -> str:
    text, _assets = extract_document(filename, data)
    return strip_embedded_markers(text)


def normalise_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\u0000", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def split_text(text: str, chunk_size: int = 512, overlap: int = 50) -> list[str]:
    """Split on paragraph boundaries, then hard-wrap oversized paragraphs."""
    text = normalise_text(text)
    if not text:
        return []
    size = max(100, int(chunk_size))
    overlap = max(0, min(int(overlap), size // 2))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > size:
            if current:
                chunks.append(current.strip())
                current = ""
            start = 0
            while start < len(paragraph):
                end = min(len(paragraph), start + size)
                if end < len(paragraph):
                    boundary = max(paragraph.rfind("。", start, end), paragraph.rfind(".", start, end))
                    if boundary > start + size // 2:
                        end = boundary + 1
                chunks.append(paragraph[start:end].strip())
                if end >= len(paragraph):
                    break
                start = max(start + 1, end - overlap)
            continue
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= size:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = paragraph
    if current:
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def chunk_with_offsets(text: str, chunk_size: int = 512, overlap: int = 50) -> list[dict[str, Any]]:
    chunks = split_text(text, chunk_size, overlap)
    cursor = 0
    output: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        start = text.find(chunk, cursor)
        if start < 0:
            start = text.find(chunk)
        if start < 0:
            start = cursor
        end = start + len(chunk)
        cursor = max(start + 1, end - overlap)
        output.append({"content": chunk, "index": index, "start_at": start, "end_at": end})
    return output

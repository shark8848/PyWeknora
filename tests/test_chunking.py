from __future__ import annotations

import io
import zipfile
from html import escape
from urllib.parse import quote

from PIL import Image

from app.services import chunking


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), (12, 34, 56)).save(output, format="PNG")
    return output.getvalue()


def _pdf() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(
        output,
        format="PDF",
        save_all=True,
        append_images=[Image.new("RGB", (8, 8), (0, 255, 0))],
    )
    return output.getvalue()


def _docx() -> bytes:
    document = b"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
            xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Overview</w:t></w:r></w:p>
    <w:p>
      <w:r><w:t>See figure: </w:t></w:r>
      <w:r><w:drawing><wp:inline>
        <wp:docPr id="1" name="Picture 1" descr="Network diagram"/>
        <a:graphic><a:graphicData><a:blip r:embed="rId1"/></a:graphicData></a:graphic>
      </wp:inline></w:drawing></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/chart.png"/>
</Relationships>
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", relationships)
        archive.writestr("word/media/chart.png", _png())
    return output.getvalue()


def _pptx() -> bytes:
    slide = b"""<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
       xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p><a:r><a:t>Slide title</a:t></a:r></a:p></p:txBody></p:sp>
    <p:pic>
      <p:nvPicPr><p:cNvPr id="2" name="Picture 2" descr="Architecture"/></p:nvPicPr>
      <p:blipFill><a:blip r:embed="rId2"/></p:blipFill>
    </p:pic>
  </p:spTree></p:cSld>
</p:sld>
"""
    relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image2.png"/>
</Relationships>
"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide)
        archive.writestr("ppt/slides/_rels/slide1.xml.rels", relationships)
        archive.writestr("ppt/media/image2.png", _png())
    return output.getvalue()


def _epub_literal_paths(
    chapter_path: str,
    image_src: str,
    image_path: str,
    decoy_path: str,
) -> tuple[bytes, bytes]:
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
        ' version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    package = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
        f'<item id="chapter" href="{quote(chapter_path)}" media-type="application/xhtml+xml"/>'
        f'<item id="expected" href="{quote(image_path)}" media-type="image/png"/>'
        f'<item id="decoy" href="{quote(decoy_path)}" media-type="image/png"/>'
        '</manifest><spine><itemref idref="chapter"/></spine></package>'
    ).encode()
    chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Chapter</h1>'
        f'<img alt="expected" src="{escape(image_src, quote=True)}"/>'
        '</body></html>'
    ).encode()
    expected = _png()
    decoy = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(decoy, format="PNG")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)
        archive.writestr(f"EPUB/{chapter_path}", chapter)
        archive.writestr(f"EPUB/{image_path}", expected)
        archive.writestr(f"EPUB/{decoy_path}", decoy.getvalue())
    return output.getvalue(), expected


def _epub_colliding_chapters() -> tuple[bytes, dict[str, bytes]]:
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
        ' version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    package = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
        '<item id="root" href="root.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="first" href="first/chapter.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="second" href="second/chapter.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="ri" href="pic.png" media-type="image/png"/>'
        '<item id="fi" href="first/pic.png" media-type="image/png"/>'
        '<item id="si" href="second/pic.png" media-type="image/png"/>'
        '</manifest><spine><itemref idref="root"/><itemref idref="first"/>'
        '<itemref idref="second"/></spine></package>'
    ).encode()
    colors = {"root": (255, 0, 0), "first": (0, 255, 0), "second": (0, 0, 255)}
    images: dict[str, bytes] = {}
    for name, color in colors.items():
        output = io.BytesIO()
        Image.new("RGB", (2, 2), color).save(output, format="PNG")
        images[name] = output.getvalue()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)
        for name in colors:
            path = "EPUB/pic.png" if name == "root" else f"EPUB/{name}/pic.png"
            archive.writestr(path, images[name])
            chapter = "EPUB/root.xhtml" if name == "root" else f"EPUB/{name}/chapter.xhtml"
            archive.writestr(
                chapter,
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                f'<h1>{name}</h1><p>Text {name}</p>'
                f'<img alt="{name}" src="pic.png"/></body></html>',
            )
    return output.getvalue(), images


def test_extract_docx_embeds_images_in_document_order() -> None:
    text, assets = chunking.extract_document("report.docx", _docx())
    assert text.startswith("Overview\nSee figure:")
    assert "![Network diagram](embedded-image://0)" in text
    assert len(assets) == 1
    assert assets[0]["name"] == "chart.png"
    assert assets[0]["mime_type"] == "image/png"
    assert assets[0]["caption"] == "Network diagram"
    assert assets[0]["data"].startswith(b"\x89PNG\r\n\x1a\n")
    assert "embedded-image://" not in chunking.extract_text("report.docx", _docx())


def test_extract_pptx_embeds_images_from_slide_relationships() -> None:
    text, assets = chunking.extract_document("deck.pptx", _pptx())
    assert "Slide title" in text
    assert "![Architecture](embedded-image://0)" in text
    assert len(assets) == 1
    assert assets[0]["name"] == "image2.png"
    assert assets[0]["caption"] == "Architecture"


def test_extract_pdf_embeds_page_images_in_page_order() -> None:
    text, assets = chunking.extract_document("report.pdf", _pdf())
    assert "![Page 1 image 1](embedded-image://0)" in text
    assert "![Page 2 image 1](embedded-image://1)" in text
    assert text.index("embedded-image://0") < text.index("embedded-image://1")
    assert not text.startswith("%PDF")
    assert len(assets) == 2
    assert [asset["caption"] for asset in assets] == ["Page 1 image 1", "Page 2 image 1"]
    assert all(asset["mime_type"] == "image/jpeg" for asset in assets)
    assert all(asset["data"].startswith(b"\xff\xd8") for asset in assets)
    assert "embedded-image://" not in chunking.extract_text("report.pdf", _pdf())


def test_extract_epub_preserves_chapter_order_and_relative_images() -> None:
    content, images = _epub_colliding_chapters()
    text, assets = chunking.extract_document("book.epub", content)
    assert text.index("Text root") < text.index("Text first") < text.index("Text second")
    assert len(assets) == 3
    for name, expected in images.items():
        asset = next(item for item in assets if item["data"] == expected)
        assert f"![{name}]({asset['marker']})" in text
    assert "embedded-image://" not in chunking.extract_text("book.epub", content)


def test_epub_chapters_follow_spine_then_manifest_without_duplicates() -> None:
    container = (
        '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
        ' version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    package = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
        '<item id="appendix" href="appendix.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="cover" href="cover.svg" media-type="image/svg+xml"/>'
        '</manifest><spine><itemref idref="one"/><itemref idref="cover"/>'
        '<itemref idref="two"/><itemref idref="one"/></spine></package>'
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)
        for name in ("one", "two", "appendix"):
            archive.writestr(f"EPUB/{name}.xhtml", f"<html><body>{name}</body></html>")
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as book:
        chapters, _ = chunking._epub_package(book)
    assert chapters == ["EPUB/one.xhtml", "EPUB/two.xhtml", "EPUB/appendix.xhtml"]


def test_extract_epub_preserves_literal_archive_path_characters() -> None:
    cases = (
        ("Text#1/chapter.xhtml", "../images/pic.png", "images/pic.png", "Text#1/cover.png"),
        ("Text?1/chapter.xhtml", "../images/pic.png", "images/pic.png", "Text?1/cover.png"),
        ("Text%231/chapter.xhtml", "pic.png", "Text%231/pic.png", "Text#1/pic.png"),
        ("Text/chapter.xhtml", "../images/pic%231.png?version=1#image", "images/pic#1.png", "images/pic#2.png"),
        ("Text/chapter.xhtml", "../images/pic%3F1.png?version=1#image", "images/pic?1.png", "images/pic?2.png"),
        ("Text/chapter.xhtml", "../images/pic%25231.png", "images/pic%231.png", "images/pic#1.png"),
        ("Text/chapter.xhtml", "../images/pic%231.png", "images/pic#1.png", "images/pic%231.png"),
        ("chapter.xhtml", "images/my%20pic.png", "images/my%20pic.png", "my pic.png"),
        ("Text/chapter.xhtml", "../images/my%20pic.png?version=1#image", "images/my%20pic.png", "my pic.png"),
        ("Text/chapter.xhtml", "images/my%20pic.png", "Text/images/my%20pic.png", "images/my pic.png"),
        ("Text/chapter.xhtml", "../images/my%20pic.png", "images/my pic.png", "images/my%20pic.png"),
        ("Text/chapter.xhtml", "%5Cimages/pic.png", "images/pic.png", "Text/images/pic.png"),
    )
    for chapter_path, image_src, image_path, decoy_path in cases:
        content, expected = _epub_literal_paths(
            chapter_path, image_src, image_path, decoy_path
        )
        text, assets = chunking.extract_document("literal.epub", content)
        asset = next(item for item in assets if item["data"] == expected)
        assert f"![expected]({asset['marker']})" in text, (chapter_path, image_src)

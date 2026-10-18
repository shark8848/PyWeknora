from __future__ import annotations

from collections import Counter

import pytest

from app.services import chunker_adaptive


@pytest.mark.parametrize(
    ("open_fence", "body", "close_fence"),
    [
        ("```go", "# Fake\n## Fake sub", "```"),
        ("~~~markdown", "# Fake\n## Fake sub", "~~~"),
        ("````markdown", "```\n# Fake\n## Fake sub", "````"),
        ("~~~~", "~~~\n# Fake\n## Fake sub", "~~~~~"),
        ("```", "~~~\n# Fake\n## Fake sub", "```"),
        ("```", "``` trailing\n# Fake\n## Fake sub", "```"),
        ("```", "    ```\n# Fake\n## Fake sub", "```"),
        ("```", "\t```\n# Fake\n## Fake sub", "```"),
        ("   ~~~go", "# Fake\n## Fake sub", "  ~~~~ \t"),
        ("```go title=example", "# Fake\n## Fake sub", "```"),
        ("~~~go `example`", "# Fake\n## Fake sub", "~~~"),
        ("~~~", "# Fake\n## Fake sub", ""),
    ],
)
def test_heading_scans_respect_markdown_fences(
    open_fence: str, body: str, close_fence: str
) -> None:
    document = f"# Real\n{open_fence}\n{body}"
    expected = ["# Real"]
    if close_fence:
        document += f"\n{close_fence}\n# After\n## Real sub"
        expected.extend(["# After", "## Real sub"])

    profile = chunker_adaptive.profile_document(document)
    assert profile["md_heading_total"] == len(expected)
    assert profile["md_heading_counts"] == dict(
        Counter(str(len(line) - len(line.lstrip("#"))) for line in expected)
    )
    assert profile["has_code"] is True
    code_chars = sum(len(line) for line in body.split("\n"))
    assert profile["code_ratio"] == pytest.approx(code_chars / len(document))
    assert [line for _, _, line in chunker_adaptive._heading_entries(document)] == expected


@pytest.mark.parametrize(
    "open_fence", ["``", "~~", "    ```", "\t~~~", "```go `example`", "`~`"]
)
def test_invalid_fence_openers_do_not_hide_headings(open_fence: str) -> None:
    document = f"{open_fence}\n# Real\n## Real sub"
    profile = chunker_adaptive.profile_document(document)

    assert profile["has_code"] is False
    assert profile["code_ratio"] == 0
    assert profile["md_heading_total"] == 2
    assert [line for _, _, line in chunker_adaptive._heading_entries(document)] == [
        "# Real",
        "## Real sub",
    ]


def test_heading_entries_keep_offsets_after_fenced_code() -> None:
    document = "# Real\n```markdown\n# Fake\n```\n## Real sub\n"
    entries = chunker_adaptive._heading_entries(document)

    assert [line for _, _, line in entries] == ["# Real", "## Real sub"]
    assert entries[0][0] == 0
    assert entries[1][0] == document.index("## Real sub")

"""Reject machine Wiki rewrites that silently drop Markdown table rows."""

from __future__ import annotations


INGEST_ROW_IDENTITY_LOSS_LIMIT = 0.20
_EXAMPLE_LIMIT = 5


def _is_table_row(line: str) -> bool:
    trimmed = line.strip()
    return trimmed.startswith("|") and trimmed.count("|") >= 2


def _is_delimiter_row(line: str) -> bool:
    return all(cell.strip("-: \t") == "" for cell in line.split("|"))


def _normalize_cell(cell: str) -> str:
    cell = cell.replace("\u3000", " ")
    for marker in ("*", "_", "`"):
        cell = cell.replace(marker, "")
    return " ".join(cell.lower().split())


def _row_identity(line: str) -> str:
    for cell in line.split("|"):
        identity = _normalize_cell(cell)
        if identity:
            return identity
    return ""


def table_row_identities(content: str) -> list[str]:
    """Return data-row identities in file order, excluding headers and delimiters."""
    lines = str(content or "").splitlines()
    identities: list[str] = []
    for index, line in enumerate(lines):
        if not _is_table_row(line) or _is_delimiter_row(line):
            continue
        if (
            index + 1 < len(lines)
            and _is_table_row(lines[index + 1])
            and _is_delimiter_row(lines[index + 1])
        ):
            continue
        identity = _row_identity(line)
        if identity:
            identities.append(identity)
    return identities


def unique_row_identity_count(content: str) -> int:
    return len(set(table_row_identities(content)))


def row_identity_loss_ratio(missing: int, stored: int) -> float:
    if stored <= 0:
        return 0.0
    return missing / stored


def missing_row_identities(existing: str, rewritten: str) -> list[str]:
    """Return sorted stored row identities absent from the rewrite."""
    stored = table_row_identities(existing)
    if not stored:
        return []
    present = set(table_row_identities(rewritten))
    return sorted({identity for identity in stored if identity not in present})


def dropped_rows_message(
    existing: str,
    rewritten: str,
    slug: str,
    *,
    loss_limit: float,
) -> str:
    """Return a refusal message when a rewrite exceeds the row-loss limit."""
    missing = missing_row_identities(existing, rewritten)
    if not missing:
        return ""
    stored = unique_row_identity_count(existing)
    if row_identity_loss_ratio(len(missing), stored) <= loss_limit:
        return ""
    examples = ", ".join(missing[:_EXAMPLE_LIMIT])
    return (
        f"Wiki page write dropped table rows the stored page still has: slug {slug} "
        f"lost {len(missing)} of {stored} table row identities (e.g. {examples}); "
        "the stored page is unchanged"
    )

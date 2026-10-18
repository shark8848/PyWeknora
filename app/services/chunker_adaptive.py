"""Adaptive, offset-preserving chunking used by the chunker preview API.

This mirrors the strategy surface of WeKnora's Go chunker: a document profile
selects a heading-aware, heuristic, or legacy splitter; failed tiers fall
through to the next tier; and parent/child previews derive the same splitter
configs as ingestion.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 80
MAX_PREVIEW_CHARS = 64 * 1024
MAX_PREVIEW_CHUNKS = 500

_CHARS_PER_TOKEN = {"en": 4.0, "de": 4.5, "zh": 1.7, "mixed": 3.0}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBERED_RE = re.compile(r"^[ \t]*(?:\d+(?:\.\d+){1,3}\.?|(?:\d+|[IVX]{1,5})\.)[ \t]+\S.{0,200}$")
_ALL_CAPS_RE = re.compile(r"^[ \t]*([A-ZÄÖÜ][A-ZÄÖÜ \-]{3,80}):?\s*$")
_VISUAL_RE = re.compile(r"^[ \t]*(?:-{3,}|={3,}|\*{3,}|_{3,})[ \t]*$")
_FOOTER_RE = re.compile(r"^[ \t]*(?:Seite|Page|页码?)\s+\d+(?:\s*(?:von|of|/)\s*\d+)?[ \t]*$", re.I)
_GERMAN_RE = re.compile(r"^[ \t]*(?:Kapitel|Abschnitt|Teil)\s+(?:[0-9]+|[IVX]{1,5})[\.: ].{0,200}$")
_ENGLISH_RE = re.compile(r"^[ \t]*(?:Chapter|Section|Part)\s+(?:[0-9]+|[IVX]{1,5})[\.: ].{0,200}$")
_CHINESE_RE = re.compile(r"^[ \t]*第[ \t]*[一二三四五六七八九十百千零〇0-9]+[ \t]*(?:章|节|節|部分|篇)[ \t]?.{0,200}$")

_PROTECTED_PATTERNS = (
    re.compile(r"(?s)\$\$.*?\$\$"),
    re.compile(r"!\[[^\]\n]{0,200}\]\([^)\n]{1,500}\)"),
    re.compile(r"\[[^\]\n]{1,200}\]\([^)\n]{1,500}\)"),
    re.compile(r"(?m)^[ ]*(?:\|[^|\n]*)+\|[\r\n]+[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|[\r\n]+"),
    re.compile(r"(?m)^[ ]*(?:\|[^|\n]*)+\|[\r\n]+"),
    re.compile(r"(?s)```(?:\w+)?[\r\n].*?```"),
    re.compile(r"`[^`\r\n]+`"),
)


@dataclass(slots=True)
class Chunk:
    content: str
    start: int
    end: int
    seq: int = 0
    context_header: str = ""


@dataclass(slots=True)
class SplitterConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    separators: tuple[str, ...] = ("\n\n", "\n", "。")
    strategy: str = ""
    token_limit: int = 0
    languages: tuple[str, ...] = ()


@dataclass(slots=True)
class Unit:
    text: str
    start: int
    end: int


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def detect_language(text: str) -> str:
    if not text:
        return "mixed"
    cjk = latin = umlaut = 0
    for char in text[:4000]:
        code = ord(char)
        if (
            0x3040 <= code <= 0x30FF
            or 0x3400 <= code <= 0x9FFF
            or 0xAC00 <= code <= 0xD7AF
        ):
            cjk += 1
        elif char in "äöüÄÖÜß":
            umlaut += 1
            latin += 1
        elif ("a" <= char <= "z") or ("A" <= char <= "Z"):
            latin += 1
    total = cjk + latin
    if total == 0:
        return "mixed"
    cjk_ratio = cjk / total
    latin_ratio = latin / total
    if cjk_ratio >= 0.15 and latin_ratio >= 0.15:
        return "mixed"
    if cjk_ratio > 0.3:
        return "zh"
    lowered = text[:512].lower()
    if umlaut or any(word in lowered for word in (" der ", " die ", " das ", " und ", " ist ", " nicht ", " mit ", " auf ")):
        return "de"
    return "en"


@dataclass
class _MarkdownFence:
    marker: str = ""
    length: int = 0

    def consume(self, line: str) -> tuple[bool, bool]:
        inside = self.length > 0
        line = line.removesuffix("\r")
        indent = 0
        while indent < len(line) and line[indent] == " ":
            indent += 1
        if indent > 3 or indent == len(line):
            return inside, False
        line = line[indent:]
        marker = line[0]
        if marker not in "`~":
            return inside, False
        length = 0
        while length < len(line) and line[length] == marker:
            length += 1
        if length < 3:
            return inside, False
        rest = line[length:]
        if inside:
            if marker == self.marker and length >= self.length and not rest.strip(" \t"):
                self.marker = ""
                self.length = 0
                return True, True
            return True, False
        if marker == "`" and "`" in rest:
            return False, False
        self.marker, self.length = marker, length
        return True, True


def profile_document(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    heading_counts: dict[str, int] = {}
    numbered = all_caps = visual = footer = 0
    german = english = chinese = 0
    has_tables = has_code = False
    code_chars = 0
    lengths: list[int] = []
    fence = _MarkdownFence()
    for line in lines:
        code, delimiter = fence.consume(line)
        if code:
            has_code = True
            if not delimiter:
                code_chars += len(line)
            continue
        lengths.append(len(line))
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            heading_counts[str(level)] = heading_counts.get(str(level), 0) + 1
            continue
        numbered += int(bool(_NUMBERED_RE.match(line)))
        german += int(bool(_GERMAN_RE.match(line)))
        english += int(bool(_ENGLISH_RE.match(line)))
        chinese += int(bool(_CHINESE_RE.match(line)))
        all_caps += int(bool(_ALL_CAPS_RE.match(line)))
        visual += int(bool(_VISUAL_RE.match(line)))
        footer += int(bool(_FOOTER_RE.match(line)))
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            has_tables = True
    avg = sum(lengths) / len(lengths) if lengths else 0.0
    std = math.sqrt(sum((length - avg) ** 2 for length in lengths) / len(lengths)) if lengths else 0.0
    total_chars = len(text)
    language = detect_language(text)
    return {
        "total_chars": total_chars,
        "total_lines": len(lines),
        "avg_line_len": avg,
        "std_line_len": std,
        "md_heading_counts": heading_counts,
        "md_heading_total": sum(heading_counts.values()),
        "numbered_section_count": numbered,
        "all_caps_short_line_count": all_caps,
        "blank_paragraph_breaks": text.count("\n\n\n"),
        "form_feed_count": text.count("\f"),
        "visual_sep_count": visual,
        "german_chapter_count": german,
        "english_chapter_count": english,
        "chinese_chapter_count": chinese,
        "repeated_footer_count": footer,
        "has_tables": has_tables,
        "has_code": has_code,
        "code_ratio": (code_chars / total_chars) if total_chars else 0.0,
        "detected_langs": [language],
    }


def _dominant_heading_level(profile: dict[str, Any]) -> int:
    counts = {int(level): int(count) for level, count in profile["md_heading_counts"].items()}
    if not counts:
        return 0
    for level in range(1, 7):
        if counts.get(level, 0) >= 3:
            return level
    for level in range(6, 0, -1):
        if counts.get(level, 0) > 0:
            return level
    return 0


def _heuristic_markers(profile: dict[str, Any]) -> int:
    return sum(
        int(profile[key])
        for key in (
            "numbered_section_count",
            "german_chapter_count",
            "english_chapter_count",
            "chinese_chapter_count",
            "all_caps_short_line_count",
            "visual_sep_count",
            "form_feed_count",
        )
    )


def resolve_chain(text: str, strategy: str) -> tuple[list[str], dict[str, Any]]:
    profile = profile_document(text)
    if strategy == "heading":
        return ["heading", "legacy"], profile
    if strategy == "heuristic":
        return ["heuristic", "legacy"], profile
    if strategy in {"recursive", "legacy", ""}:
        return ["legacy"], profile

    chain: list[str] = []
    density = profile["md_heading_total"] / profile["total_lines"] if profile["total_lines"] else 0.0
    if (
        profile["md_heading_total"] >= 3
        and density > 0.005
        and _dominant_heading_level(profile) > 0
    ):
        chain.append("heading")
    if (
        _heuristic_markers(profile) >= 5
        or profile["form_feed_count"] > 0
        or profile["german_chapter_count"] + profile["english_chapter_count"] + profile["chinese_chapter_count"] > 0
    ):
        chain.append("heuristic")
    chain.append("legacy")
    return chain, profile


def chars_for_token_limit(tokens: int, language: str) -> int:
    if tokens <= 0:
        return 0
    return int(tokens * _CHARS_PER_TOKEN.get(language, 3.0) * 0.9)


def normalize_config(
    chunk_size: Any = 0,
    chunk_overlap: Any = 0,
    separators: Any = None,
    strategy: Any = "",
    token_limit: Any = 0,
    languages: Any = None,
) -> SplitterConfig:
    size = int(chunk_size or DEFAULT_CHUNK_SIZE)
    overlap = int(chunk_overlap or DEFAULT_CHUNK_OVERLAP)
    if size <= 0:
        size = DEFAULT_CHUNK_SIZE
    if overlap < 0:
        overlap = 0
    values = tuple(str(value) for value in separators or () if str(value))
    if not values:
        values = ("\n\n", "\n", "。")
    limit = int(token_limit or 0)
    langs = tuple(str(value) for value in languages or () if str(value))
    if limit > 0:
        budget = chars_for_token_limit(limit, langs[0] if langs else "mixed")
        if budget > 0 and budget < size:
            size = budget
    if overlap > size // 2 and size > 0:
        overlap = size // 2
    return SplitterConfig(
        chunk_size=size,
        chunk_overlap=overlap,
        separators=values,
        strategy=str(strategy or "").lower(),
        token_limit=limit,
        languages=langs,
    )


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for pattern in _PROTECTED_PATTERNS:
        found.extend((match.start(), match.end()) for match in pattern.finditer(text) if match.end() > match.start())
    if not found:
        return []
    found.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    merged: list[tuple[int, int]] = []
    last_end = 0
    for start, end in found:
        if start >= last_end:
            merged.append((start, end))
            last_end = end
    return merged


def _split_segment(text: str, start: int, separators: tuple[str, ...], size: int) -> list[Unit]:
    if not text:
        return []
    if len(text) <= size or not separators:
        return [Unit(text, start, start + len(text))]
    for index, separator in enumerate(separators):
        if not separator or separator not in text:
            continue
        pieces: list[Unit] = []
        cursor = 0
        while cursor <= len(text):
            found = text.find(separator, cursor)
            if found < 0:
                if cursor < len(text):
                    pieces.append(Unit(text[cursor:], start + cursor, start + len(text)))
                break
            if found > cursor:
                pieces.append(Unit(text[cursor:found], start + cursor, start + found))
            end = found + len(separator)
            pieces.append(Unit(text[found:end], start + found, start + end))
            cursor = end
        if len(pieces) <= 1:
            continue
        remaining = separators[index + 1 :]
        output: list[Unit] = []
        for piece in pieces:
            if len(piece.text) > size and remaining:
                output.extend(_split_segment(piece.text, piece.start, remaining, size))
            else:
                output.append(piece)
        return output
    return [Unit(text, start, start + len(text))]


def _hard_split(unit: Unit, size: int) -> list[Unit]:
    if len(unit.text) <= size:
        return [unit]
    output: list[Unit] = []
    cursor = 0
    while cursor < len(unit.text):
        end = min(len(unit.text), cursor + max(1, size))
        if end < len(unit.text):
            window = unit.text[cursor:end]
            boundary = max(window.rfind("\n"), window.rfind(" "))
            if boundary > size // 2:
                end = cursor + boundary + 1
        output.append(Unit(unit.text[cursor:end], unit.start + cursor, unit.start + end))
        cursor = end
    return output


def _units_with_protection(text: str, separators: tuple[str, ...], size: int) -> list[Unit]:
    units: list[Unit] = []
    cursor = 0
    for start, end in _protected_ranges(text):
        if start > cursor:
            units.extend(_split_segment(text[cursor:start], cursor, separators, size))
        protected = Unit(text[start:end], start, end)
        if len(protected.text) > 7500:
            cursor_r = 0
            while cursor_r < len(protected.text):
                next_r = min(len(protected.text), cursor_r + 7500)
                units.append(Unit(protected.text[cursor_r:next_r], start + cursor_r, start + next_r))
                cursor_r = next_r
        else:
            units.append(protected)
        cursor = end
    if cursor < len(text):
        units.extend(_split_segment(text[cursor:], cursor, separators, size))
    return units


def _chunk_from_units(units: list[Unit], seq: int) -> Chunk:
    return Chunk(
        content="".join(unit.text for unit in units),
        start=units[0].start,
        end=units[-1].end,
        seq=seq,
    )


def _overlap_units(units: list[Unit], overlap: int, next_len: int, size: int) -> list[Unit]:
    if overlap <= 0 or not units:
        return []
    kept: list[Unit] = []
    total = 0
    for unit in reversed(units):
        if total + len(unit.text) > overlap:
            break
        if total + len(unit.text) + next_len > size:
            break
        kept.insert(0, unit)
        total += len(unit.text)
    return kept


def _split_legacy(text: str, config: SplitterConfig) -> list[Chunk]:
    if not text:
        return []
    units = _units_with_protection(text, config.separators, config.chunk_size)
    chunks: list[Chunk] = []
    current: list[Unit] = []
    current_len = 0
    for unit in units:
        pieces = _hard_split(unit, config.chunk_size)
        for piece in pieces:
            piece_len = len(piece.text)
            if current and current_len + piece_len > config.chunk_size:
                chunks.append(_chunk_from_units(current, len(chunks)))
                current = _overlap_units(current, config.chunk_overlap, piece_len, config.chunk_size)
                current_len = sum(len(item.text) for item in current)
            if not current and piece_len > config.chunk_size:
                chunks.append(Chunk(piece.text, piece.start, piece.end, len(chunks)))
                continue
            current.append(piece)
            current_len += piece_len
    if current:
        chunks.append(_chunk_from_units(current, len(chunks)))
    return chunks


def _heading_entries(text: str) -> list[tuple[int, int, str]]:
    entries: list[tuple[int, int, str]] = []
    offset = 0
    fence = _MarkdownFence()
    for line in text.split("\n"):
        code, _ = fence.consume(line)
        if not code:
            match = _HEADING_RE.match(line)
            if match:
                entries.append((offset, len(match.group(1)), line))
        offset += len(line) + 1
    return entries


def _split_heading(text: str, config: SplitterConfig, profile: dict[str, Any]) -> list[Chunk]:
    primary = _dominant_heading_level(profile)
    if not primary:
        return _split_legacy(text, config)
    headings = _heading_entries(text)
    boundaries: list[tuple[int, str]] = [(0, "")]
    for start, level, line in headings:
        if level <= primary:
            if start == 0:
                boundaries[0] = (0, line)
            elif start > 0:
                boundaries.append((start, line))
    if len(boundaries) <= 1:
        return _split_legacy(text, config)

    chunks: list[Chunk] = []
    stack: list[tuple[int, str]] = []
    for index, (start, line) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(text)
        if line:
            level = len(line) - len(line.lstrip("#"))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, line.strip()))
        breadcrumb = "\n".join(item[1] for item in stack)
        section = text[start:end]
        if not section:
            continue
        if len(breadcrumb) + 2 + len(section) <= config.chunk_size:
            chunks.append(Chunk(section, start, end, len(chunks), breadcrumb))
            continue
        inner = _split_legacy(section, config)
        for chunk in inner:
            chunks.append(
                Chunk(
                    content=chunk.content,
                    start=start + chunk.start,
                    end=start + chunk.end,
                    seq=len(chunks),
                    context_header=breadcrumb,
                )
            )
    return chunks


def _line_starts(text: str) -> list[tuple[int, str]]:
    output: list[tuple[int, str]] = []
    offset = 0
    for line in text.split("\n"):
        output.append((offset, line))
        offset += len(line) + 1
    return output


def _split_heuristic(text: str, config: SplitterConfig) -> list[Chunk]:
    if len(text) <= config.chunk_size:
        return _split_legacy(text, config)
    boundaries: set[int] = set()
    fence = _MarkdownFence()
    for offset, line in _line_starts(text):
        code, _ = fence.consume(line)
        if code:
            continue
        if (
            _GERMAN_RE.match(line)
            or _ENGLISH_RE.match(line)
            or _CHINESE_RE.match(line)
            or _NUMBERED_RE.match(line)
            or _ALL_CAPS_RE.match(line)
            or _VISUAL_RE.match(line)
        ):
            boundaries.add(offset)
    boundaries.update(index for index, char in enumerate(text) if char == "\f")
    if not boundaries:
        return _split_legacy(text, config)
    ordered = sorted(boundaries)
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        target = min(len(text), start + config.chunk_size)
        chosen = max((boundary for boundary in ordered if start < boundary <= target), default=target)
        if chosen <= start:
            chosen = target
        hard = Chunk(text[start:chosen], start, chosen, len(chunks))
        chunks.append(hard)
        if chosen >= len(text):
            break
        start = max(start + 1, chosen - config.chunk_overlap)
    return chunks


def _validate(chunks: list[Chunk], total_chars: int, chunk_size: int) -> tuple[bool, str]:
    if not chunks:
        return False, "no chunks produced"
    if len(chunks) == 1 and total_chars > 2 * chunk_size:
        return False, "single chunk for large document"
    lengths = [len(chunk.content) for chunk in chunks]
    tiny = sum(1 for length in lengths[:-1] if length < 50)
    if tiny > len(chunks) // 4 and tiny > 2:
        return False, "too many tiny chunks"
    maximum = max(lengths, default=0)
    if maximum < chunk_size // 4 and total_chars > chunk_size:
        return False, "all chunks far below target size"
    if chunk_size > 0 and maximum > 2 * chunk_size:
        return False, "chunk exceeds 2x target size"
    return True, ""


def split_with_diagnostics(text: str, config: SplitterConfig) -> tuple[list[Chunk], dict[str, Any]]:
    chain, profile = resolve_chain(text, config.strategy)
    rejected: list[dict[str, str]] = []
    last: list[Chunk] = []
    selected = "legacy"
    for tier in chain:
        if tier == "heading":
            chunks = _split_heading(text, config, profile)
        elif tier == "heuristic":
            chunks = _split_heuristic(text, config)
        else:
            chunks = _split_legacy(text, config)
        ok, reason = _validate(chunks, len(text), config.chunk_size)
        if ok:
            selected = tier
            return chunks, {
                "selected_tier": selected,
                "tier_chain": chain,
                "rejected": rejected,
                "profile": profile,
            }
        rejected.append({"tier": tier, "reason": reason})
        last = chunks
    return (last or _split_legacy(text, config)), {
        "selected_tier": selected,
        "tier_chain": chain,
        "rejected": rejected,
        "profile": profile,
    }


def derive_parent_child_configs(base: SplitterConfig, parent_size: int, child_size: int) -> tuple[SplitterConfig, SplitterConfig]:
    parent_size = parent_size if parent_size > 0 else 4096
    child_size = child_size if child_size > 0 else 384
    parent = SplitterConfig(
        chunk_size=parent_size,
        chunk_overlap=base.chunk_overlap,
        separators=base.separators,
        strategy=base.strategy,
        languages=base.languages,
    )
    child = SplitterConfig(
        chunk_size=child_size,
        chunk_overlap=child_size // 5,
        separators=base.separators,
        strategy=base.strategy,
        token_limit=base.token_limit,
        languages=base.languages,
    )
    return parent, child


def merge_breadcrumbs(parent: str, child: str) -> str:
    if not parent:
        return child
    if not child:
        return parent
    parent_lines = parent.split("\n")
    child_lines = child.split("\n")
    if parent_lines and child_lines and parent_lines[-1].strip() == child_lines[0].strip():
        child_lines = child_lines[1:]
    lines = parent_lines + [line for line in child_lines if line.strip() and line.strip() not in {item.strip() for item in parent_lines}]
    return "\n".join(lines)


def split_parent_child(text: str, parent: SplitterConfig, child: SplitterConfig) -> list[Chunk]:
    parents, _ = split_with_diagnostics(text, parent)
    output: list[Chunk] = []
    for parent_chunk in parents:
        children, _ = split_with_diagnostics(parent_chunk.content, child)
        for item in children:
            output.append(
                Chunk(
                    content=item.content,
                    start=parent_chunk.start + item.start,
                    end=parent_chunk.start + item.end,
                    seq=len(output),
                    context_header=merge_breadcrumbs(parent_chunk.context_header, item.context_header),
                )
            )
    return output


def approx_tokens(length: int, language: str) -> int:
    if length <= 0:
        return 0
    ratio = _CHARS_PER_TOKEN.get(language, 3.0)
    return max(1, int(length / ratio + 0.5))


def compute_stats(lengths: list[int]) -> dict[str, Any]:
    if not lengths:
        return {"count": 0, "avg_chars": 0, "min_chars": 0, "max_chars": 0, "stddev_chars": 0}
    average = sum(lengths) / len(lengths)
    variance = sum((length - average) ** 2 for length in lengths) / len(lengths)
    return {
        "count": len(lengths),
        "avg_chars": int(average),
        "min_chars": min(lengths),
        "max_chars": max(lengths),
        "stddev_chars": int(math.sqrt(max(0.0, variance))),
    }


def preview(text: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    config = normalize_config(
        chunk_size=payload.get("chunk_size"),
        chunk_overlap=payload.get("chunk_overlap"),
        separators=payload.get("separators"),
        strategy=payload.get("strategy"),
        token_limit=payload.get("token_limit"),
        languages=payload.get("languages"),
    )
    if bool(payload.get("enable_parent_child")):
        parent_config, child_config = derive_parent_child_configs(
            config,
            int(payload.get("parent_chunk_size") or 0),
            int(payload.get("child_chunk_size") or 0),
        )
        chunks = split_parent_child(text, parent_config, child_config)
        _, diagnostics = split_with_diagnostics(text, parent_config)
    else:
        chunks, diagnostics = split_with_diagnostics(text, config)

    language = (diagnostics["profile"].get("detected_langs") or ["mixed"])[0]
    lengths = [len(chunk.content) for chunk in chunks]
    total = len(chunks)
    stats = compute_stats(lengths)
    if total > MAX_PREVIEW_CHUNKS:
        stats["truncated_to"] = total
        chunks = chunks[:MAX_PREVIEW_CHUNKS]
        lengths = lengths[:MAX_PREVIEW_CHUNKS]
    results = [
        {
            "seq": chunk.seq if chunk.seq < MAX_PREVIEW_CHUNKS else index,
            "start": chunk.start,
            "end": chunk.end,
            "size_chars": lengths[index],
            "size_tokens_approx": approx_tokens(lengths[index], language),
            "context_header": chunk.context_header,
            "content": chunk.content,
        }
        for index, chunk in enumerate(chunks)
    ]
    return {
        "selected_tier": diagnostics["selected_tier"],
        "tier_chain": diagnostics["tier_chain"],
        "rejected": diagnostics["rejected"],
        "profile": diagnostics["profile"],
        "chunks": results,
        "stats": stats,
    }

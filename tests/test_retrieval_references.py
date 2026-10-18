from __future__ import annotations

import json

from app.services import retrieval


def test_custom_metadata_text_is_stable_and_skips_empty_values() -> None:
    raw = json.dumps(
        {
            "region": "Shanghai",
            "department": "R&D",
            "empty": "  ",
            "missing": None,
        }
    )

    assert retrieval._custom_metadata_text(raw) == (
        "department: R&D\nregion: Shanghai"
    )
    assert retrieval._custom_metadata_text("not-json") == ""
    assert retrieval._custom_metadata_text(["not", "a", "mapping"]) == ""


def test_references_preserve_knowledge_context_fields() -> None:
    row = {
        "id": "chunk-1",
        "knowledge_id": "knowledge-1",
        "knowledge_base_id": "kb-1",
        "knowledge_base_name": "Course",
        "title": "Lesson",
        "file_name": "lesson.md",
        "knowledge_source": "upload",
        "knowledge_channel": "api",
        "knowledge_description": "Course notes",
        "knowledge_custom_metadata": '{"course_url":"https://example.com/course"}',
        "content": "evidence",
        "chunk_index": 3,
        "chunk_type": "text",
        "parent_chunk_id": "parent-1",
        "score": 0.9,
    }

    reference = retrieval.references([row])[0]

    assert reference["knowledge_title"] == "Lesson"
    assert reference["knowledge_filename"] == "lesson.md"
    assert reference["knowledge_source"] == "upload"
    assert reference["knowledge_channel"] == "api"
    assert reference["knowledge_description"] == "Course notes"
    assert reference["knowledge_custom_metadata"] == (
        "course_url: https://example.com/course"
    )
    assert reference["chunk_index"] == 3
    assert reference["parent_chunk_id"] == "parent-1"


def test_subset_note_only_describes_a_truncated_candidate_pool() -> None:
    note = retrieval.subset_note(2, 5)

    assert '<subset shown="2" candidates="5">' in note
    assert "only the top 2 of 5 candidate passages" in note
    assert "do not treat these passage counts as the requested total" in note
    assert retrieval.subset_note(5, 5) == ""
    assert retrieval.subset_note(0, 5) == ""


def test_retrieval_rows_keep_candidate_and_shown_counts_separate() -> None:
    rows = retrieval._RetrievalRows([{"id": "first"}, {"id": "second"}], candidates=5)

    assert list(rows) == [{"id": "first"}, {"id": "second"}]
    assert rows.candidates == 5

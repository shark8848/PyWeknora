from app.services import retrieval


def test_mmr_prefers_diverse_passage_and_preserves_ties() -> None:
    rows = [
        {"content": "hybrid retrieval combines semantic vectors and keyword search", "score": 0.9},
        {"content": "hybrid retrieval combines semantic vectors and keyword search", "score": 0.88},
        {"content": "wiki construction extracts entities concepts and source citations", "score": 0.83},
    ]
    assert retrieval.mmr_indices(rows, 3) == [0, 2, 1]

    tied = [{"content": "", "score": 0.5} for _ in range(3)]
    assert retrieval.mmr_indices(tied, 10) == [0, 1, 2]
    assert retrieval.mmr_indices(rows, 0) == []
    assert retrieval.mmr_indices([], 2) == []


def test_retrieve_applies_mmr_before_limit(monkeypatch) -> None:
    rows = [
        {"id": "duplicate", "content": "duplicate", "chunk_type": "text"},
        {"id": "near-duplicate", "content": "near-duplicate", "chunk_type": "text"},
        {"id": "diverse", "content": "diverse", "chunk_type": "text"},
    ]
    vectors = {
        "duplicate": [1.0],
        "near-duplicate": [0.9],
        "diverse": [0.5],
    }
    monkeypatch.setattr(retrieval.db, "fetch_all", lambda *_args, **_kwargs: rows)
    monkeypatch.setattr(retrieval, "embed_texts", lambda texts, runtime=None: [[1.0]])
    monkeypatch.setattr(retrieval, "local_embedding", lambda text: vectors[text])
    monkeypatch.setattr(retrieval, "cosine", lambda left, right: left[0] * right[0])

    result = retrieval.retrieve(1, "query", limit=2)

    assert [row["id"] for row in result] == ["duplicate", "diverse"]
    assert result.candidates == 3

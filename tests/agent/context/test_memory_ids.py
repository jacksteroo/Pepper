"""Issue #33: memory_ids round-trip from MemoryManager rows into provenance."""
from __future__ import annotations

import json

from agent.context.selectors import RetrievedMemorySelector


def test_no_records_yields_empty_list() -> None:
    sel = RetrievedMemorySelector()
    rec = sel.select("ctx", memory_records=[])
    assert rec.provenance["memory_ids"] == []
    assert rec.provenance["n_memories"] == 0


def test_each_pair_is_list_of_uuid_str_and_float() -> None:
    rows = [
        {"id": "a1", "score": 0.91},
        {"id": "b2", "sim": 0.42},
    ]
    sel = RetrievedMemorySelector()
    rec = sel.select("ctx", memory_records=rows)
    pairs = rec.provenance["memory_ids"]
    assert len(pairs) == 2
    for pair in pairs:
        assert isinstance(pair, list)
        assert len(pair) == 2
        uid, score = pair
        assert isinstance(uid, str)
        assert isinstance(score, float)


def test_score_preferred_over_sim() -> None:
    """If a row has both ``score`` and ``sim``, the blended ``score`` wins."""
    rows = [{"id": "x", "score": 0.9, "sim": 0.1}]
    sel = RetrievedMemorySelector()
    pairs = sel.select("ctx", memory_records=rows).provenance["memory_ids"]
    assert pairs[0][1] == 0.9


def test_missing_id_skipped() -> None:
    rows = [{"score": 0.9}, {"id": "valid", "score": 0.5}]
    sel = RetrievedMemorySelector()
    pairs = sel.select("ctx", memory_records=rows).provenance["memory_ids"]
    assert len(pairs) == 1
    assert pairs[0][0] == "valid"


def test_pairs_are_json_serialisable() -> None:
    rows = [{"id": "a", "score": 0.5}]
    sel = RetrievedMemorySelector()
    rec = sel.select("ctx", memory_records=rows)
    j = json.loads(json.dumps(rec.provenance))
    # JSON has no tuple — confirm we get a 2-element array.
    assert j["memory_ids"][0] == ["a", 0.5]


def test_no_raw_content_in_provenance() -> None:
    """Privacy: the raw memory ``content`` must NOT travel into provenance."""
    rows = [
        {"id": "a", "content": "SUPER_SECRET_PERSONAL_DATA", "score": 0.5},
    ]
    sel = RetrievedMemorySelector()
    rec = sel.select("ctx", memory_records=rows)
    serialised = json.dumps(rec.provenance)
    assert "SUPER_SECRET_PERSONAL_DATA" not in serialised


def test_legacy_call_without_memory_records_still_works() -> None:
    """Backward-compat: callers that don't thread structured rows still get a
    populated content block, just no memory_ids."""
    sel = RetrievedMemorySelector()
    rec = sel.select("MEMORY_BLOCK")
    assert rec.content == "MEMORY_BLOCK"
    assert rec.provenance["memory_ids"] == []

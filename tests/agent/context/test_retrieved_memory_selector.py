"""RetrievedMemorySelector unit tests."""
from __future__ import annotations

import json

from agent.context.selectors import RetrievedMemorySelector


def test_passes_through_pre_fetched_string() -> None:
    sel = RetrievedMemorySelector()
    rec = sel.select("# Recall results\n- foo bar")

    assert rec.name == "retrieved_memory"
    assert rec.content == "# Recall results\n- foo bar"
    assert rec.provenance["chars"] == len(rec.content)
    assert rec.provenance["present"] is True


def test_empty_string_marked_absent() -> None:
    sel = RetrievedMemorySelector()
    rec = sel.select("")
    assert rec.content == ""
    assert rec.provenance["present"] is False
    assert rec.provenance["chars"] == 0


def test_none_treated_as_empty() -> None:
    sel = RetrievedMemorySelector()
    rec = sel.select(None)  # type: ignore[arg-type]
    assert rec.content == ""
    assert rec.provenance["present"] is False


def test_provenance_is_json_serializable() -> None:
    sel = RetrievedMemorySelector()
    rec = sel.select("payload")
    json.dumps(rec.provenance)

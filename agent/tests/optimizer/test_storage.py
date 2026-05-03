"""Tests for ``agent/optimizer/storage.py`` — round-trip + lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.optimizer.schema import CandidatePrompt, PromptStatus
from agent.optimizer.storage import (
    PromptStore,
    StorageError,
    compute_version_hash,
)


def _make(**overrides) -> CandidatePrompt:
    base = dict(
        target="ctx_assembly",
        version_hash=compute_version_hash("ctx_assembly", "hello"),
        parent_version="",
        optimizer_run_id="run-abc",
        prompt_text="hello",
        eval_score=0.42,
        status=PromptStatus.CANDIDATE,
        created_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
        sanitization=[],
    )
    base.update(overrides)
    return CandidatePrompt(**base)


def test_round_trip(tmp_path):
    store = PromptStore(tmp_path)
    c = _make()
    store.put(c)
    got = store.get(c.target, c.version_hash)
    assert got == c


def test_list_returns_newest_first(tmp_path):
    store = PromptStore(tmp_path)
    older = _make(
        version_hash="0000000000000001",
        prompt_text="A",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer = _make(
        version_hash="0000000000000002",
        prompt_text="B",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    store.put(older)
    store.put(newer)
    listed = store.list(older.target)
    assert [c.version_hash for c in listed] == [newer.version_hash, older.version_hash]


def test_list_filters_by_status(tmp_path):
    store = PromptStore(tmp_path)
    candidate = _make(version_hash="aaaa000000000001", status=PromptStatus.CANDIDATE)
    store.put(candidate)
    accepted = _make(
        version_hash="aaaa000000000002",
        status=PromptStatus.ACCEPTED,
        prompt_text="x",
    )
    # Direct ACCEPTED put for a never-seen-version is allowed.
    store.put(accepted)
    only_accepted = store.list(candidate.target, status=PromptStatus.ACCEPTED)
    assert len(only_accepted) == 1
    assert only_accepted[0].version_hash == accepted.version_hash


def test_invalid_status_transition_rejected(tmp_path):
    store = PromptStore(tmp_path)
    accepted = _make(status=PromptStatus.ACCEPTED)
    store.put(accepted)
    # ACCEPTED → CANDIDATE is not allowed.
    rolled_back_to_candidate = _make(status=PromptStatus.CANDIDATE)
    with pytest.raises(StorageError, match="invalid status transition"):
        store.put(rolled_back_to_candidate)


def test_valid_lifecycle_transitions(tmp_path):
    store = PromptStore(tmp_path)
    # CANDIDATE -> ACCEPTED -> ROLLED_BACK -> CANDIDATE
    c1 = _make(status=PromptStatus.CANDIDATE)
    store.put(c1)
    store.put(_make(status=PromptStatus.ACCEPTED))
    store.put(_make(status=PromptStatus.ROLLED_BACK))
    store.put(_make(status=PromptStatus.CANDIDATE))
    final = store.get(c1.target, c1.version_hash)
    assert final.status == PromptStatus.CANDIDATE


def test_target_traversal_rejected(tmp_path):
    store = PromptStore(tmp_path)
    bad = _make(target="../escape")
    with pytest.raises(StorageError):
        store.put(bad)


def test_version_hash_is_deterministic():
    h1 = compute_version_hash("ctx", "hello world")
    h2 = compute_version_hash("ctx", "hello world")
    assert h1 == h2
    h3 = compute_version_hash("router", "hello world")
    assert h3 != h1
    assert len(h1) == 16


def test_iter_targets_lists_present(tmp_path):
    store = PromptStore(tmp_path)
    store.put(_make(target="ctx_assembly"))
    store.put(_make(
        target="router_classifier",
        version_hash=compute_version_hash("router_classifier", "hello"),
    ))
    assert sorted(store.iter_targets()) == ["ctx_assembly", "router_classifier"]


def test_accepted_with_pii_rejected(tmp_path):
    """ACCEPTED candidates with non-empty sanitization are refused.

    The sanitizer must be more than advisory: ACCEPTED prompts land
    under agent/prompts/ and are committed to git. A leaked life-context
    token in an accepted prompt would ship publicly.
    """
    store = PromptStore(tmp_path)
    bad = _make(
        status=PromptStatus.ACCEPTED,
        sanitization=["life_context token: 'pepperton'"],
    )
    with pytest.raises(StorageError, match="non-empty sanitization"):
        store.put(bad)


def test_candidate_with_pii_allowed(tmp_path):
    """CANDIDATE status with PII findings is allowed — that's the
    expected state until the prompt is sanitized."""
    store = PromptStore(tmp_path)
    c = _make(
        status=PromptStatus.CANDIDATE,
        sanitization=["life_context token: 'pepperton'"],
    )
    store.put(c)  # must not raise
    got = store.get(c.target, c.version_hash)
    assert got.sanitization == ["life_context token: 'pepperton'"]


def test_corrupt_file_is_ignored_in_list(tmp_path):
    store = PromptStore(tmp_path)
    store.put(_make())
    # Drop a malformed file in the target dir.
    (tmp_path / "ctx_assembly" / "deadbeef00000000.json").write_text("{not json")
    listed = store.list("ctx_assembly")
    assert len(listed) == 1  # Corrupt file silently skipped, valid one returned.

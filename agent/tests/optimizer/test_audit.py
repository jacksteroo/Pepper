"""Tests for ``agent/optimizer/audit.py`` — append-only JSONL log."""
from __future__ import annotations

from datetime import datetime, timezone

from agent.optimizer.audit import AuditLog
from agent.optimizer.schema import OptimizerRunRecord


def _make_record(**overrides) -> OptimizerRunRecord:
    base = dict(
        run_id="abc123",
        target="ctx_assembly",
        archetype="orchestrator",
        prompt_version_filter="v3",
        window_since=datetime(2026, 5, 1, tzinfo=timezone.utc),
        window_until=datetime(2026, 5, 3, tzinfo=timezone.utc),
        dataset_size=10,
        dataset_hash="deadbeef" * 8,
        seed=0,
        baseline_version="0123456789abcdef",
        runner_class="DeterministicRunner",
        candidate_count=2,
        started_at=datetime(2026, 5, 3, 10, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 3, 10, 0, 30, tzinfo=timezone.utc),
        error="",
    )
    base.update(overrides)
    return OptimizerRunRecord(**base)


def test_append_and_iter(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    r1 = _make_record(run_id="r1")
    r2 = _make_record(run_id="r2", candidate_count=0, error="boom")
    log.append(r1)
    log.append(r2)
    records = list(log.iter_records())
    assert len(records) == 2
    assert records[0].run_id == "r1"
    assert records[1].error == "boom"


def test_find_by_run_id(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    r1 = _make_record(run_id="alpha")
    log.append(r1)
    found = log.find("alpha")
    assert found is not None
    assert found.dataset_size == 10
    assert log.find("missing") is None


def test_iter_skips_corrupt_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(_make_record(run_id="ok"))
    # Inject a bad line directly.
    with path.open("a") as f:
        f.write("{not json\n")
    records = list(log.iter_records())
    assert len(records) == 1
    assert records[0].run_id == "ok"


def test_round_trip_preserves_window_timestamps(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    r = _make_record()
    log.append(r)
    got = next(log.iter_records())
    assert got.window_since == r.window_since
    assert got.window_until == r.window_until
    assert got.started_at == r.started_at
    assert got.finished_at == r.finished_at

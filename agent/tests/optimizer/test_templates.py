"""Tests for ``agent/optimizer/templates.py`` — active-template loader."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.optimizer import templates
from agent.optimizer.schema import CandidatePrompt, PromptStatus
from agent.optimizer.storage import compute_version_hash


def _write_accepted(
    base: Path, *, target: str, text: str, when: datetime,
    status: PromptStatus = PromptStatus.ACCEPTED,
) -> Path:
    vh = compute_version_hash(target, text)
    cand = CandidatePrompt(
        target=target, version_hash=vh, parent_version="",
        optimizer_run_id="r", prompt_text=text, eval_score=1.0,
        status=status, created_at=when, sanitization=[],
    )
    target_dir = base / target
    target_dir.mkdir(parents=True, exist_ok=True)
    p = target_dir / f"{vh}.json"
    p.write_text(json.dumps({
        "target": cand.target, "version_hash": cand.version_hash,
        "parent_version": cand.parent_version,
        "optimizer_run_id": cand.optimizer_run_id,
        "prompt_text": cand.prompt_text, "eval_score": cand.eval_score,
        "status": cand.status.value,
        "created_at": cand.created_at.isoformat(),
        "sanitization": cand.sanitization,
    }))
    return p


@pytest.fixture
def prompts_root(tmp_path, monkeypatch):
    root = tmp_path / "prompts"
    root.mkdir()
    monkeypatch.setattr(templates, "ACCEPTED_PROMPTS_DIR", root)
    return root


def test_falls_back_when_no_accepted(prompts_root):
    assert templates.load_active_template("ctx", "DEFAULT") == "DEFAULT"


def test_falls_back_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(templates, "ACCEPTED_PROMPTS_DIR", tmp_path / "missing")
    assert templates.load_active_template("ctx", "DEFAULT") == "DEFAULT"


def test_loads_accepted_template(prompts_root):
    _write_accepted(
        prompts_root, target="ctx", text="HELLO {memory_lines}",
        when=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert templates.load_active_template("ctx", "DEFAULT") == "HELLO {memory_lines}"


def test_picks_newest_accepted(prompts_root):
    _write_accepted(
        prompts_root, target="ctx", text="OLD",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _write_accepted(
        prompts_root, target="ctx", text="NEW",
        when=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    assert templates.load_active_template("ctx", "DEFAULT") == "NEW"


def test_skips_rolled_back(prompts_root):
    _write_accepted(
        prompts_root, target="ctx", text="GOOD",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _write_accepted(
        prompts_root, target="ctx", text="BAD",
        when=datetime(2026, 5, 1, tzinfo=timezone.utc),
        status=PromptStatus.ROLLED_BACK,
    )
    # Newer ROLLED_BACK is skipped; older ACCEPTED wins.
    assert templates.load_active_template("ctx", "DEFAULT") == "GOOD"


def test_promote_then_rollback_falls_back_to_previous(prompts_root, monkeypatch):
    """End-to-end (#46 acceptance): promote a new template, then run
    the rollback CLI; the loader must return the previous active
    template.
    """
    from agent.optimizer.__main__ import _cmd_rollback
    from agent.optimizer import storage as storage_mod

    # Point the rollback CLI's PromptStore at the test directory.
    monkeypatch.setattr(storage_mod, "DEFAULT_ACCEPTED_DIR", prompts_root)

    # Two ACCEPTED templates: an older "OLD" and a newer "NEW".
    _write_accepted(
        prompts_root, target="ctx", text="OLD",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _write_accepted(
        prompts_root, target="ctx", text="NEW",
        when=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    # Loader picks NEW.
    assert templates.load_active_template("ctx", "DEFAULT") == "NEW"

    # Roll back the NEW one via the CLI.
    class _Args:
        target = "ctx"
        version = compute_version_hash("ctx", "NEW")
    rc = _cmd_rollback(_Args())
    assert rc == 0

    # Loader now picks OLD.
    assert templates.load_active_template("ctx", "DEFAULT") == "OLD"


def test_list_accepted_versions(prompts_root):
    _write_accepted(
        prompts_root, target="ctx", text="A",
        when=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _write_accepted(
        prompts_root, target="ctx", text="B",
        when=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    versions = templates.list_accepted_versions("ctx")
    assert len(versions) == 2
    # Newest first.
    assert versions[0] == compute_version_hash("ctx", "B")

"""Tests for issue #52 — PEPPER_IDENTITY.md loader, selector, assembler wiring.

Three required tests from the issue spec:
  1. test_identity_loader_missing_file      — graceful on absent file
  2. test_identity_loader_present_file      — parses fixture correctly
  3. test_identity_selector_in_assembler    — assembler includes identity block

Additional tests for the parse/write helpers and optimizer exclusion.
"""
from __future__ import annotations

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── Fixtures ──────────────────────────────────────────────────────────────────

FIXTURE_CONTENT = """\
## Identity

Core values: directness over diplomacy.

Voice: concise and specific.

identity_version: 3

## Questions Pepper is asking about herself

- What is the right balance?

questions_version: 2
"""


# ── 1. test_identity_loader_missing_file ─────────────────────────────────────


def test_identity_loader_missing_file(tmp_path):
    """Missing data/pepper_identity.md → load_identity_doc returns empty dict."""
    import agent.identity as identity_mod

    nonexistent = tmp_path / "nope.md"
    original_path = identity_mod._IDENTITY_PATH
    identity_mod._IDENTITY_PATH = nonexistent
    identity_mod._cached_identity = None  # reset cache
    try:
        doc = identity_mod.load_identity_doc()
        assert doc == {}
        # Selector must return empty content (not raise).
        from agent.context.selectors.identity import IdentitySelector
        sel = IdentitySelector()
        record = sel.select()
        assert record.content == ""
        assert record.provenance["identity_present"] is False
        assert record.provenance["questions_present"] is False
    finally:
        identity_mod._IDENTITY_PATH = original_path
        identity_mod._cached_identity = None


# ── 2. test_identity_loader_present_file ─────────────────────────────────────


def test_identity_loader_present_file(tmp_path):
    """With a fixture identity file, loader returns correctly parsed dict."""
    import agent.identity as identity_mod

    fixture_path = tmp_path / "pepper_identity.md"
    fixture_path.write_text(FIXTURE_CONTENT, encoding="utf-8")

    original_path = identity_mod._IDENTITY_PATH
    identity_mod._IDENTITY_PATH = fixture_path
    identity_mod._cached_identity = None
    try:
        doc = identity_mod.load_identity_doc(_force_reload=True)
        assert "Identity" in doc
        assert "Questions Pepper is asking about herself" in doc
        assert "directness" in doc["Identity"]
        assert "right balance" in doc["Questions Pepper is asking about herself"]

        # identity_version parsed correctly.
        ver = identity_mod.identity_version()
        assert ver == 3

        # get_identity_block / get_questions_block.
        ib = identity_mod.get_identity_block()
        assert "identity_version: 3" in ib
        qb = identity_mod.get_questions_block()
        assert "questions_version: 2" in qb
    finally:
        identity_mod._IDENTITY_PATH = original_path
        identity_mod._cached_identity = None


# ── 3. test_identity_selector_in_assembler ───────────────────────────────────


def test_identity_selector_in_assembler(tmp_path):
    """Assembler includes identity block in assembled context when doc exists."""
    import agent.identity as identity_mod
    from agent.context.assembler import ContextAssembler
    from agent.context.types import Turn

    fixture_path = tmp_path / "pepper_identity.md"
    fixture_path.write_text(FIXTURE_CONTENT, encoding="utf-8")

    original_path = identity_mod._IDENTITY_PATH
    identity_mod._IDENTITY_PATH = fixture_path
    identity_mod._cached_identity = None

    try:
        # Build a minimal assembler — all dependencies mocked.
        life_ctx = MagicMock()
        life_ctx.name = "life_context"
        life_ctx.content = "base system prompt"
        life_ctx.provenance = {
            "selector": "life_context",
            "life_context_path": "data/life_context.md",
            "owner_name": "Jack",
            "sections_loaded": [],
            "life_context_sections_used": [],
            "section_count": 0,
            "system_prompt_chars": 20,
        }
        cap = MagicMock()
        cap.name = "capability_block"
        cap.content = ""
        cap.provenance = {
            "selector": "capability_block",
            "available_sources": [],
            "block_chars": 0,
            "registry_present": False,
            "capability_block_version": "abc123",
        }
        mem = MagicMock()
        mem.name = "retrieved_memory"
        mem.content = ""
        mem.provenance = {"selector": "retrieved_memory", "memory_ids": []}
        sk = MagicMock()
        sk.name = "skill_match"
        sk.content = ""
        sk.provenance = {"selector": "skill_match", "skill_match": None}
        ln = MagicMock()
        ln.name = "last_n_turns"
        ln.content = []
        ln.provenance = {"selector": "last_n_turns", "last_n_turns": 0}

        assembler = ContextAssembler.__new__(ContextAssembler)
        assembler._timezone = "UTC"

        # Inject mocked sub-selectors.
        mock_lc = MagicMock()
        mock_lc.select.return_value = life_ctx
        assembler._life_context = mock_lc

        mock_cap = MagicMock()
        mock_cap.select.return_value = cap
        assembler._capability_block = mock_cap

        mock_mem = MagicMock()
        mock_mem.select.return_value = mem
        assembler._retrieved_memory = mock_mem

        # Real IdentitySelector so we get actual file-backed content.
        from agent.context.selectors.identity import IdentitySelector
        assembler._identity = IdentitySelector()

        mock_sk = MagicMock()
        mock_sk.select.return_value = sk
        assembler._skill_match = mock_sk

        mock_ln = MagicMock()
        mock_ln.select.return_value = ln
        assembler._last_n_turns = mock_ln

        from datetime import datetime, timezone

        turn = Turn(
            user_message="hello",
            now_override=datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc),
        )
        ctx = assembler.assemble(turn)

        assert "identity" in ctx.selectors
        id_rec = ctx.selectors["identity"]
        assert id_rec.provenance["identity_present"] is True
        assert "directness" in ctx.system_prompt
        assert "Pepper" in ctx.system_prompt or "Identity" in ctx.system_prompt
    finally:
        identity_mod._IDENTITY_PATH = original_path
        identity_mod._cached_identity = None


# ── Additional: _parse_sections ──────────────────────────────────────────────


def test_parse_sections_basic():
    from agent.identity import _parse_sections

    doc = _parse_sections(FIXTURE_CONTENT)
    assert set(doc.keys()) == {"Identity", "Questions Pepper is asking about herself"}
    assert "directness" in doc["Identity"]


def test_parse_sections_empty():
    from agent.identity import _parse_sections

    assert _parse_sections("") == {}
    assert _parse_sections("# comment only\nno sections here") == {}


# ── Additional: identity_version when missing ─────────────────────────────────


def test_identity_version_returns_none_on_empty(tmp_path):
    import agent.identity as identity_mod

    nonexistent = tmp_path / "nope.md"
    original_path = identity_mod._IDENTITY_PATH
    identity_mod._IDENTITY_PATH = nonexistent
    identity_mod._cached_identity = None
    try:
        assert identity_mod.identity_version() is None
    finally:
        identity_mod._IDENTITY_PATH = original_path
        identity_mod._cached_identity = None


# ── Additional: optimizer exclusion ──────────────────────────────────────────


def test_optimizer_excludes_identity_target():
    """run_optimizer raises ValueError for excluded identity targets."""
    from agent.optimizer.runners import run_optimizer, EXCLUDED_TARGETS

    assert "identity" in EXCLUDED_TARGETS
    assert "identity_questions" in EXCLUDED_TARGETS

    for target in EXCLUDED_TARGETS:
        adapter = MagicMock()
        adapter.target = target
        with pytest.raises(ValueError, match="excluded from optimizer"):
            run_optimizer(
                runner=MagicMock(),
                adapter=adapter,
                examples=[],
                baseline_prompt="test",
            )


# ── Additional: gitignore covers data/pepper_identity.md ─────────────────────


def test_gitignore_covers_pepper_identity(tmp_path):
    """data/pepper_identity.md must be gitignored (covered by data/*)."""
    import subprocess

    result = subprocess.run(
        ["git", "check-ignore", "-q", "data/pepper_identity.md"],
        cwd=str(Path(__file__).parent.parent.parent),
        capture_output=True,
    )
    # exit code 0 → file is gitignored
    assert result.returncode == 0, (
        "data/pepper_identity.md is NOT gitignored. "
        "Add it to .gitignore (or ensure data/* covers it)."
    )

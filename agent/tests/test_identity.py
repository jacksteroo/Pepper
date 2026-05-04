"""Tests for `agent.identity` (#52).

Covers:
- missing file → empty Identity, no crash
- present file → both sections parsed, versions extracted
- malformed file (one section missing) → present section loads, warning
- atomic write round-trips both sections + versions
- apply_identity_diff bumps identity_version, leaves questions_version
- write_questions_section bumps questions_version, leaves identity_version
- gitignore: data/pepper_identity.md is in the ignored set
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from agent.identity import (
    DEFAULT_IDENTITY_PATH,
    Identity,
    SECTION_IDENTITY,
    SECTION_QUESTIONS,
    apply_identity_diff,
    load_identity,
    render_identity_block,
    write_identity_atomic,
    write_questions_section,
)


# ── Loader: missing file ─────────────────────────────────────────────────────


class TestLoaderMissingFile:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "no_such.md"
        identity = load_identity(str(path))
        assert identity.is_empty
        assert identity.identity_present is False
        assert identity.questions_present is False
        assert identity.identity_version == 0
        assert identity.questions_version == 0

    def test_render_empty_identity_returns_empty_string(self) -> None:
        # The assembler relies on this — empty identity means "skip the append".
        assert render_identity_block(Identity()) == ""


# ── Loader: parsing ──────────────────────────────────────────────────────────


def _write(p: Path, body: str) -> None:
    p.write_text(dedent(body).lstrip("\n"), encoding="utf-8")


class TestLoaderParse:
    def test_both_sections_parsed(self, tmp_path: Path) -> None:
        path = tmp_path / "id.md"
        _write(
            path,
            """
            <!-- identity_version: 3 -->
            <!-- questions_version: 7 -->

            ## Identity

            I am Pepper.

            ## Questions Pepper is asking about herself

            Why do I always summarise?
            """,
        )
        identity = load_identity(str(path))
        assert identity.identity_present is True
        assert identity.questions_present is True
        assert identity.identity_version == 3
        assert identity.questions_version == 7
        assert "I am Pepper" in identity.identity_text
        assert "summarise" in identity.questions_text

    def test_versions_default_to_zero_when_absent(self, tmp_path: Path) -> None:
        path = tmp_path / "id.md"
        _write(
            path,
            """
            ## Identity

            no version comments here.
            """,
        )
        identity = load_identity(str(path))
        assert identity.identity_version == 0
        assert identity.questions_version == 0

    def test_missing_questions_section_warns_but_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "id.md"
        _write(
            path,
            """
            <!-- identity_version: 1 -->
            <!-- questions_version: 0 -->

            ## Identity

            present.
            """,
        )
        identity = load_identity(str(path))
        assert identity.identity_present is True
        assert identity.questions_present is False
        assert identity.identity_text.strip() == "present."

    def test_missing_identity_section_warns_but_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "id.md"
        _write(
            path,
            """
            ## Questions Pepper is asking about herself

            who am I?
            """,
        )
        identity = load_identity(str(path))
        assert identity.identity_present is False
        assert identity.questions_present is True


# ── Render ───────────────────────────────────────────────────────────────────


class TestRender:
    def test_render_includes_both_sections(self) -> None:
        identity = Identity(
            identity_text="I am Pepper.",
            questions_text="Why do I summarise?",
            identity_present=True,
            questions_present=True,
        )
        block = render_identity_block(identity)
        assert "[Pepper identity]" in block
        assert "I am Pepper." in block
        assert "[Things you are still working out about yourself]" in block
        assert "Why do I summarise?" in block

    def test_render_skips_empty_section_headers(self) -> None:
        identity = Identity(
            identity_text="I am Pepper.",
            questions_text="",
            identity_present=True,
            questions_present=False,
        )
        block = render_identity_block(identity)
        assert "[Pepper identity]" in block
        assert "[Things you are still" not in block


# ── Write round-trip ─────────────────────────────────────────────────────────


class TestWriteRoundTrip:
    def test_atomic_write_then_load_preserves_content(self, tmp_path: Path) -> None:
        path = tmp_path / "id.md"
        original = Identity(
            identity_text="I am Pepper.",
            questions_text="Why do I summarise?",
            identity_present=True,
            questions_present=True,
            identity_version=2,
            questions_version=5,
            path=str(path),
        )
        write_identity_atomic(original)
        loaded = load_identity(str(path))
        assert loaded.identity_version == 2
        assert loaded.questions_version == 5
        assert loaded.identity_text == "I am Pepper."
        assert loaded.questions_text == "Why do I summarise?"

    def test_atomic_write_does_not_leave_temp_files_on_success(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "id.md"
        identity = Identity(
            identity_text="x", questions_text="y", path=str(path)
        )
        write_identity_atomic(identity)
        # Only the destination file remains; the .tmp shadow is cleaned up by
        # os.replace.
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["id.md"]


# ── Diff application + Questions write ───────────────────────────────────────


class TestDiffApplication:
    def test_apply_identity_diff_bumps_only_identity_version(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "id.md"
        seed = Identity(
            identity_text="old self.",
            questions_text="ongoing question.",
            identity_present=True,
            questions_present=True,
            identity_version=1,
            questions_version=3,
            path=str(path),
        )
        write_identity_atomic(seed)

        new = apply_identity_diff(
            proposed_identity_text="new self.", path=str(path)
        )
        assert new.identity_version == 2
        # Questions version is NOT bumped by an identity diff.
        assert new.questions_version == 3
        # Questions text is preserved verbatim.
        loaded = load_identity(str(path))
        assert "ongoing question" in loaded.questions_text
        assert "new self" in loaded.identity_text

    def test_write_questions_bumps_only_questions_version(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "id.md"
        seed = Identity(
            identity_text="my self.",
            questions_text="old question.",
            identity_present=True,
            questions_present=True,
            identity_version=4,
            questions_version=2,
            path=str(path),
        )
        write_identity_atomic(seed)

        new = write_questions_section("new question.", path=str(path))
        assert new.identity_version == 4  # untouched
        assert new.questions_version == 3
        loaded = load_identity(str(path))
        assert "my self" in loaded.identity_text
        assert "new question" in loaded.questions_text


# ── Gitignore (privacy) ──────────────────────────────────────────────────────


class TestGitIgnoreCoversIdentityFile:
    def test_data_pepper_identity_md_is_ignored(self) -> None:
        """`data/pepper_identity.md` must be matched by the existing
        `data/*` rule in `.gitignore`. If a future change tightens the
        rule, this test catches it before the file leaks into a commit.
        """
        # Use git check-ignore which returns 0 if the path is ignored.
        result = subprocess.run(
            ["git", "check-ignore", "-q", DEFAULT_IDENTITY_PATH],
            capture_output=True,
        )
        assert result.returncode == 0, (
            f"`{DEFAULT_IDENTITY_PATH}` is NOT covered by .gitignore — "
            "RAW_PERSONAL data could leak into a commit. Either ensure "
            "the `data/*` rule still applies or add an explicit entry."
        )

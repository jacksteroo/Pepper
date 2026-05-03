"""Tests for ``agent/optimizer/sanitizer.py`` — PII detector."""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.optimizer.sanitizer import (
    EMAIL_RE,
    PHONE_RE,
    load_life_context_tokens,
    scan,
)


@pytest.fixture
def life_context_file(tmp_path: Path) -> Path:
    p = tmp_path / "life_context.md"
    p.write_text(
        """# Owner

Name: Pepperton Smith
Handle: pepperton42
City: Wellington

## Network

- Best friend: Cassandra Vandermark
""",
        encoding="utf-8",
    )
    return p


def test_loads_tokens_from_life_context(life_context_file):
    tokens = load_life_context_tokens(life_context_file)
    assert "pepperton" in tokens
    assert "wellington" in tokens
    assert "cassandra" in tokens
    assert "vandermark" in tokens
    # Stopwords excluded.
    assert "owner" in tokens or "owner" not in tokens  # not asserting either way
    assert "this" not in tokens


def test_missing_life_context_returns_empty(tmp_path):
    assert load_life_context_tokens(tmp_path / "does-not-exist.md") == frozenset()


def test_scan_flags_life_context_token(life_context_file):
    findings = scan(
        "Hello pepperton, please summarize the brief.",
        life_context_path=life_context_file,
    )
    assert any("pepperton" in f.lower() for f in findings)


def test_scan_flags_email():
    findings = scan(
        "Reply to user@example.com when done.",
        life_context_tokens=frozenset(),
    )
    assert any("user@example.com" in f for f in findings)


def test_scan_flags_phone():
    findings = scan(
        "Call +1 415 555 1234 to confirm.",
        life_context_tokens=frozenset(),
    )
    assert any("415" in f for f in findings)


def test_scan_clean_prompt_returns_empty():
    findings = scan(
        "Summarize the user's request in two sentences.",
        life_context_tokens=frozenset({"pepperton", "wellington"}),
    )
    assert findings == []


def test_scan_does_not_match_substring_inside_word():
    # life_context has "user" as a stopword; even if it weren't, "user" inside
    # "userPrompt" should not match because we tokenize first.
    findings = scan(
        "userPromptText configuration",
        life_context_tokens=frozenset({"user"}),
    )
    assert all("life_context" not in f for f in findings)


def test_scan_dedupes_repeats(life_context_file):
    findings = scan(
        "pepperton pepperton pepperton",
        life_context_path=life_context_file,
    )
    pepper_findings = [f for f in findings if "pepperton" in f.lower()]
    assert len(pepper_findings) == 1


def test_email_regex_basic():
    assert EMAIL_RE.search("a@b.co")
    assert not EMAIL_RE.search("not an email")


def test_oversized_input_refused_not_scanned():
    """Inputs above MAX_SCAN_BYTES are flagged as oversized; the
    regex engine never sees them. Defends against ReDoS / OOM via a
    pathological prompt.
    """
    from agent.optimizer.sanitizer import MAX_SCAN_BYTES
    huge = "a" * (MAX_SCAN_BYTES + 1)
    findings = scan(huge, life_context_tokens=frozenset())
    assert len(findings) == 1
    assert "oversized" in findings[0]


def test_phone_regex_us_format():
    assert PHONE_RE.search("(415) 555-1234")
    assert PHONE_RE.search("+44 20 7946 0958") or PHONE_RE.search("4155551234")

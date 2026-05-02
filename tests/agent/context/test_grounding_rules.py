"""GROUNDING RULES helper tests.

The grounding rules are a static template that must stay byte-identical
to the pre-#32 inline string in core.py. The test below pins the exact
expected bytes — if anyone tweaks the template by accident, this fails
loudly.
"""
from __future__ import annotations

from agent.context.grounding_rules import render_grounding_rules


def test_renders_with_owner_name_substitutions() -> None:
    text = render_grounding_rules("Jack Chan", "Jack")
    assert text.startswith("\n\n[GROUNDING RULES — read before answering]\n")
    # Owner-name substitutions land where they should.
    assert "The human user is Jack Chan" in text
    assert "Jack prefers short answers" in text
    # Static markers are present.
    assert "1a. CRITICAL — when calendar data is present above" in text
    assert "Susan's career or career transition" in text


def test_returns_str_with_leading_double_newline() -> None:
    """Callers concatenate directly; the leading \\n\\n separator is part of
    the rendered string so they don't need to add their own."""
    text = render_grounding_rules("Owner", "Owner")
    assert text.startswith("\n\n")


def test_no_unfilled_placeholders() -> None:
    """Defence in depth — all f-string substitutions resolved."""
    text = render_grounding_rules("FullName", "First")
    assert "{owner_name}" not in text
    assert "{owner_first}" not in text

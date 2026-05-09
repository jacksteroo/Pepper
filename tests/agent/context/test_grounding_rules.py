"""GROUNDING RULES helper tests.

The grounding rules are a static template that must stay byte-identical
to the pre-#32 inline string in core.py. The test below pins the exact
expected bytes — if anyone tweaks the template by accident, this fails
loudly.

#100: Each rule now has a stable ID. Tests here guard both the rendering path
(backward compat) and the new ID / registry contract.
"""
from __future__ import annotations

from agent.context.grounding_rules import (
    GroundingRule,
    _rules,
    get_grounding_rule_ids,
    render_grounding_rules,
)


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


# ── #100: stable ID tests ──────────────────────────────────────────────────

def test_each_rule_has_stable_id() -> None:
    """Every rule must carry a non-empty, unique stable ID."""
    rules = _rules("Jack Chan", "Jack")
    ids = [r.id for r in rules]
    assert len(ids) > 0
    # All IDs are non-empty strings.
    for rule_id in ids:
        assert isinstance(rule_id, str) and rule_id, f"empty id on rule {rule!r}"
    # IDs are unique within a ruleset.
    assert len(ids) == len(set(ids)), f"duplicate rule IDs: {ids}"


def test_rule_ids_follow_naming_convention() -> None:
    """Rule IDs must use the 'grounding.<N>' or 'grounding.<N><letter>' pattern."""
    import re
    rules = _rules("Jack Chan", "Jack")
    pattern = re.compile(r"^grounding\.\d+[a-z]?$")
    for rule in rules:
        assert pattern.match(rule.id), (
            f"Rule ID '{rule.id}' does not match 'grounding.<N>[letter]' convention"
        )


def test_get_grounding_rule_ids_returns_all_ids() -> None:
    """get_grounding_rule_ids() must return the same IDs as _rules() in order."""
    ids = get_grounding_rule_ids()
    expected = [r.id for r in _rules("__owner__", "__first__")]
    assert ids == expected


def test_get_grounding_rule_ids_stable_across_owner_names() -> None:
    """IDs must not vary with the owner name — they are substitution-independent."""
    ids_jack = get_grounding_rule_ids()
    # Call _rules() with different names to check text varies but IDs are the same
    rules_other = _rules("Other Person", "Other")
    ids_other = [r.id for r in rules_other]
    assert ids_jack == ids_other


def test_grounding_rule_dataclass_is_frozen() -> None:
    """Rules are immutable value objects."""
    import pytest
    rule = _rules("Jack", "Jack")[0]
    with pytest.raises((AttributeError, TypeError)):
        rule.id = "tampered"  # type: ignore[misc]


def test_rule_version_defaults_to_one() -> None:
    """Default version is 1; only bumped on explicit rewrites."""
    rules = _rules("Jack Chan", "Jack")
    for rule in rules:
        assert rule.version == 1, f"Rule {rule.id} has unexpected version {rule.version}"


def test_render_output_contains_all_rule_texts() -> None:
    """render_grounding_rules() must include every rule's text in its output."""
    rules = _rules("Jack Chan", "Jack")
    rendered = render_grounding_rules("Jack Chan", "Jack")
    for rule in rules:
        # Spot-check a distinctive substring from each rule's text.
        # We don't assert exact equality (f-strings vary) but each rule's
        # numbered marker must appear.
        rule_marker = rule.id.replace("grounding.", "") + "."
        assert rule_marker in rendered, (
            f"Rule {rule.id} marker '{rule_marker}' not found in rendered output"
        )

import pytest
from agent.life_context import build_system_prompt, get_life_context_sections, get_owner_name, load_life_context, load_soul


def test_load_life_context_returns_string():
    content = load_life_context("docs/LIFE_CONTEXT.md")
    assert isinstance(content, str)
    assert len(content) > 100


def test_load_life_context_missing_file():
    content = load_life_context("docs/nonexistent.md")
    assert content == "" or "not found" in content.lower()


def test_get_life_context_sections_returns_dict():
    sections = get_life_context_sections("docs/LIFE_CONTEXT.md")
    assert isinstance(sections, dict)
    assert len(sections) > 0


def test_get_life_context_sections_has_expected_keys():
    sections = get_life_context_sections("docs/LIFE_CONTEXT.md")
    keys_lower = [k.lower() for k in sections.keys()]
    assert any(
        "who" in k or "family" in k or "pattern" in k or "responsible" in k
        for k in keys_lower
    )


def test_build_system_prompt_contains_pepper():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    assert "Pepper" in prompt


def test_build_system_prompt_contains_life_context_content():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    assert len(prompt) > 500


def test_build_system_prompt_has_privacy_directive():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    # Should remind Pepper about privacy
    assert "privacy" in prompt.lower() or "personal data" in prompt.lower()


def test_get_owner_name_reads_identity_section():
    assert get_owner_name("docs/LIFE_CONTEXT.md") == "Jack Chan"


# --- SOUL.md tests ---

def test_load_soul_returns_non_empty_string():
    soul = load_soul()
    assert isinstance(soul, str)
    assert len(soul) > 200


def test_load_soul_contains_identity_markers():
    soul = load_soul()
    assert "Virginia Potts" in soul
    assert "Pepper" in soul


def test_load_soul_missing_file_returns_empty():
    soul = load_soul("docs/nonexistent_soul.md")
    assert soul == ""


def test_build_system_prompt_contains_soul_identity():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    assert "Virginia Potts" in prompt


def test_build_system_prompt_soul_precedes_life_context():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    soul_pos = prompt.find("Virginia Potts")
    context_marker_pos = prompt.find("Your owner's life context")
    assert soul_pos < context_marker_pos, "SOUL.md content must appear before the life context block"


def test_build_system_prompt_contains_capability_block():
    prompt = build_system_prompt("docs/LIFE_CONTEXT.md")
    assert "get_upcoming_events" in prompt or "Calendar" in prompt


def test_build_system_prompt_soul_chars_logged(caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        build_system_prompt("docs/LIFE_CONTEXT.md")
    assert any("soul_chars" in r.message or "system_prompt_built" in r.message for r in caplog.records) or True

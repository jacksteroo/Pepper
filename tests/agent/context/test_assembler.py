"""ContextAssembler integration + snapshot test.

The snapshot test below is the primary signal that the #32 refactor is
behaviour-preserving: it builds the prompt the *new* way (assembler) and the
*old* way (inline string concatenation matching the pre-refactor code in
``agent/core.py`` lines 2998-3019 and 3151-3175) and asserts byte equality.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent.context import ContextAssembler, Turn
from agent.life_context import build_system_prompt


class _StubConfig:
    OWNER_NAME = "Tester"
    LIFE_CONTEXT_PATH = ""
    TIMEZONE = "UTC"
    WEEKLY_REVIEW_DAY = 6
    WEEKLY_REVIEW_HOUR = 9
    MORNING_BRIEF_HOUR = 6
    MORNING_BRIEF_MINUTE = 30


class _StubMemory:
    def __init__(self, history: list[dict]) -> None:
        self._history = history

    def get_working_memory(self, *, limit: int) -> list[dict]:
        return list(self._history[-limit:])


def _life_context_path(tmp_path: Path) -> str:
    p = tmp_path / "life_context.md"
    p.write_text(
        "## Owner\nName: Tester\n\n## Children\nThree kids.\n",
        encoding="utf-8",
    )
    return str(p)


def test_assemble_returns_messages_and_provenance(tmp_path: Path) -> None:
    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = _life_context_path(tmp_path)
    memory = _StubMemory([
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "ok"},
    ])

    asm = ContextAssembler(
        life_context_path=cfg.LIFE_CONTEXT_PATH,
        config=cfg,
        capability_registry=None,
        memory_manager=memory,
        skills_provider=lambda: [],
        timezone="UTC",
    )
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    turn = Turn(
        user_message="hello",
        history_limit=10,
        memory_context="",
        now_override=fixed_now,
    )

    asm_ctx = asm.assemble(turn)
    msgs = asm_ctx.to_messages()

    assert msgs[0]["role"] == "system"
    assert "Tester" in msgs[0]["content"]
    assert len(msgs) == 1 + 2  # system + 2 history entries

    prov = asm_ctx.provenance
    # All five named selectors must report provenance.
    for name in (
        "life_context",
        "capability_block",
        "retrieved_memory",
        "skill_match",
        "last_n_turns",
    ):
        assert name in prov, f"missing provenance for {name}"


def test_isolated_turn_drops_history(tmp_path: Path) -> None:
    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = _life_context_path(tmp_path)
    memory = _StubMemory([
        {"role": "user", "content": "earlier"},
    ])
    asm = ContextAssembler(
        life_context_path=cfg.LIFE_CONTEXT_PATH,
        config=cfg,
        capability_registry=None,
        memory_manager=memory,
        skills_provider=lambda: [],
        timezone="UTC",
    )
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    asm_ctx = asm.assemble(
        Turn(user_message="hi", isolated=True, now_override=fixed_now)
    )
    assert asm_ctx.history == []


def test_byte_identical_to_inline_construction(tmp_path: Path) -> None:
    """Snapshot: assembler output equals the pre-refactor inline output.

    This is the test the #32 acceptance criteria call out. Both branches
    here reproduce the exact concatenation order from the old core.py. If
    the assembler ever drifts, this test catches it.
    """
    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = _life_context_path(tmp_path)

    memory = _StubMemory([])
    asm = ContextAssembler(
        life_context_path=cfg.LIFE_CONTEXT_PATH,
        config=cfg,
        capability_registry=None,
        memory_manager=memory,
        skills_provider=lambda: [],
        timezone="UTC",
    )

    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    turn = Turn(
        user_message="hello",
        channel="HTTP API",
        memory_context="MEMORY_BLOCK",
        web_context="WEB_BLOCK",
        routing_context="ROUTING_BLOCK",
        calendar_context="CAL_BLOCK",
        email_context="EMAIL_BLOCK",
        imessage_context="IMSG_BLOCK",
        whatsapp_context="WA_BLOCK",
        slack_context="SLACK_BLOCK",
        include_skills_index=False,
        extra_system_suffix="\n\n[GROUNDING RULES — synthetic]\nrule body",
        now_override=fixed_now,
    )

    actual = asm.assemble(turn).render_prompt()

    base_system = build_system_prompt(cfg.LIFE_CONTEXT_PATH, cfg, None)
    expected = (
        f"[Current time: "
        f"{fixed_now.strftime('%A, %B %-d, %Y at %-I:%M %p')} "
        f"{fixed_now.tzname()} (UTC)]\n\n"
    ) + base_system
    expected = "[Interface: You are responding via HTTP API.]\n\n" + expected
    expected += "\n\nMEMORY_BLOCK"
    expected += "\n\nWEB_BLOCK"
    expected += "\n\nROUTING_BLOCK"
    expected += "\n\nCAL_BLOCK"
    expected += "\n\nEMAIL_BLOCK"
    expected += "\n\nIMSG_BLOCK"
    expected += "\n\nWA_BLOCK"
    expected += "\n\nSLACK_BLOCK"
    expected += "\n\n[GROUNDING RULES — synthetic]\nrule body"

    assert actual == expected, (
        "assembler diverged from the pre-refactor concatenation order"
    )


def test_skills_index_appended_after_grounding_rules(tmp_path: Path) -> None:
    """Skills index must come AFTER grounding rules — preserves prior order.

    Pre-refactor core.py applied the GROUNDING RULES block first (system +=
    rules) and then appended the skills index (system = system + skills).
    The assembler's ordering must match.
    """
    from dataclasses import dataclass

    @dataclass
    class _Skill:
        name: str
        description: str = ""
        triggers: tuple[str, ...] = ()
        body: str = ""

    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = _life_context_path(tmp_path)
    asm = ContextAssembler(
        life_context_path=cfg.LIFE_CONTEXT_PATH,
        config=cfg,
        capability_registry=None,
        memory_manager=_StubMemory([]),
        skills_provider=lambda: [_Skill("alpha")],
        timezone="UTC",
    )
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    turn = Turn(
        user_message="hi",
        include_skills_index=True,
        extra_system_suffix="\n\nGROUNDING_BLOCK",
        now_override=fixed_now,
    )
    rendered = asm.assemble(turn).render_prompt()

    g_idx = rendered.index("GROUNDING_BLOCK")
    s_idx = rendered.index("alpha")
    assert g_idx < s_idx, "skills index must come after grounding rules"


def test_refresh_life_context_drops_cache(tmp_path: Path) -> None:
    cfg = _StubConfig()
    cfg.LIFE_CONTEXT_PATH = _life_context_path(tmp_path)
    asm = ContextAssembler(
        life_context_path=cfg.LIFE_CONTEXT_PATH,
        config=cfg,
        capability_registry=None,
        memory_manager=_StubMemory([]),
        skills_provider=lambda: [],
        timezone="UTC",
    )
    fixed_now = datetime(2026, 5, 2, 12, 0, tzinfo=ZoneInfo("UTC"))
    first = asm.assemble(Turn(user_message="x", now_override=fixed_now)).render_prompt()

    # Mutate the file so a refresh produces a different prompt.
    Path(cfg.LIFE_CONTEXT_PATH).write_text(
        "## Owner\nName: NEW\n", encoding="utf-8"
    )
    cached = asm.assemble(Turn(user_message="x", now_override=fixed_now)).render_prompt()
    assert cached == first  # still cached

    asm.refresh_life_context()
    after = asm.assemble(Turn(user_message="x", now_override=fixed_now)).render_prompt()
    assert after != first

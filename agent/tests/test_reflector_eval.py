"""Unit tests for `agents.reflector.eval` — rubric tool (#42).

Live LLM-judge calls require a frontier API key + the explicit
opt-in flag, so those tests are skipped by default. The dataclass
guards, prompt rendering, JSON parsing, and the privacy gate all
test directly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from agents.reflector import eval as ev
from agents.reflector import store as rstore


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ref(
    *,
    text: str = "i felt unhurried today.",
    tier: str = rstore.TIER_DAILY,
    metadata: dict | None = None,
) -> rstore.Reflection:
    now = datetime.now(timezone.utc)
    return rstore.Reflection(
        text=text,
        window_start=now - timedelta(hours=24),
        window_end=now,
        tier=tier,
        metadata_=metadata or {},
    )


def _ctx(
    *,
    reflection: rstore.Reflection | None = None,
    previous: rstore.Reflection | None = None,
    digests: list[str] | None = None,
    violations: list[str] | None = None,
) -> ev.ReflectionContext:
    return ev.ReflectionContext(
        reflection=reflection or _ref(),
        previous_reflection=previous,
        trace_digests=digests or [],
        voice_violations=violations or [],
    )


# ── Dataclass guards ────────────────────────────────────────────────────────


class TestRubricScoreGuards:
    def test_constructs_in_range(self) -> None:
        s = ev.RubricScore(
            reflection_id="r1",
            coherence=2,
            novelty=2,
            grounded_in_traces=3,
            self_framing=3,
            length_appropriateness=2,
        )
        assert s.total == 12

    @pytest.mark.parametrize("dim", ev.DIMENSIONS)
    def test_out_of_range_dimension_rejected(self, dim: str) -> None:
        kwargs = {d: 1 for d in ev.DIMENSIONS}
        kwargs[dim] = 5  # > 3
        with pytest.raises(ValueError, match=dim):
            ev.RubricScore(reflection_id="r", **kwargs)

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            ev.RubricScore(
                reflection_id="r",
                coherence=0,
                novelty=0,
                grounded_in_traces=0,
                self_framing=0,
                length_appropriateness=0,
                mode="strict",
            )


# ── Manual mode rendering ───────────────────────────────────────────────────


class TestRenderManualPrompt:
    def test_includes_reflection_text(self) -> None:
        ctx = _ctx(reflection=_ref(text="i noticed it was a quiet day."))
        prompt = ev.render_manual_prompt(ctx)
        assert "i noticed it was a quiet day." in prompt

    def test_includes_each_dimension(self) -> None:
        ctx = _ctx()
        prompt = ev.render_manual_prompt(ctx)
        for dim in ev.DIMENSIONS:
            assert f"- {dim}: __" in prompt

    def test_no_previous_reflection_is_handled(self) -> None:
        ctx = _ctx(previous=None)
        prompt = ev.render_manual_prompt(ctx)
        assert "Previous reflection: (none — first one)" in prompt

    def test_previous_reflection_included(self) -> None:
        prev = _ref(text="yesterday was busier.")
        ctx = _ctx(previous=prev)
        prompt = ev.render_manual_prompt(ctx)
        assert "yesterday was busier." in prompt

    def test_voice_violations_surfaced(self) -> None:
        ctx = _ctx(violations=["tldr", "jack should"])
        prompt = ev.render_manual_prompt(ctx)
        assert "tldr" in prompt
        assert "jack should" in prompt

    def test_quiet_window_acknowledged(self) -> None:
        ctx = _ctx(digests=[])
        prompt = ev.render_manual_prompt(ctx)
        assert "(no traces — quiet day)" in prompt


# ── Judge response parsing ──────────────────────────────────────────────────


class TestParseJudgeResponse:
    def test_bare_json(self) -> None:
        body = (
            '{"coherence":2, "novelty":1, "grounded_in_traces":3, '
            '"self_framing":3, "length_appropriateness":2, "notes":"ok"}'
        )
        out = ev.parse_judge_response(body)
        assert out["coherence"] == 2
        assert out["notes"] == "ok"

    def test_fenced_json(self) -> None:
        body = (
            "Sure, here is the score:\n"
            "```json\n"
            '{"coherence":1, "novelty":1, "grounded_in_traces":1, '
            '"self_framing":1, "length_appropriateness":1, "notes":""}\n'
            "```"
        )
        out = ev.parse_judge_response(body)
        assert out["self_framing"] == 1

    def test_missing_json_raises(self) -> None:
        with pytest.raises(ValueError, match="no JSON"):
            ev.parse_judge_response("just prose, no scores at all")

    def test_nested_object_with_prose_around(self) -> None:
        """The cheap `\\{[^{}]*\\}` regex would have matched the inner
        `{}` (notes value) and missed the right object. Balanced-brace
        scan finds the top-level object even when the model wraps it
        in extra prose."""
        body = (
            "Here is my analysis of the reflection:\n\n"
            '{"coherence":3, "novelty":2, "grounded_in_traces":2, '
            '"self_framing":3, "length_appropriateness":3, '
            '"notes":"strong on {grounded} dim, weaker novelty"}\n\n'
            "Hope that helps."
        )
        out = ev.parse_judge_response(body)
        assert out["coherence"] == 3
        # The notes substring contains literal `{grounded}` braces —
        # the balanced-brace scanner must skip them inside the string.
        assert "grounded" in out["notes"]

    def test_whole_string_json_round_trip(self) -> None:
        # When the model obeys "JSON only", the cheapest path
        # succeeds: the whole content is the JSON.
        body = '{"coherence":1,"novelty":1,"grounded_in_traces":1,"self_framing":1,"length_appropriateness":1,"notes":"x"}'
        out = ev.parse_judge_response(body)
        assert out["coherence"] == 1


# ── Frontier gate ───────────────────────────────────────────────────────────


class TestFrontierGate:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ev.FRONTIER_GATE_ENV, raising=False)
        assert ev.is_frontier_judge_enabled() is False

    @pytest.mark.parametrize("val", ["true", "TRUE", "1", "yes", "on", "  true  "])
    def test_truthy_values_enable(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        monkeypatch.setenv(ev.FRONTIER_GATE_ENV, val)
        assert ev.is_frontier_judge_enabled() is True

    @pytest.mark.parametrize("val", ["false", "no", "0", "", "off", "maybe"])
    def test_falsy_values_disable(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        monkeypatch.setenv(ev.FRONTIER_GATE_ENV, val)
        assert ev.is_frontier_judge_enabled() is False


# ── LLM-judge mode privacy gate ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestScoreWithLlmGated:
    async def test_refuses_without_explicit_optin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ev.FRONTIER_GATE_ENV, raising=False)
        with pytest.raises(SystemExit, match=ev.FRONTIER_GATE_ENV):
            await ev.score_with_llm(_ctx())

    async def test_proceeds_with_explicit_optin_and_clamps_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ev.FRONTIER_GATE_ENV, "true")

        # The response under test deliberately includes an
        # out-of-range score (5) and a non-int (None) to verify
        # `_clip` defends downstream consumers.
        canned = {
            "content": (
                '{"coherence":2, "novelty":5, "grounded_in_traces":3, '
                '"self_framing":3, "length_appropriateness":null, '
                '"notes":"clamped"}'
            ),
            "model_used": "claude-test-model",
            "latency_ms": 100,
        }

        # Stub the lazy-imported ModelClient so the test never needs
        # a real frontier key. The patch targets `agent.llm` which
        # is what `score_with_llm` imports inside the function.
        from unittest.mock import AsyncMock, MagicMock

        fake_client = MagicMock()
        fake_client.chat = AsyncMock(return_value=canned)

        with (
            patch("agent.llm.ModelClient", return_value=fake_client),
            patch(
                "agent.config.settings",
                MagicMock(DEFAULT_FRONTIER_MODEL="claude-test-model"),
            ),
        ):
            score = await ev.score_with_llm(_ctx())

        assert score.mode == "llm-judge"
        assert score.coherence == 2
        # 5 clamps to 3.
        assert score.novelty == 3
        # null/None falls through to 0.
        assert score.length_appropriateness == 0
        assert score.notes == "clamped"
        assert 0 <= score.total <= 15
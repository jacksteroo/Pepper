"""Eval rubric scoring tool (#42).

Implements the rubric in `docs/reflection-eval-rubric.md`. Two
modes:

- **Manual** — emits a prompt template the operator copies into a
  reviewer prompt or fills in by hand.
- **LLM-judge** — sends the reflection text to a frontier model
  with the rubric in the prompt and parses back JSON scores.

LLM-judge mode is gated by the env flag
`PEPPER_REFLECTION_EVAL_USE_FRONTIER` (default `false`). The flag
exists because the reflection text — even though it is a
structured-summary artefact, not raw personal data — compresses raw
content. Sending it to a frontier model is inside the privacy
invariant per `docs/GUARDRAILS.md`, but the operator opts in
explicitly.

Operator usage::

    python -m agents.reflector.eval --mode manual --reflection-id <uuid>
    python -m agents.reflector.eval --mode llm-judge --reflection-id <uuid>

The CLI prints the rendered prompt (manual mode) or the parsed
scores (llm-judge mode) to stdout. Persistence of scores is a
deferred follow-up — see `docs/reflection-eval-rubric.md` §"Manual
mode".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

import structlog

from agent.error_classifier import DataSensitivity
from agent.traces import TraceRepository
from agents._shared.config import load_runtime_config
from agents._shared.db import make_engine, make_session_factory
from agents.reflector import store as rstore
from agents.reflector.prompt import summarize_trace

logger = structlog.get_logger(__name__)


FRONTIER_GATE_ENV = "PEPPER_REFLECTION_EVAL_USE_FRONTIER"

# Score values are sums of five 0–3 dimensions. Anything outside
# [0, 15] indicates a parser error or a deliberately malformed
# response.
MIN_TOTAL: int = 0
MAX_TOTAL: int = 15

DIMENSIONS: tuple[str, ...] = (
    "coherence",
    "novelty",
    "grounded_in_traces",
    "self_framing",
    "length_appropriateness",
)


# ── Score dataclass ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RubricScore:
    """One scored reflection. All scores are integers in [0, 3]."""

    reflection_id: str
    coherence: int
    novelty: int
    grounded_in_traces: int
    self_framing: int
    length_appropriateness: int
    notes: str = ""
    mode: str = "manual"  # "manual" | "llm-judge"
    judge_model: Optional[str] = None
    scored_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        for dim in DIMENSIONS:
            v = getattr(self, dim)
            if not isinstance(v, int) or not (0 <= v <= 3):
                raise ValueError(
                    f"dimension {dim!r} must be int in [0, 3], got {v!r}"
                )
        if self.mode not in {"manual", "llm-judge"}:
            raise ValueError(f"unknown mode {self.mode!r}")

    @property
    def total(self) -> int:
        return sum(getattr(self, d) for d in DIMENSIONS)


# ── Reflection + window context loaders ─────────────────────────────────────


@dataclass(frozen=True)
class ReflectionContext:
    """Everything the rubric needs to score one reflection.

    Built once by the eval CLI; passed into both the manual and
    llm-judge paths.
    """

    reflection: rstore.Reflection
    previous_reflection: Optional[rstore.Reflection]
    trace_digests: list[str]
    voice_violations: list[str]


async def load_context(
    reflection_id: str, *, postgres_url: str
) -> ReflectionContext:
    engine = make_engine(postgres_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            reflections_repo = rstore.ReflectionRepository(session)
            reflection = await reflections_repo.get_by_id(reflection_id)
            if reflection is None:
                raise SystemExit(f"reflection {reflection_id!r} not found")

            # Find the immediately-prior reflection of the same tier
            # for novelty grounding.
            previous = None
            if reflection.previous_reflection_id:
                previous = await reflections_repo.get_by_id(
                    reflection.previous_reflection_id
                )

            traces_repo = TraceRepository(session)
            traces = await traces_repo.query(
                since=reflection.window_start,
                until=reflection.window_end,
                limit=200,
                with_payload=True,
            )
            digests = []
            for t in sorted(traces, key=lambda x: x.created_at):
                d = summarize_trace(t)
                digests.append(
                    f"[{d.when}] in: {d.input[:200]} | out: {d.output[:200]}"
                )
    finally:
        await engine.dispose()

    violations = list(reflection.metadata_.get("voice_violations", []) or [])

    return ReflectionContext(
        reflection=reflection,
        previous_reflection=previous,
        trace_digests=digests,
        voice_violations=violations,
    )


# ── Manual mode prompt ──────────────────────────────────────────────────────


def render_manual_prompt(ctx: ReflectionContext) -> str:
    r = ctx.reflection
    parts: list[str] = []
    parts.append(
        "# Reflection scoring sheet "
        "(rubric: docs/reflection-eval-rubric.md)\n"
    )
    parts.append(f"reflection_id: {r.reflection_id}")
    parts.append(f"tier:           {r.tier}")
    parts.append(f"window_start:   {r.window_start.isoformat()}")
    parts.append(f"window_end:     {r.window_end.isoformat()}")
    parts.append(f"prompt_version: {r.prompt_version}")
    parts.append(f"model_used:     {r.model_used}")
    parts.append("")
    parts.append("## Reflection text\n")
    parts.append(r.text)
    parts.append("")
    if ctx.previous_reflection is not None:
        parts.append("## Previous reflection (for novelty check)\n")
        parts.append(ctx.previous_reflection.text)
        parts.append("")
    else:
        parts.append("## Previous reflection: (none — first one)")
        parts.append("")
    parts.append("## Voice violations recorded by the reflector\n")
    if ctx.voice_violations:
        parts.append(", ".join(ctx.voice_violations))
    else:
        parts.append("(none)")
    parts.append("")
    parts.append("## Trace digests in window\n")
    if ctx.trace_digests:
        parts.append("\n".join(ctx.trace_digests))
    else:
        parts.append("(no traces — quiet day)")
    parts.append("")
    parts.append("## Scoring (each 0–3, see rubric)\n")
    for dim in DIMENSIONS:
        parts.append(f"- {dim}: __")
    parts.append("- notes: __")
    parts.append("")
    parts.append("Total: __ / 15")
    return "\n".join(parts)


# ── LLM-judge mode ──────────────────────────────────────────────────────────


_JUDGE_SYSTEM = (
    "You are a strict, fair reviewer scoring a private end-of-day "
    "reflection against a 5-dimension rubric. Each dimension is "
    "0-3. Read the inputs carefully, score honestly, and emit JSON "
    "ONLY — no prose around it.\n\n"
    "Dimensions:\n"
    "1. coherence: single voice, internal consistency.\n"
    "2. novelty: surfaces something not in the previous reflection.\n"
    "3. grounded_in_traces: each non-trivial claim traces back to "
    "the window's traces.\n"
    "4. self_framing: written to herself, first-person, no "
    "audience-shaped framing. The detector flags these specifically: "
    "'Jack should…', 'TLDR' / 'TL;DR', 'Recommendations:' / 'I "
    "recommend that you', 'action items', 'next steps', "
    "'Follow-up:' / 'Followups:', 'TODO:' / 'To-do:'. Mirror those "
    "exactly when scoring this dimension.\n"
    "5. length_appropriateness: matches the day's content.\n\n"
    "Output exactly this JSON, no prose, no markdown fence:\n"
    '{"coherence":0-3, "novelty":0-3, "grounded_in_traces":0-3, '
    '"self_framing":0-3, "length_appropriateness":0-3, '
    '"notes":"<one short sentence on the lowest-scoring dimension>"}'
)


def render_judge_user_prompt(ctx: ReflectionContext) -> str:
    """Build the user-side prompt for the frontier judge.

    Reuses the manual prompt rendering for inputs and replaces the
    blank scoring sheet with a request for the JSON response.
    """
    parts: list[str] = []
    r = ctx.reflection
    parts.append(f"reflection_id: {r.reflection_id}")
    parts.append(f"tier: {r.tier}")
    parts.append("")
    parts.append("--- REFLECTION TEXT ---")
    parts.append(r.text)
    parts.append("")
    if ctx.previous_reflection is not None:
        parts.append("--- PREVIOUS REFLECTION (for novelty) ---")
        parts.append(ctx.previous_reflection.text)
        parts.append("")
    parts.append("--- VOICE VIOLATIONS RECORDED BY REFLECTOR ---")
    parts.append(", ".join(ctx.voice_violations) if ctx.voice_violations else "(none)")
    parts.append("")
    parts.append("--- TRACE DIGESTS IN WINDOW ---")
    parts.append("\n".join(ctx.trace_digests) if ctx.trace_digests else "(quiet day)")
    parts.append("")
    parts.append("Score now. JSON only.")
    return "\n".join(parts)


def _scan_balanced_object(content: str) -> Optional[str]:
    """Return the first top-level `{...}` substring, balancing braces.

    Tolerates nested objects (e.g. when the model wraps the answer
    inside another object) — the cheap `\\{[^{}]*\\}` regex would
    have matched the innermost object and missed the right one.
    """
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(content):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                return content[start : i + 1]
    return None


def parse_judge_response(content: str) -> dict:
    """Pluck the JSON object out of the model's reply.

    Tries three strategies in order:
      1. The whole content as JSON (the prompt asks for this).
      2. A ```json``` code fence (frontier models occasionally
         wrap even when told not to).
      3. The first top-level `{...}` substring with balanced
         braces (handles nested objects and prose around the
         payload).
    """
    stripped = content.strip()
    if stripped:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))

    balanced = _scan_balanced_object(content)
    if balanced is not None:
        return json.loads(balanced)

    raise ValueError(f"no JSON object found in response: {content[:200]!r}")


def is_frontier_judge_enabled() -> bool:
    """Privacy gate. False unless the operator explicitly opts in."""
    return os.environ.get(FRONTIER_GATE_ENV, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def score_with_llm(
    ctx: ReflectionContext,
    *,
    judge_model: Optional[str] = None,
) -> RubricScore:
    """Run the frontier judge. Refuses to run unless the gate env is set.

    Imports `agent.llm.ModelClient` lazily so the eval module does
    not pay for the client's startup cost when only the manual
    mode is being used.
    """
    if not is_frontier_judge_enabled():
        raise SystemExit(
            f"LLM-judge mode requires {FRONTIER_GATE_ENV}=true. The "
            "reflection text is sent to a non-local model — see "
            "docs/reflection-eval-rubric.md §'LLM-judge mode'."
        )
    from agent.config import settings
    from agent.llm import ModelClient

    client = ModelClient(config=settings)
    chosen_model = judge_model or settings.DEFAULT_FRONTIER_MODEL
    user_prompt = render_judge_user_prompt(ctx)

    result = await client.chat(
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        model=chosen_model,
        # Reflections are structured-summary, not RAW_PERSONAL —
        # SANITIZED is the right sensitivity per
        # docs/reflection-eval-rubric.md §"LLM-judge mode".
        data_sensitivity=DataSensitivity.SANITIZED,
    )
    raw = result.get("content") or ""
    parsed = parse_judge_response(raw)

    # Defensive: clamp out-of-range values to the closest valid
    # score so a single noisy dimension does not invalidate the
    # whole pass. Log when we clamp so calibration sees it.
    def _clip(v) -> int:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return 0
        if iv < 0:
            return 0
        if iv > 3:
            return 3
        return iv

    return RubricScore(
        reflection_id=ctx.reflection.reflection_id,
        coherence=_clip(parsed.get("coherence")),
        novelty=_clip(parsed.get("novelty")),
        grounded_in_traces=_clip(parsed.get("grounded_in_traces")),
        self_framing=_clip(parsed.get("self_framing")),
        length_appropriateness=_clip(parsed.get("length_appropriateness")),
        notes=str(parsed.get("notes") or ""),
        mode="llm-judge",
        judge_model=chosen_model,
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agents.reflector.eval",
        description=(
            "Score a reflection against the rubric in "
            "docs/reflection-eval-rubric.md."
        ),
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("manual", "llm-judge"),
    )
    parser.add_argument("--reflection-id", required=True)
    parser.add_argument(
        "--judge-model",
        default=None,
        help="LLM-judge mode only. Defaults to DEFAULT_FRONTIER_MODEL.",
    )
    return parser.parse_args(argv)


async def _main_async(args: argparse.Namespace) -> int:
    config = load_runtime_config("reflector")
    ctx = await load_context(
        args.reflection_id, postgres_url=config.postgres_url
    )
    if args.mode == "manual":
        sys.stdout.write(render_manual_prompt(ctx))
        sys.stdout.write("\n")
        return 0
    score = await score_with_llm(ctx, judge_model=args.judge_model)
    payload = asdict(score)
    payload["scored_at"] = score.scored_at.isoformat()
    payload["total"] = score.total
    sys.stdout.write(json.dumps(payload, indent=2))
    sys.stdout.write("\n")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

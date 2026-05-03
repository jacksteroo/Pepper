"""Context-assembly adapter — Epic 05 (#46).

Optimizes the **memory-block envelope** that wraps retrieved memory
items before they are handed to the LLM. The default template lives
in this module (``DEFAULT_TEMPLATE``); the production loader at
``agent.optimizer.templates.load_active_template`` reads the
most-recent ``ACCEPTED`` candidate and falls back to ``DEFAULT_TEMPLATE``
when none has been promoted.

Why this is a real target
-------------------------

The memory-block envelope is the smallest piece of "context-assembly
prompt" in the codebase that is genuinely model-facing. The retrieval
ranking itself is embedding-driven (no LLM in the loop, so no prompt
to optimize there). The memory-block envelope, by contrast, is text
the LLM reads on every turn — restructuring it (e.g. switching to a
JSON list, adding usage instructions) plausibly moves comprehension.

What this PR ships
------------------

- The template extraction and the loader (so candidates can replace
  the envelope at runtime once promoted).
- The structural scorer below — verifies a candidate template renders,
  contains the required ``{memory_lines}`` placeholder, and stays
  within a sensible byte budget. It is **not** a comprehension delta
  metric.
- The mutation set used by ``DeterministicRunner``.

What this PR does NOT ship
--------------------------

- A real comprehension-delta scorer that runs an LLM against the #30
  retrieval eval. That requires a model client and a populated
  ``retrieval_eval_set.jsonl`` (gitignored, owner-only). The hooks
  exist (``score`` is a method on the adapter), so the operator can
  swap in a real scorer by subclassing ``ContextAssemblyAdapter`` and
  re-registering. The acceptance criterion "Eval delta ≥5pp Recall@5"
  is operator-gated on a real run, not on this PR.

Promotion lifecycle
-------------------

The same pipeline as every target:

    candidate (data/optimizer/candidates/context_assembly/<v>.json)
      → eval gate (#48) (re-runs sanitizer, runs this module's runner)
      → ACCEPTED (agent/prompts/context_assembly/<v>.json) — committed

Rollback to a prior accepted version is a single CLI command:

    python -m agent.optimizer rollback --target context_assembly --version <hash>
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from agent.optimizer.adapters import register_adapter
from agent.optimizer.eval_gate import register_runner

if TYPE_CHECKING:  # pragma: no cover — type-only
    from agent.optimizer.schema import CandidatePrompt, TraceExample


TARGET_NAME: str = "context_assembly"

# The baseline envelope. Identical to what `agent/memory.py` has used
# inline for the last year — extracted here so the optimizer has
# something to mutate. Kept as a Python constant (rather than a JSON
# file) so importing this module never depends on the filesystem.
DEFAULT_TEMPLATE: str = (
    "[Relevant memories from your history]\n"
    "{memory_lines}\n"
    "[End memories]"
)

# Sanity bounds for a candidate template. Real memory-block envelopes
# are well under 1KB; anything bigger is either a bug or a
# pathological optimizer mutation.
_MAX_TEMPLATE_BYTES: int = 2048
_REQUIRED_PLACEHOLDER: str = "{memory_lines}"


def render_template(template_text: str, memory_lines: str) -> str:
    """Render the memory block.

    Production callers (``agent/memory.py``) use this. Always uses
    ``str.replace`` rather than ``.format`` so a candidate template
    that happens to contain stray ``{`` / ``}`` characters does not
    raise — the optimizer mutation surface is text, not Python format
    grammar.
    """
    return template_text.replace(_REQUIRED_PLACEHOLDER, memory_lines)


def _structural_score(template_text: str) -> float:
    """Score in [0, 1].

    Today this is a structural sanity check — does the candidate
    render correctly and stay within a reasonable byte budget. The
    follow-up (operator-driven) replaces this with an LLM-driven
    comprehension delta against the #30 retrieval eval.
    """
    if _REQUIRED_PLACEHOLDER not in template_text:
        return 0.0
    if len(template_text.encode("utf-8", errors="replace")) > _MAX_TEMPLATE_BYTES:
        return 0.0
    rendered = render_template(template_text, "• example memory item")
    if not rendered.strip():
        return 0.0
    # Heuristic spread: shorter is better (cheaper context), but a
    # template that's *too* short usually drops useful guidance.
    # Map [50, 500] bytes → [1.0, 0.7]; outside drops further.
    n = len(template_text)
    if n < 30:
        return 0.5
    if n <= 500:
        # Linear interpolation, anchored at the bounds above.
        return 1.0 - 0.3 * (max(n - 50, 0) / 450.0)
    # Above 500B keep penalising linearly down to the byte-budget cap.
    return max(0.4 - 0.3 * (n - 500) / (_MAX_TEMPLATE_BYTES - 500), 0.05)


class ContextAssemblyAdapter:
    """Adapter for the memory-block envelope.

    Implements the ``OptimizerAdapter`` protocol. ``score`` and
    ``mutate`` are deterministic for a given ``seed``, so the
    optimizer's reproducibility test holds.
    """

    target = TARGET_NAME

    def score(self, prompt_text: str, example: "TraceExample") -> float:
        # The example is unused by the structural scorer; it exists
        # in the signature so a richer scorer (LLM-driven) can drop
        # in without changing the call site.
        del example
        return _structural_score(prompt_text)

    def mutate(
        self,
        prompt_text: str,
        examples: "Sequence[TraceExample]",
        seed: int,
    ) -> list[str]:
        """Deterministic mutations of the envelope.

        Same ``seed`` → same outputs. The set is intentionally small
        and structural; richer mutations land via GEPA's reflection
        step in production runs.
        """
        del examples
        # Each mutation must keep ``{memory_lines}`` to render.
        return [
            # 1. JSON-list rewrite, terser.
            'Memories (most relevant first):\n{memory_lines}',
            # 2. Add a brief instruction to the LLM about how to use them.
            (
                "[Relevant memories from your history]\n"
                "Use these to ground your answer; cite them when applicable.\n"
                "{memory_lines}\n"
                "[End memories]"
            ),
            # 3. Trim the closing tag.
            "[Relevant memories from your history]\n{memory_lines}",
            # 4. No-op spaces (must NOT be returned because == baseline-after-strip).
            #    Skipped intentionally — the runner's `if mutated == baseline_prompt`
            #    filter would catch it but adding it teaches no signal.
        ]


def _eval_runner(candidate: "CandidatePrompt") -> float:
    """Eval-gate runner for ``context_assembly``.

    Runs the same structural scorer as the adapter. The eval gate's
    job is to refuse promotion of broken templates; the structural
    scorer catches the obvious cases (missing placeholder, oversize,
    empty). When the operator swaps in an LLM-driven adapter the
    runner can be replaced via ``register_runner`` to match.
    """
    return _structural_score(candidate.prompt_text)


# ── Registrations (run at import time) ──────────────────────────────────────

register_adapter(TARGET_NAME, ContextAssemblyAdapter)
register_runner(TARGET_NAME, _eval_runner)

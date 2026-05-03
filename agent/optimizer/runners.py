"""Optimizer runners — the only module that touches the framework.

Per ADR-0007 the framework is GEPA, isolated behind this file. Other
modules in ``agent/optimizer/`` depend on the ``OptimizerRunner``
protocol declared here, never on ``import gepa`` directly. A future
swap to DSPy is a single-file rewrite of this module.

This module ships two runners:

- ``GepaRunner`` — production. Imports ``gepa`` lazily so the rest of
  the optimizer is importable in environments where ``gepa`` is not
  installed (CI minimal images, hermetic tests).
- ``DeterministicRunner`` — fixture-only. Used by tests and by the
  end-to-end CLI smoke test (``--runner=deterministic``). Produces
  reproducible candidates from a tiny pool of mutation operators.
  Not for production. The class exists because #45 acceptance
  requires "Determinism: same input traces + same seed → same
  candidate set" — GEPA gives us reproducibility *given an LM*; the
  deterministic runner gives us reproducibility *without* one.

Adapter contract
----------------

Both runners take an ``OptimizerAdapter`` (defined here, not
GEPA-specific) that the target wires up:

- ``score(prompt_text, example) -> float`` — higher is better.
- ``mutate(prompt_text, examples, seed) -> Iterable[str]`` — only used
  by ``DeterministicRunner``. ``GepaRunner`` ignores it and uses GEPA's
  reflection-mutation step.

The split keeps targets (#46, #47) free to ship without re-implementing
GEPA's adapter shape — they implement scoring (the part that depends on
the target) and inherit the rest.
"""
from __future__ import annotations

import math
import statistics
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

import structlog

from agent.optimizer.audit import AuditLog
from agent.optimizer.datasets import dataset_hash
from agent.optimizer.sanitizer import load_life_context_tokens, scan
from agent.optimizer.schema import (
    CandidatePrompt,
    OptimizerRunRecord,
    PromptStatus,
    TraceExample,
)
from agent.optimizer.storage import PromptStore, compute_version_hash

if TYPE_CHECKING:  # pragma: no cover — type-only
    pass

logger = structlog.get_logger(__name__)


@runtime_checkable
class OptimizerAdapter(Protocol):
    """Target-specific scoring + mutation hooks.

    Targets (#46, #47) implement this. The runner stays target-agnostic.
    """

    target: str  # e.g. "context_assembly", "router_classifier"

    def score(self, prompt_text: str, example: TraceExample) -> float:
        """Return a higher-is-better score for ``prompt_text`` on ``example``."""

    def mutate(
        self,
        prompt_text: str,
        examples: Sequence[TraceExample],
        seed: int,
    ) -> list[str]:
        """Return candidate mutations of ``prompt_text``.

        Used only by ``DeterministicRunner``. ``GepaRunner`` ignores
        this — GEPA proposes mutations via its own reflection step.
        Targets that only ever run under GEPA may return ``[]``.
        """


@runtime_checkable
class OptimizerRunner(Protocol):
    """Run an optimizer over a dataset, return candidate prompts.

    Implementations: ``GepaRunner`` (production), ``DeterministicRunner``
    (tests + smoke).

    ``run_id`` is provided by the caller (typically ``run_optimizer``)
    so the audit row's ``run_id`` and the produced candidates'
    ``optimizer_run_id`` are always the same value, even on failure
    paths that produce zero candidates.
    """

    def run(
        self,
        *,
        baseline_prompt: str,
        examples: Sequence[TraceExample],
        adapter: OptimizerAdapter,
        seed: int,
        run_id: str,
    ) -> list[CandidatePrompt]:
        ...


# ── Helpers shared by all runners ────────────────────────────────────────────


def _build_candidate(
    *,
    target: str,
    prompt_text: str,
    parent_version: str,
    optimizer_run_id: str,
    eval_score: float,
    life_context_tokens: frozenset[str],
) -> CandidatePrompt:
    return CandidatePrompt(
        target=target,
        version_hash=compute_version_hash(target, prompt_text),
        parent_version=parent_version,
        optimizer_run_id=optimizer_run_id,
        prompt_text=prompt_text,
        eval_score=eval_score,
        status=PromptStatus.CANDIDATE,
        sanitization=scan(prompt_text, life_context_tokens=life_context_tokens),
    )


def _mean_score(prompt_text: str, examples: Sequence[TraceExample], adapter: OptimizerAdapter) -> float:
    if not examples:
        return float("nan")
    return statistics.fmean(adapter.score(prompt_text, e) for e in examples)


# ── DeterministicRunner ──────────────────────────────────────────────────────


class DeterministicRunner:
    """Reproducible runner that does not depend on ``gepa``.

    Used in tests and the CLI smoke path. The "optimization" is:

    1. Score the baseline.
    2. Ask the adapter for mutation candidates (deterministic given seed).
    3. Score each candidate.
    4. Return all candidates that beat baseline, sorted by score desc.

    No reflection, no Pareto front — that's GEPA's job. This class only
    exists to make the optimizer module independently shippable and
    testable without installing GEPA.
    """

    def run(
        self,
        *,
        baseline_prompt: str,
        examples: Sequence[TraceExample],
        adapter: OptimizerAdapter,
        seed: int,
        run_id: str | None = None,
    ) -> list[CandidatePrompt]:
        if not examples:
            return []
        run_id = run_id or uuid.uuid4().hex
        life_context_tokens = load_life_context_tokens()
        baseline_score = _mean_score(baseline_prompt, examples, adapter)
        baseline_hash = compute_version_hash(adapter.target, baseline_prompt)

        candidates: list[CandidatePrompt] = []
        for mutated in adapter.mutate(baseline_prompt, examples, seed):
            if mutated == baseline_prompt:
                continue
            score = _mean_score(mutated, examples, adapter)
            if score <= baseline_score:
                continue
            candidates.append(
                _build_candidate(
                    target=adapter.target,
                    prompt_text=mutated,
                    parent_version=baseline_hash,
                    optimizer_run_id=run_id,
                    eval_score=score,
                    life_context_tokens=life_context_tokens,
                ),
            )
        candidates.sort(key=lambda c: c.eval_score, reverse=True)
        return candidates


# ── GepaRunner ───────────────────────────────────────────────────────────────


class GepaRunner:
    """Production runner — wraps ``gepa.optimize``.

    Lazy-imports ``gepa`` so the rest of ``agent/optimizer/`` is
    importable when GEPA is not installed (e.g. on a stripped CI
    image).

    The GEPA adapter is built inline — translates between Pepper's
    ``OptimizerAdapter`` and GEPA's ``GEPAAdapter`` protocol. Trajectories
    and reflective-dataset construction are minimal: scores only. Targets
    that want richer reflection feedback can subclass this and override
    ``_build_gepa_adapter``.

    Privacy invariant (ADR-0007)
    ----------------------------

    GEPA's reflection step calls an LM with trace content (raw user
    inputs end up in the reflective dataset). To honour the
    "no-frontier-API-in-the-inner-loop" invariant, ``reflection_lm``
    MUST be a local model URL/identifier. ``__init__`` enforces this:
    a missing or non-local ``reflection_lm`` raises before any trace
    content can be sent. ``LOCAL_LM_PREFIXES`` lists the allowed
    schemes; override via ``allowed_lm_prefixes`` if Pepper ever runs
    a different local provider.
    """

    # Strings that GEPA's litellm-style ``reflection_lm`` argument may
    # legitimately start with. Any other string is rejected — this is
    # the "fail-closed" gate that keeps trace content from being
    # forwarded to a remote provider.
    LOCAL_LM_PREFIXES: tuple[str, ...] = (
        "ollama/",            # ollama via litellm
        "ollama_chat/",
        "openai/local-",      # local OpenAI-compatible proxy (LM Studio, etc.)
        "http://localhost",   # raw http to a local endpoint
        "http://127.0.0.1",
    )

    def __init__(
        self,
        *,
        reflection_lm: str,
        max_metric_calls: int = 50,
        allowed_lm_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self._max_metric_calls = max_metric_calls
        self._reflection_lm = reflection_lm
        self._allowed_prefixes = allowed_lm_prefixes or self.LOCAL_LM_PREFIXES
        self._validate_reflection_lm()

    def _validate_reflection_lm(self) -> None:
        """Refuse non-local ``reflection_lm`` values.

        ADR-0007: trace content must not be sent to a frontier API
        during optimization. The runner cannot tell at call-time
        whether a string identifier resolves to a local or remote
        endpoint, so we use an explicit allowlist of well-known local
        prefixes. Operators wanting an unusual local provider override
        ``allowed_lm_prefixes``.
        """
        lm = self._reflection_lm
        if not lm or not isinstance(lm, str):
            raise ValueError(
                "GepaRunner requires a non-empty `reflection_lm` "
                "(e.g. 'ollama/llama3'); see ADR-0007 for the "
                "no-frontier-API-in-the-inner-loop invariant.",
            )
        if not any(lm.startswith(p) for p in self._allowed_prefixes):
            raise ValueError(
                f"reflection_lm {lm!r} does not match any local-model "
                f"prefix {self._allowed_prefixes!r}. Refusing to run "
                "to preserve the privacy invariant from ADR-0007. If "
                "this is a local provider, pass it via "
                "`allowed_lm_prefixes=`.",
            )

    def run(
        self,
        *,
        baseline_prompt: str,
        examples: Sequence[TraceExample],
        adapter: OptimizerAdapter,
        seed: int,
        run_id: str | None = None,
    ) -> list[CandidatePrompt]:
        if not examples:
            return []
        try:
            import gepa  # noqa: PLC0415 — lazy import: keeps GEPA optional at module load
        except ImportError as e:
            raise RuntimeError(
                "GepaRunner requires the 'gepa' package — install with "
                "`uv pip install gepa`. See ADR-0007 for context.",
            ) from e

        run_id = run_id or uuid.uuid4().hex
        life_context_tokens = load_life_context_tokens()
        baseline_hash = compute_version_hash(adapter.target, baseline_prompt)

        gepa_adapter = self._build_gepa_adapter(adapter)
        seed_candidate: dict[str, str] = {adapter.target: baseline_prompt}

        result = gepa.optimize(
            seed_candidate=seed_candidate,
            trainset=list(examples),
            adapter=gepa_adapter,
            reflection_lm=self._reflection_lm,
            max_metric_calls=self._max_metric_calls,
            seed=seed,
            display_progress_bar=False,
            raise_on_exception=False,
        )

        # GEPA returns a result with a Pareto frontier of candidate texts.
        # We convert each frontier candidate into a CandidatePrompt. Score
        # is the mean of GEPA's per-example scores; details depend on the
        # GEPA version in use, so we tolerate a few attribute names.
        return self._extract_candidates(
            result=result,
            adapter=adapter,
            baseline_hash=baseline_hash,
            run_id=run_id,
            life_context_tokens=life_context_tokens,
        )

    @staticmethod
    def _build_gepa_adapter(pepper_adapter: OptimizerAdapter):
        """Wrap a Pepper ``OptimizerAdapter`` into a GEPA ``GEPAAdapter``.

        Built inline (closure over ``pepper_adapter``) instead of as a
        top-level class so we don't expose a GEPA type in our public
        surface — keeps the framework boundary at this file.
        """
        import gepa  # noqa: PLC0415 — already imported by .run()

        target_name = pepper_adapter.target

        class _Adapter(gepa.GEPAAdapter):
            def evaluate(self, batch, candidate, capture_traces=False):
                prompt_text = candidate[target_name]
                scores = [pepper_adapter.score(prompt_text, ex) for ex in batch]
                outputs = [{"score": s} for s in scores]
                trajectories = [{"input": ex.input, "score": s} for ex, s in zip(batch, scores)]
                return gepa.EvaluationBatch(
                    outputs=outputs,
                    scores=scores,
                    trajectories=trajectories if capture_traces else None,
                )

            def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
                # Minimal reflection: feed the worst-scoring example and its
                # current prompt back to GEPA's reflection LM. Targets that
                # want richer feedback override this via their own subclass.
                if not eval_batch.scores:
                    return {c: [] for c in components_to_update}
                worst_idx = int(min(range(len(eval_batch.scores)),
                                    key=lambda i: eval_batch.scores[i]))
                worst_traj = (eval_batch.trajectories or [{}])[worst_idx]
                feedback = [{
                    "Inputs": worst_traj.get("input", ""),
                    "Generated Outputs": str(eval_batch.outputs[worst_idx]),
                    "Feedback": (
                        f"Score {eval_batch.scores[worst_idx]:.3f} on this "
                        f"example. Improve the prompt to score higher."
                    ),
                }]
                return {c: feedback for c in components_to_update}

        return _Adapter()

    @staticmethod
    def _extract_candidates(
        *,
        result,
        adapter: OptimizerAdapter,
        baseline_hash: str,
        run_id: str,
        life_context_tokens: frozenset[str],
    ) -> list[CandidatePrompt]:
        # GEPA's GEPAResult exposes `.candidates` (list of dicts) and
        # `.val_aggregate_scores` (parallel list of mean scores).
        # The exact attribute set varies between GEPA versions; we read
        # defensively rather than tightly coupling to one shape.
        raw_candidates = getattr(result, "candidates", []) or []
        raw_scores = getattr(result, "val_aggregate_scores", None)
        if raw_scores is None:
            raw_scores = [float("nan")] * len(raw_candidates)

        out: list[CandidatePrompt] = []
        for cand_dict, score in zip(raw_candidates, raw_scores):
            prompt_text = cand_dict.get(adapter.target)
            if not prompt_text:
                continue
            out.append(
                _build_candidate(
                    target=adapter.target,
                    prompt_text=prompt_text,
                    parent_version=baseline_hash,
                    optimizer_run_id=run_id,
                    eval_score=float(score) if score is not None else float("nan"),
                    life_context_tokens=life_context_tokens,
                ),
            )
        # NaN comparisons are all False, so sorting a list with NaNs
        # is non-deterministic. Treat NaN as -inf for ordering only —
        # the candidate's stored ``eval_score`` keeps the literal NaN.
        out.sort(
            key=lambda c: (-math.inf if math.isnan(c.eval_score) else c.eval_score),
            reverse=True,
        )
        return out


# ── Top-level orchestration ─────────────────────────────────────────────────


def run_optimizer(
    *,
    runner: OptimizerRunner,
    adapter: OptimizerAdapter,
    examples: Sequence[TraceExample],
    baseline_prompt: str,
    seed: int = 0,
    archetype: str = "",
    prompt_version_filter: str = "",
    window_since: Optional[datetime] = None,
    window_until: Optional[datetime] = None,
    store: Optional[PromptStore] = None,
    audit_log: Optional[AuditLog] = None,
) -> tuple[OptimizerRunRecord, list[CandidatePrompt]]:
    """End-to-end: run, persist candidates, append audit row.

    The audit row is **always** appended — even if the runner raised or
    no candidate beat baseline. Failures populate ``record.error``;
    consumers (eval gate, operator) read that field to decide whether
    to act on the run.

    The ``run_id`` is generated here at the top and threaded through
    both the runner and the audit row so they always agree, regardless
    of whether the run produced candidates. Each candidate the runner
    yields carries the same ``optimizer_run_id``.

    Candidates persisted only on the happy path. If a store write
    fails partway through, ``record.candidate_count`` reflects the
    list size returned by the runner (i.e. what *would* have been
    written) and ``record.error`` carries the persistence error.
    """
    store = store or PromptStore()
    audit_log = audit_log or AuditLog()
    run_id = uuid.uuid4().hex
    started_at = datetime.now(timezone.utc)
    baseline_hash = compute_version_hash(adapter.target, baseline_prompt)
    error_msg = ""
    candidates: list[CandidatePrompt] = []

    try:
        candidates = runner.run(
            baseline_prompt=baseline_prompt,
            examples=examples,
            adapter=adapter,
            seed=seed,
            run_id=run_id,
        )
        for c in candidates:
            store.put(c)
    except Exception as e:  # pragma: no cover — defensive
        error_msg = f"{type(e).__name__}: {e}"
        logger.exception("optimizer.run.failed", target=adapter.target, error=error_msg)

    finished_at = datetime.now(timezone.utc)
    record = OptimizerRunRecord(
        run_id=run_id,
        target=adapter.target,
        archetype=archetype,
        prompt_version_filter=prompt_version_filter,
        window_since=window_since,
        window_until=window_until,
        dataset_size=len(examples),
        dataset_hash=dataset_hash(examples),
        seed=seed,
        baseline_version=baseline_hash,
        runner_class=type(runner).__name__,
        candidate_count=len(candidates),
        started_at=started_at,
        finished_at=finished_at,
        error=error_msg,
    )
    audit_log.append(record)
    return record, candidates

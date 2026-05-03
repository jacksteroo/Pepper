"""Dataclasses for the optimizer module.

Three records, all framework-agnostic:

- ``TraceExample`` — one trace projected into the shape an optimizer needs
  (input + reference output + provenance fields). Not a 1-to-1 of ``Trace``;
  fields the optimizer doesn't need are dropped to keep the surface tight.
- ``CandidatePrompt`` — one prompt produced by an optimizer run, with all
  the metadata #45 calls out (version_hash, parent_version, optimizer_run_id,
  eval_score, status). Persisted via ``storage.PromptStore``.
- ``OptimizerRunRecord`` — one row in the audit log. One per ``optimize()``
  call.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


class PromptStatus(str, enum.Enum):
    """Lifecycle states a candidate prompt can be in.

    See #45 §Implementation step 4. Promotion (candidate → accepted) is
    enforced by the eval gate (#48), not by this module.
    """

    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class TraceExample:
    """One trace, projected for optimizer consumption.

    Fields kept deliberately minimal — only what an adapter typically needs
    to build a candidate-evaluation prompt. ``assembled_context`` and
    ``tools_called`` are loaded lazily by the dataset builder when callers
    pass ``with_payload=True``; otherwise they are empty.
    """

    trace_id: str
    archetype: str
    prompt_version: str
    input: str
    output: str
    assembled_context: dict[str, Any] = field(default_factory=dict)
    tools_called: list[dict[str, Any]] = field(default_factory=list)
    user_reaction: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class CandidatePrompt:
    """A prompt produced by an optimizer run.

    Stored as JSON under ``data/optimizer/candidates/<target>/<version_hash>.json``
    until #48 promotes the chosen candidate into ``agent/prompts/<target>/``.

    Field semantics:

    - ``version_hash`` — sha256 of ``(target, prompt_text)``, hex-truncated to
      16 chars. Stable across machines; round-trips via ``PromptStore``.
    - ``parent_version`` — the version_hash this candidate was mutated from.
      Empty string for seeds (the baseline before the first optimizer run).
    - ``optimizer_run_id`` — UUID4 string assigned by the runner per call.
      Joins to ``OptimizerRunRecord.run_id`` in the audit log.
    - ``eval_score`` — the score produced by the optimizer's metric. Higher
      is better (GEPA convention; see ADR-0007). May be float or NaN if
      the run aborted.
    - ``status`` — see ``PromptStatus``. Defaults to ``CANDIDATE``.
    - ``sanitization`` — output of ``sanitizer.scan()``. Empty list ⇒ clean.
    """

    target: str
    version_hash: str
    parent_version: str
    optimizer_run_id: str
    prompt_text: str
    eval_score: float
    status: PromptStatus = PromptStatus.CANDIDATE
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    sanitization: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OptimizerRunRecord:
    """One row in the audit log (``data/optimizer/audit.jsonl``).

    Append-only, one record per ``optimize()`` call regardless of outcome.
    Fields chosen so a future operator can reproduce a run from the log
    alone:

    - ``run_id`` joins back to the candidate prompts produced by this run.
    - ``dataset_hash`` is sha256 of the sorted trace_ids in the dataset.
      Combined with ``seed``, makes the run reproducible.
    - ``baseline_version`` is the version_hash of the seed prompt (the one
      every candidate was mutated from). Empty string if a fresh prompt.
    - ``candidate_count`` — how many candidates the run produced. Zero is
      a valid outcome (GEPA can return no improvement over baseline).
    - ``error`` — populated when the run aborted. Empty otherwise.
    """

    run_id: str
    target: str
    archetype: str
    prompt_version_filter: str
    window_since: Optional[datetime]
    window_until: Optional[datetime]
    dataset_size: int
    dataset_hash: str
    seed: int
    baseline_version: str
    runner_class: str
    candidate_count: int
    started_at: datetime
    finished_at: datetime
    error: str = ""

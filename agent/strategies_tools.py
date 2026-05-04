"""LLM-facing tools for the Strategy Hub (#54).

Two tools:

- ``query_strategies(situation, top_k=5)`` — read-only. Returns the
  top-k active strategies most relevant to the operator-described
  situation. v0 ranking: keyword overlap on token sets (no embedding
  call). The repository's ``bump_usage`` is invoked on the matched
  strategies so the confidence score reflects real usage.

- ``propose_strategy_update(strategy_id?, new_text, reason)`` — write
  path. Routes through ``pending_strategy_diffs``; **never writes
  directly** to the strategies table. ``strategy_id`` is optional —
  when present, the proposal is a new version of an existing
  strategy; when absent, a new lineage. Per #54 AC, the optimizer is
  forbidden from optimizing the strategy block (enforced separately
  via FORBIDDEN_TARGETS in the optimizer).

The dispatcher accepts an injected pair of repositories so the tool
implementations stay framework-agnostic (testable with stubs, no
direct DB import paths in the schema module). The caller in
``agent/core.py`` wires real repositories at request time.
"""
from __future__ import annotations

import re
import uuid as _uuid
from typing import Any, Callable, Optional

import structlog

from agent.strategies.repository import (
    Strategy,
    StrategyCreatedBy,
    StrategyRepository,
    StrategyStatus,
)
from agent.strategy_diffs import StrategyDiff, StrategyDiffRepository

logger = structlog.get_logger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

MAX_SITUATION_LEN: int = 2000
MAX_NEW_TEXT_LEN: int = 2000
MAX_REASON_LEN: int = 2000
DEFAULT_TOP_K: int = 5
MAX_TOP_K: int = 20

# Words too generic to score on. Kept tiny on purpose — for v0 we
# accept some noise; the LLM's situation text will dominate the
# overlap if there's any signal there at all.
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "the", "is", "are", "was", "were", "be", "to",
    "of", "for", "in", "on", "at", "by", "with", "as", "it", "this",
    "that", "i", "we", "you", "they", "he", "she", "do", "does",
    "did", "should", "would", "could", "have", "has", "had", "but",
    "or", "if", "when", "while", "from", "into", "about", "over",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# ── Tool schema ──────────────────────────────────────────────────────────────


STRATEGIES_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "query_strategies",
            "description": (
                "Retrieve the most relevant interpretable strategies for "
                "the current situation. Strategies are short, "
                "operator-readable rules ('When X, do Y') stored in the "
                "Strategy Hub. Use this when you want to ground a "
                "decision in a strategy that has been validated by the "
                "operator (or proposed by you and approved). Returns up "
                "to top_k matches with a relevance score. Calling this "
                "tool also bumps the matched strategies' usage_count, "
                "which feeds confidence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "situation": {
                        "type": "string",
                        "description": (
                            "Plain-language description of the situation "
                            "you want strategies for. Required."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": (
                            "How many strategies to return (default 5, "
                            "max 20)."
                        ),
                    },
                },
                "required": ["situation"],
            },
        },
    },
    {
        "type": "function",
        "side_effects": True,
        "function": {
            "name": "propose_strategy_update",
            "description": (
                "Propose a new strategy or a new version of an existing "
                "one. The proposal is queued for operator approval — it "
                "is NEVER applied directly to the Strategy Hub. Use "
                "when you notice a recurring pattern that justifies a "
                "rule, or when an existing strategy needs revision. If "
                "strategy_id is omitted, the proposal is a new lineage."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {
                        "type": "string",
                        "description": (
                            "UUID of the strategy this proposal supersedes. "
                            "Omit for a new lineage."
                        ),
                    },
                    "new_text": {
                        "type": "string",
                        "description": (
                            "The strategy in natural language, one sentence. "
                            "Required."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why this update — the trace-grounded rationale "
                            "the operator will read when deciding. Required."
                        ),
                    },
                },
                "required": ["new_text", "reason"],
            },
        },
    },
]


# ── Ranking primitives ───────────────────────────────────────────────────────


def _tokenize(text: str) -> set[str]:
    return {
        t
        for t in _TOKEN_RE.findall(text.lower())
        if t and t not in _STOPWORDS and len(t) > 2
    }


def _score_overlap(situation_tokens: set[str], strategy_text: str) -> float:
    """Symmetric Jaccard over salient tokens, in [0, 1].

    v0. Phase 2 will swap this for cosine similarity over the
    `qwen3-embedding:0.6b` embedding column we already index.
    """
    if not situation_tokens:
        return 0.0
    s_tokens = _tokenize(strategy_text)
    if not s_tokens:
        return 0.0
    intersection = situation_tokens & s_tokens
    if not intersection:
        return 0.0
    union = situation_tokens | s_tokens
    return len(intersection) / len(union)


def rank_strategies(
    situation: str,
    strategies: list[Strategy],
    *,
    top_k: int = DEFAULT_TOP_K,
) -> list[tuple[Strategy, float]]:
    """Rank `strategies` by relevance to `situation`. Returns sorted
    descending by score with ties broken by recency.

    Strategies with score 0.0 are filtered out — surfacing irrelevant
    strategies in the model's tool result is worse than returning
    fewer.
    """
    situation_tokens = _tokenize(situation)
    scored: list[tuple[Strategy, float]] = []
    for s in strategies:
        score = _score_overlap(situation_tokens, s.text)
        if score > 0.0:
            scored.append((s, score))
    scored.sort(
        key=lambda pair: (pair[1], pair[0].created_at),
        reverse=True,
    )
    return scored[:top_k]


# ── Executors ────────────────────────────────────────────────────────────────


async def execute_query_strategies(
    args: dict[str, Any],
    *,
    repo: StrategyRepository,
) -> dict[str, Any]:
    raw_situation = args.get("situation")
    if not isinstance(raw_situation, str) or not raw_situation.strip():
        return {"error": "query_strategies requires a non-empty 'situation'"}
    if len(raw_situation) > MAX_SITUATION_LEN:
        return {"error": f"situation exceeds {MAX_SITUATION_LEN} chars"}
    raw_top_k = args.get("top_k", DEFAULT_TOP_K)
    try:
        top_k = int(raw_top_k)
    except (TypeError, ValueError):
        return {"error": "top_k must be an integer"}
    if top_k < 1:
        return {"error": "top_k must be >= 1"}
    if top_k > MAX_TOP_K:
        top_k = MAX_TOP_K

    active = await repo.query_active(limit=200)
    ranked = rank_strategies(raw_situation, active, top_k=top_k)

    out_strategies: list[dict[str, Any]] = []
    for strategy, score in ranked:
        # Bump usage on each match — feeds confidence v0. We do this
        # whether or not the model ends up using the strategy because
        # surfacing it to the model is itself "use."
        await repo.bump_usage(strategy.strategy_id)
        out_strategies.append(
            {
                "strategy_id": str(strategy.strategy_id),
                "text": strategy.text,
                "version": strategy.version,
                "created_by": strategy.created_by,
                "score": round(score, 4),
                "confidence": strategy.confidence,
            }
        )

    logger.info(
        "query_strategies_executed",
        top_k=top_k,
        match_count=len(out_strategies),
    )
    return {
        "ok": True,
        "count": len(out_strategies),
        "strategies": out_strategies,
    }


async def execute_propose_strategy_update(
    args: dict[str, Any],
    *,
    diffs_repo: StrategyDiffRepository,
    proposed_by: str = StrategyCreatedBy.JACK,
) -> dict[str, Any]:
    raw_strategy_id = args.get("strategy_id")
    raw_new_text = args.get("new_text")
    raw_reason = args.get("reason")

    if not isinstance(raw_new_text, str) or not raw_new_text.strip():
        return {"error": "propose_strategy_update requires a non-empty 'new_text'"}
    if len(raw_new_text) > MAX_NEW_TEXT_LEN:
        return {"error": f"new_text exceeds {MAX_NEW_TEXT_LEN} chars"}
    if not isinstance(raw_reason, str) or not raw_reason.strip():
        return {"error": "propose_strategy_update requires a non-empty 'reason'"}
    if len(raw_reason) > MAX_REASON_LEN:
        return {"error": f"reason exceeds {MAX_REASON_LEN} chars"}

    target_strategy_id: Optional[_uuid.UUID] = None
    if raw_strategy_id:
        if not isinstance(raw_strategy_id, str):
            return {"error": "strategy_id must be a UUID string"}
        try:
            target_strategy_id = _uuid.UUID(raw_strategy_id)
        except ValueError:
            return {"error": f"strategy_id is not a valid UUID: {raw_strategy_id!r}"}

    diff = StrategyDiff(
        proposed_text=raw_new_text.strip(),
        rationale=raw_reason.strip(),
        target_strategy_id=target_strategy_id,
        proposed_by=proposed_by,
    )
    await diffs_repo.append(diff)

    logger.info(
        "propose_strategy_update_queued",
        diff_id=str(diff.diff_id),
        target_strategy_id=str(target_strategy_id) if target_strategy_id else None,
    )
    return {
        "ok": True,
        "queued": True,
        "diff_id": str(diff.diff_id),
        "target_strategy_id": str(target_strategy_id) if target_strategy_id else None,
        "message": (
            "Strategy update proposed. The operator will see it in the "
            "pending-actions queue and decide whether to apply."
        ),
    }


# ── Dispatcher ───────────────────────────────────────────────────────────────


async def execute_strategies_tool(
    name: str,
    args: dict[str, Any],
    *,
    repo: StrategyRepository,
    diffs_repo: StrategyDiffRepository,
    proposed_by: str = StrategyCreatedBy.JACK,
) -> dict[str, Any]:
    if name == "query_strategies":
        return await execute_query_strategies(args, repo=repo)
    if name == "propose_strategy_update":
        return await execute_propose_strategy_update(
            args, diffs_repo=diffs_repo, proposed_by=proposed_by
        )
    return {"error": f"unknown strategies tool: {name!r}"}

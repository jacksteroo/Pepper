"""Append-only repository for the ``strategies`` table.

Surface intentionally restricted: ``append``, ``query_by_similarity``,
``query_all_active``, ``flag``, ``supersede``, ``detect_contradiction``,
and ``bump_usage``. No generic ``update_*`` or ``delete_*`` methods.

Version chains: to revise a strategy, call ``append`` with a new row
that has ``parent_strategy_id`` set to the old row's id, then call
``supersede`` to mark the old row as superseded.

``flag`` and ``supersede`` are narrow status-mutation carve-outs —
the only sanctioned writes beyond the initial INSERT.
"""
from __future__ import annotations

import uuid
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from agent.strategies.models import StrategyRow

logger = structlog.get_logger(__name__)

# Allowed status values.
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_FLAGGED = "flagged"

# Cosine distance threshold below which two strategies are considered
# "similar enough" to compare for contradiction.  At 768-dim this
# roughly captures strategies addressing the same topic.
_CONTRADICTION_SIMILARITY_THRESHOLD = 0.30  # cosine distance ≤ 0.30 → similar


class StrategyRepository:
    """Append-only repository for strategies.

    This class is the *only* sanctioned way to touch the strategies
    table from application code.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Append ───────────────────────────────────────────────────────

    async def append(self, row: StrategyRow) -> StrategyRow:
        """Insert a new strategy row.

        Never UPDATE in-place.  To revise, create a new row with
        ``parent_strategy_id`` set and call ``supersede`` on the old id.
        Returns the persisted row (server-side defaults resolved).
        """
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        logger.info(
            "strategy_appended",
            strategy_id=str(row.strategy_id),
            version=row.version,
            created_by=row.created_by,
            status=row.status,
        )
        return row

    # ── Reads ─────────────────────────────────────────────────────────

    async def query_all_active(self) -> list[StrategyRow]:
        """Return all active strategies, newest first."""
        stmt = (
            select(StrategyRow)
            .where(StrategyRow.status == STATUS_ACTIVE)
            .order_by(StrategyRow.created_at.desc())
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def query_by_similarity(
        self,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[StrategyRow]:
        """Return the top-k active strategies nearest to ``query_embedding``.

        Uses cosine distance on the ``embedding`` column (HNSW index).
        Rows with null embedding are excluded — they have not been
        embedded yet and cannot be ranked.
        """
        from agent.strategies import STRATEGY_EMBEDDING_DIM  # noqa: PLC0415

        if len(query_embedding) != STRATEGY_EMBEDDING_DIM:
            raise ValueError(
                f"query_embedding must be {STRATEGY_EMBEDDING_DIM}-dim, "
                f"got {len(query_embedding)}"
            )
        top_k = max(1, min(top_k, 100))

        stmt = (
            select(StrategyRow)
            .where(
                StrategyRow.status == STATUS_ACTIVE,
                StrategyRow.embedding.isnot(None),
            )
            .order_by(
                StrategyRow.embedding.cosine_distance(query_embedding)
            )
            .limit(top_k)
            .options(undefer(StrategyRow.embedding))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ── Narrow status mutations (carve-outs from append-only) ─────────

    async def flag(self, strategy_id: uuid.UUID, reason: str) -> None:
        """Mark a strategy as flagged for review.

        Does not delete or overwrite the text — flagged strategies are
        visible for audit.  Log the reason for traceability.
        """
        row = await self._session.get(StrategyRow, strategy_id)
        if row is None:
            raise LookupError(f"strategy {strategy_id} not found")
        row.status = STATUS_FLAGGED
        await self._session.flush()
        logger.info(
            "strategy_flagged",
            strategy_id=str(strategy_id),
            reason=reason[:256],
        )

    async def supersede(
        self, old_id: uuid.UUID, new_id: uuid.UUID
    ) -> None:
        """Mark ``old_id`` as superseded by ``new_id``.

        Idempotent: if ``old_id`` is already superseded, no-op.
        """
        row = await self._session.get(StrategyRow, old_id)
        if row is None:
            raise LookupError(f"strategy {old_id} not found")
        if row.status == STATUS_SUPERSEDED:
            return
        row.status = STATUS_SUPERSEDED
        await self._session.flush()
        logger.info(
            "strategy_superseded",
            old_id=str(old_id),
            new_id=str(new_id),
        )

    async def bump_usage(self, strategy_id: uuid.UUID) -> None:
        """Increment ``usage_count`` for a strategy.

        Called when a strategy is surfaced in a prompt so we track
        which strategies are actually being used.
        """
        row = await self._session.get(StrategyRow, strategy_id)
        if row is None:
            return  # graceful — strategy may have been superseded
        row.usage_count = (row.usage_count or 0) + 1
        await self._session.flush()

    # ── Contradiction detection ────────────────────────────────────────

    async def detect_contradiction(
        self,
        new_text: str,
        existing: list[StrategyRow],
        llm_client: Optional[object] = None,
    ) -> Optional[StrategyRow]:
        """Detect whether ``new_text`` contradicts any strategy in ``existing``.

        Version 0 heuristic: for each existing strategy whose embedding
        is within ``_CONTRADICTION_SIMILARITY_THRESHOLD`` cosine distance
        of the new strategy's meaning, ask a local LLM whether the two
        strategies contradict each other.

        Falls back to keyword-based negation detection when the LLM
        client is unavailable.

        Returns the first conflicting ``StrategyRow``, or ``None``.
        """
        if not existing:
            return None

        for candidate in existing:
            if candidate.status != STATUS_ACTIVE:
                continue

            conflict = await _check_pair_contradiction(
                new_text, candidate.text, llm_client
            )
            if conflict:
                logger.info(
                    "strategy_contradiction_detected",
                    new_text_preview=new_text[:80],
                    conflicting_id=str(candidate.strategy_id),
                )
                return candidate

        return None


# ── Contradiction helpers ─────────────────────────────────────────────────────


_NEGATION_WORDS = frozenset({
    "never", "not", "don't", "do not", "avoid", "stop",
    "instead", "opposite", "contrary", "unlike", "rather than",
})


def _simple_negation_overlap(a: str, b: str) -> bool:
    """Return True if both texts share a key term AND one uses negation.

    Very conservative — only fires when the texts share substantive
    content words AND one flips polarity via a negation word.
    """
    a_lower = a.lower()
    b_lower = b.lower()

    a_words = set(a_lower.split())
    b_words = set(b_lower.split())

    # Stop-words to skip when checking overlap.
    stop = frozenset({
        "a", "an", "the", "is", "are", "in", "on", "of", "to", "for",
        "and", "or", "but", "with", "at", "by", "from", "as", "be",
        "it", "its", "this", "that", "when", "what", "which", "how",
        "always", "should", "must", "will", "can", "may",
    })

    content_a = a_words - stop
    content_b = b_words - stop

    # Require at least 2 shared content words.
    shared = content_a & content_b
    if len(shared) < 2:
        return False

    a_has_neg = bool(_NEGATION_WORDS & a_words)
    b_has_neg = bool(_NEGATION_WORDS & b_words)

    # One has negation and the other does not → possible contradiction.
    return a_has_neg != b_has_neg


async def _check_pair_contradiction(
    new_text: str,
    existing_text: str,
    llm_client: Optional[object],
) -> bool:
    """Return True if the two texts contradict each other.

    Tries LLM classification first; falls back to keyword heuristic.
    """
    if llm_client is not None:
        try:
            result = await _llm_contradiction_check(
                new_text, existing_text, llm_client
            )
            return result
        except Exception as exc:
            logger.warning(
                "strategy_contradiction_llm_failed",
                error=str(exc),
                fallback="keyword_heuristic",
            )

    return _simple_negation_overlap(new_text, existing_text)


async def _llm_contradiction_check(
    new_text: str,
    existing_text: str,
    llm_client: object,
) -> bool:
    """Ask the local LLM whether the two texts contradict each other.

    Uses a short, deterministic prompt and parses YES/NO from the reply.
    Always runs locally — never sends personal data to a frontier model.
    """
    prompt = (
        "Do the following two behavioral guidelines directly contradict "
        "each other?\n\n"
        f'Guideline A: "{new_text}"\n\n'
        f'Guideline B: "{existing_text}"\n\n'
        "Answer with exactly one word: YES if they contradict, NO if not."
    )
    messages = [{"role": "user", "content": prompt}]
    response = await llm_client.chat(  # type: ignore[union-attr]
        messages,
        local_only=True,
        options={"num_predict": 5},
    )
    content = (response.get("content") or "").strip().upper()
    return content.startswith("YES")

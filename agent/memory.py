from __future__ import annotations

import re
import json
import structlog
from collections import deque
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, delete, and_, text, func, literal
from sqlalchemy.ext.asyncio import AsyncSession
from pgvector.sqlalchemy import Vector
from agent.models import MemoryEvent, AuditLog

logger = structlog.get_logger()


class MemoryManager:
    def __init__(self, llm_client=None, db_session_factory=None):
        self._working: deque = deque(maxlen=50)
        self._llm = llm_client
        self._db_factory = db_session_factory

    # ─── Working Memory ───────────────────────────────────────────────────────

    def add_to_working_memory(self, role: str, content: str) -> None:
        self._working.append({
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat()
        })

    def get_working_memory(self, limit: int = 20) -> list[dict]:
        items = list(self._working)
        return [{"role": m["role"], "content": m["content"]} for m in items[-limit:]]

    def clear_working_memory(self) -> None:
        self._working.clear()

    # ─── Recall Memory ────────────────────────────────────────────────────────

    async def save_to_recall(self, content: str, importance: float = None) -> None:
        """Save content to recall memory. Auto-scores importance if not provided."""
        if not self._db_factory:
            logger.warning("memory_no_db", action="save_to_recall")
            return

        # Auto-score importance using local LLM if not provided
        if importance is None:
            importance = await self._score_importance(content)

        # Generate embedding
        embedding = None
        if self._llm:
            try:
                embedding = await self._llm.embed(content)
            except Exception as e:
                logger.warning("embedding_failed", error=str(e))

        try:
            async with self._db_factory() as session:
                event = MemoryEvent(
                    type="recall",
                    content=content,
                    importance_score=importance,
                    embedding=embedding,
                )
                session.add(event)
                await session.commit()
                logger.info("memory_saved", type="recall", importance=importance, preview=content[:80])
        except Exception as e:
            logger.error("memory_save_failed", error=str(e))

    # Epic 02 (#29) — recency-adjusted semantic search over recall memory.
    # Combined score: `final = α · sim + (1 − α) · exp(−Δt / τ)`, where
    # `sim = 1 − cosine_distance` is the semantic similarity in [0, 1] and
    # `Δt` is the row's age in days. `α` defaults to 0.7 — semantic still
    # leads, but recency breaks ties on queries like "lately". `τ` defaults
    # to 30 days for general queries; per-query overrides flow through
    # `time_window_days` ("lately" → 14, "ever" → None which disables
    # recency entirely).
    #
    # HNSW only fires when `ORDER BY` is the pure cosine-distance operator,
    # so we use a two-stage CTE: HNSW picks the top-K · OVERFETCH candidates
    # by similarity, then the outer query reranks them by the combined
    # score. With OVERFETCH = 4 and limit ≤ 10, the candidate set stays
    # under 40 rows — recency reranking can't surface anything outside
    # that window, but at our corpus sizes the recall hit is negligible
    # and the latency stays bounded.
    _RECENCY_OVERFETCH = 4
    DEFAULT_RECALL_ALPHA = 0.7
    DEFAULT_RECALL_TAU_DAYS = 30

    async def search_recall(
        self,
        query: str,
        limit: int = 10,
        time_window_days: int | None = DEFAULT_RECALL_TAU_DAYS,
        alpha: float = DEFAULT_RECALL_ALPHA,
    ) -> list[dict]:
        """Semantic search over recall memory, optionally recency-boosted.

        Default behavior (since #29): semantic similarity is blended with
        an exponential recency decay. Pass ``time_window_days=None`` to
        disable recency (the "ever" override) and recover pure semantic
        ranking; pass a smaller value for "lately"-class queries.

        ``alpha`` weights semantic similarity in the combined score; it
        defaults to ``0.7`` so semantic still leads. The combined score is
        surfaced as ``score`` so the RRF combiner in #28 can fuse this
        ranked list with the BM25 list returned by ``search_bm25``.
        """
        if not self._db_factory or not self._llm:
            return []
        if not query or not query.strip():
            logger.warning("search_recall_empty_query")
            return []
        if limit < 1:
            logger.warning("search_recall_invalid_limit", limit=limit)
            limit = 10

        try:
            query_embedding = await self._llm.embed(query)
        except Exception as e:
            logger.warning("embed_query_failed", error=str(e))
            return []

        try:
            async with self._db_factory() as session:
                # Stage 1: HNSW picks candidates by pure cosine distance,
                # so the index is actually used. Going through the ORM
                # column expression is what makes pgvector bind the list
                # correctly — raw `text(":v AS vector")` fails on asyncpg.
                distance = MemoryEvent.embedding.cosine_distance(query_embedding)
                if time_window_days is None:
                    # "Ever" — semantic-only, no rerank.
                    result = await session.execute(
                        select(
                            MemoryEvent.id,
                            MemoryEvent.content,
                            MemoryEvent.importance_score,
                            MemoryEvent.created_at,
                            (1 - distance).label("sim"),
                            (1 - distance).label("score"),
                        )
                        .where(
                            and_(
                                MemoryEvent.type == "recall",
                                MemoryEvent.embedding.is_not(None),
                            )
                        )
                        .order_by(distance)
                        .limit(limit)
                    )
                    rows = result.mappings().all()
                else:
                    if time_window_days <= 0:
                        logger.warning(
                            "search_recall_invalid_tau",
                            time_window_days=time_window_days,
                        )
                        time_window_days = self.DEFAULT_RECALL_TAU_DAYS
                    bounded_alpha = max(0.0, min(1.0, alpha))
                    overfetch = limit * self._RECENCY_OVERFETCH
                    cands = (
                        select(
                            MemoryEvent.id.label("id"),
                            MemoryEvent.content.label("content"),
                            MemoryEvent.importance_score.label("importance_score"),
                            MemoryEvent.created_at.label("created_at"),
                            (1 - distance).label("sim"),
                        )
                        .where(
                            and_(
                                MemoryEvent.type == "recall",
                                MemoryEvent.embedding.is_not(None),
                            )
                        )
                        .order_by(distance)
                        .limit(overfetch)
                        .subquery("cands")
                    )
                    age_seconds = func.extract(
                        "epoch", func.now() - cands.c.created_at
                    )
                    score = (
                        literal(bounded_alpha) * cands.c.sim
                        + (1 - literal(bounded_alpha))
                        * func.exp(-age_seconds / 86400.0 / float(time_window_days))
                    )
                    result = await session.execute(
                        select(
                            cands.c.id,
                            cands.c.content,
                            cands.c.importance_score,
                            cands.c.created_at,
                            cands.c.sim,
                            score.label("score"),
                        )
                        .order_by(score.desc(), cands.c.created_at.desc())
                        .limit(limit)
                    )
                    rows = result.mappings().all()

                return [
                    {
                        "id": int(r["id"]),
                        "content": r["content"],
                        "importance_score": float(r["importance_score"]),
                        "created_at": r["created_at"].isoformat(),
                        "score": float(r["score"]),
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("recall_search_failed", error=str(e))
            return []

    # Epic 02 (#27) — BM25 keyword search over the same recall window. Uses
    # `ts_rank_cd` (cover-density rank) over `plainto_tsquery('english', :q)`.
    # ts_rank_cd was chosen over plain ts_rank because it weights matches
    # by their proximity in the document, which gives more BM25-like
    # behavior on short memory entries where co-occurrence of terms is
    # the strongest signal. The constant `0.1` is the default normalization
    # weight for short documents; we let Postgres apply its defaults
    # rather than over-tune them.
    _BM25_SQL = text(
        """
        SELECT id, content, importance_score, created_at,
               ts_rank_cd(content_tsv, plainto_tsquery('english', :q)) AS score
        FROM memory_events
        WHERE type = 'recall'
          AND content_tsv @@ plainto_tsquery('english', :q)
        ORDER BY score DESC, created_at DESC
        LIMIT :k
        """
    )

    async def search_bm25(self, query: str, limit: int = 10) -> list[dict]:
        """BM25-style keyword search over recall memory.

        Returns ranked rows ordered by `ts_rank_cd` descending, with
        `created_at DESC` as the deterministic tie-breaker. Empty queries,
        queries that produce no tsquery terms, and queries with no
        matching rows all return ``[]``.

        Recall-only by design — symmetric with `search_recall`. The RRF
        combiner in #28 fuses the recall-side BM25 list with the
        recall-side semantic list. Archival keyword search can be added
        as a follow-up if comprehension data shows it's needed.

        BM25 is timestamp-agnostic; #29's recency boost lives on the
        semantic side, so this method intentionally takes no
        `time_window_days` parameter.

        Composes with `search_recall` via the RRF combiner in #28.
        """
        if not self._db_factory:
            return []
        if not query or not query.strip():
            logger.warning("bm25_search_empty_query")
            return []
        if limit < 1:
            logger.warning("bm25_search_invalid_limit", limit=limit)
            limit = 10
        try:
            async with self._db_factory() as session:
                result = await session.execute(
                    self._BM25_SQL, {"q": query, "k": limit}
                )
                rows = result.mappings().all()
                return [
                    {
                        "id": int(r["id"]),
                        "content": r["content"],
                        "importance_score": float(r["importance_score"]),
                        "created_at": r["created_at"].isoformat(),
                        "score": float(r["score"]),
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("bm25_search_failed", error=str(e))
            return []

    # Epic 02 (#28) — Reciprocal Rank Fusion combiner.
    # Standard RRF: `score(d) = Σ 1/(k + rank_i(d))` for k = 60. The constant
    # k=60 is a conventional default from Cormack et al. 2009; it dampens
    # the contribution of items deep in any single list. Both the formula
    # and the constant are candidates for replacement once #45 (DSPy
    # optimization) has trace volume to train a learned reranker against.
    # Until then, RRF is parameter-free and works without tuning — exactly
    # what we want before traces exist.
    DEFAULT_RRF_K = 60
    _HYBRID_OVERFETCH = 2  # fetch 2× limit from each source before fusion

    async def search_hybrid(
        self,
        query: str,
        limit: int = 10,
        time_window_days: int | None = DEFAULT_RECALL_TAU_DAYS,
        alpha: float = DEFAULT_RECALL_ALPHA,
        k_rrf: int = DEFAULT_RRF_K,
    ) -> list[dict]:
        """Hybrid retrieval: BM25 (#27) + recency-adjusted semantic (#29)
        fused by Reciprocal Rank Fusion.

        Runs both retrievers concurrently — neither shares a connection,
        so the dominant cost is whichever finishes last (typically the
        embed call inside ``search_recall``). Items present in both lists
        get additive RRF contributions and surface near the top.

        Returns rows in the same shape as the source methods, with
        ``score`` overwritten by the RRF score so callers (and the eval
        runner in #30) can introspect fusion strength.

        Forward-compat: ``k_rrf`` is exposed for the learned-reranker
        follow-up tracked under #45 / Q2. Default RRF stays the policy
        until that reranker measurably beats it on the eval set.
        """
        import asyncio  # local — keeps the module import cheap

        if limit < 1:
            logger.warning("search_hybrid_invalid_limit", limit=limit)
            limit = 10
        if k_rrf < 1:
            # `1/(k_rrf + rank)` goes negative for k_rrf < 0 and explodes
            # at k_rrf == -rank, so reject any non-positive value rather
            # than silently producing nonsense rankings.
            logger.warning("search_hybrid_invalid_k_rrf", k_rrf=k_rrf)
            k_rrf = self.DEFAULT_RRF_K

        overfetch = limit * self._HYBRID_OVERFETCH
        semantic_task = self.search_recall(
            query,
            limit=overfetch,
            time_window_days=time_window_days,
            alpha=alpha,
        )
        bm25_task = self.search_bm25(query, limit=overfetch)
        semantic, bm25 = await asyncio.gather(
            semantic_task, bm25_task, return_exceptions=True
        )

        # Graceful degradation — if one source raises (transient DB error,
        # Ollama timeout on the embed), fall back to whatever the other
        # produced. Matches the `return []` posture the source methods
        # already adopt internally, but defends against the gather case.
        if isinstance(semantic, Exception):
            logger.warning("search_hybrid_semantic_failed", error=str(semantic))
            semantic = []
        if isinstance(bm25, Exception):
            logger.warning("search_hybrid_bm25_failed", error=str(bm25))
            bm25 = []

        rrf_scores: dict[int, float] = {}
        rows_by_id: dict[int, dict] = {}
        for ranked in (semantic, bm25):
            for rank, row in enumerate(ranked, start=1):
                rid = int(row["id"])
                rrf_scores[rid] = rrf_scores.get(rid, 0.0) + 1.0 / (k_rrf + rank)
                # First sighting wins for the row payload — the per-source
                # `score` is overwritten below with the RRF score.
                rows_by_id.setdefault(rid, row)

        ordered_ids = sorted(rrf_scores, key=lambda rid: -rrf_scores[rid])[:limit]
        return [
            {**rows_by_id[rid], "score": rrf_scores[rid]}
            for rid in ordered_ids
        ]

    async def get_recent_recall(self, days: int = 30) -> list[MemoryEvent]:
        """Fetch recall events from the last N days."""
        if not self._db_factory:
            return []
        cutoff = datetime.utcnow() - timedelta(days=days)
        try:
            async with self._db_factory() as session:
                result = await session.execute(
                    select(MemoryEvent)
                    .where(and_(MemoryEvent.type == "recall", MemoryEvent.created_at >= cutoff))
                    .order_by(MemoryEvent.created_at.desc())
                )
                return result.scalars().all()
        except Exception as e:
            logger.error("get_recent_recall_failed", error=str(e))
            return []

    # ─── Archival Memory ──────────────────────────────────────────────────────

    async def compress_to_archival(self) -> dict:
        """
        Find recall events older than 30 days, group by ISO week,
        summarize each week with local LLM, save as archival, delete originals.
        """
        if not self._db_factory or not self._llm:
            logger.warning("compress_to_archival_skipped", reason="no db or llm")
            return {"compressed": 0, "weeks": 0}

        cutoff = datetime.utcnow() - timedelta(days=30)
        try:
            async with self._db_factory() as session:
                result = await session.execute(
                    select(MemoryEvent)
                    .where(and_(MemoryEvent.type == "recall", MemoryEvent.created_at < cutoff))
                    .order_by(MemoryEvent.created_at)
                )
                old_events = result.scalars().all()

            if not old_events:
                logger.info("compress_to_archival", events=0, weeks=0)
                return {"compressed": 0, "weeks": 0}

            # Group by ISO week
            weeks: dict[str, list[MemoryEvent]] = {}
            for event in old_events:
                week_key = event.created_at.strftime("%Y-W%W")
                weeks.setdefault(week_key, []).append(event)

            compressed_count = 0
            for week_key, events in weeks.items():
                events_text = "\n".join(f"- {e.content}" for e in events)
                avg_importance = sum(e.importance_score for e in events) / len(events)

                # Summarize week with local LLM
                summary_result = await self._llm.chat(
                    messages=[{
                        "role": "user",
                        "content": f"Summarize these memory events from week {week_key} into a single paragraph capturing the most important information:\n\n{events_text}\n\nBe concise and preserve key facts, decisions, and relationship updates."
                    }],
                    model=f"local/{self._llm.config.DEFAULT_LOCAL_MODEL}"
                )
                summary = summary_result.get("content", events_text[:500])

                # Embed the summary
                embedding = None
                try:
                    embedding = await self._llm.embed(summary)
                except Exception:
                    pass

                async with self._db_factory() as session:
                    # Save archival entry
                    archival = MemoryEvent(
                        type="archival",
                        content=summary,
                        summary=f"Week {week_key}: {len(events)} events compressed",
                        importance_score=avg_importance,
                        embedding=embedding,
                    )
                    session.add(archival)

                    # Delete original recall events
                    ids_to_delete = [e.id for e in events]
                    await session.execute(
                        delete(MemoryEvent).where(MemoryEvent.id.in_(ids_to_delete))
                    )

                    # Audit log
                    session.add(AuditLog(
                        event_type="memory_compression",
                        details=f"Compressed {len(events)} recall events from {week_key} into archival"
                    ))
                    await session.commit()

                compressed_count += len(events)
                logger.info("week_compressed", week=week_key, events=len(events))

            return {"compressed": compressed_count, "weeks": len(weeks)}

        except Exception as e:
            logger.error("compress_to_archival_failed", error=str(e))
            return {"compressed": 0, "weeks": 0, "error": str(e)}

    async def search_archival(self, query: str, limit: int = 5) -> list[dict]:
        """Semantic search over archival memory."""
        if not self._db_factory or not self._llm:
            return []

        try:
            query_embedding = await self._llm.embed(query)
            async with self._db_factory() as session:
                result = await session.execute(
                    select(MemoryEvent)
                    .where(MemoryEvent.type == "archival")
                    .order_by(MemoryEvent.embedding.cosine_distance(query_embedding))
                    .limit(limit)
                )
                events = result.scalars().all()
                return [
                    {
                        "id": e.id,
                        "content": e.content,
                        "importance_score": e.importance_score,
                        "created_at": e.created_at.isoformat(),
                        "type": "archival",
                    }
                    for e in events
                    if e.embedding is not None
                ]
        except Exception as e:
            logger.error("archival_search_failed", error=str(e))
            return []

    # ─── Context Building ─────────────────────────────────────────────────────

    async def build_context_for_query(self, query: str) -> str:
        """
        Search recall and archival for relevant memories.
        Returns a formatted context block to prepend to LLM calls.
        """
        text, _records = await self.build_context_with_provenance(query)
        return text

    async def build_context_with_provenance(
        self, query: str
    ) -> tuple[str, list[dict]]:
        """Same as ``build_context_for_query`` but also returns the rows.

        Issue #33 needs structured memory IDs and scores in trace
        provenance. Splitting the text-rendering and the result list
        keeps the existing string contract for legacy callers while
        giving the assembler / chat path the raw rows it needs to
        emit ``memory_ids``.

        Privacy: callers must NOT log the returned rows' ``content``
        — only ``id`` and ``score`` belong in provenance. The rendered
        text is the only sanctioned place ``content`` flows into the
        LLM context.
        """
        recall_results = await self.search_recall(query, limit=5)
        archival_results = await self.search_archival(query, limit=3)

        all_results = recall_results + archival_results
        if not all_results:
            return "", []

        lines = ["[Relevant memories from your history]"]
        for r in all_results:
            age = ""
            try:
                dt = datetime.fromisoformat(r["created_at"])
                delta = datetime.utcnow() - dt
                if delta.days > 0:
                    age = f" ({delta.days}d ago)"
            except Exception:
                pass
            lines.append(f"• {r['content']}{age}")
        lines.append("[End memories]")

        return "\n".join(lines), all_results

    # ─── Admin ────────────────────────────────────────────────────────────────

    async def reset_all(self) -> dict:
        """Wipe working memory, all memory_events rows, and all conversations rows."""
        self._working.clear()
        if not self._db_factory:
            return {"ok": True, "message": "Working memory cleared (no DB connected)"}
        try:
            async with self._db_factory() as session:
                result_mem = await session.execute(delete(MemoryEvent))
                from agent.models import Conversation
                result_conv = await session.execute(delete(Conversation))
                session.add(AuditLog(
                    event_type="memory_reset",
                    details="Full memory wipe requested by owner"
                ))
                await session.commit()
            deleted_mem = result_mem.rowcount
            deleted_conv = result_conv.rowcount
            logger.info("memory_reset", memory_events=deleted_mem, conversations=deleted_conv)
            return {
                "ok": True,
                "message": f"Wiped {deleted_mem} memory events and {deleted_conv} conversation records.",
            }
        except Exception as e:
            logger.error("memory_reset_failed", error=str(e))
            return {"error": str(e)}

    # ─── Private Helpers ──────────────────────────────────────────────────────

    async def _score_importance(self, content: str) -> float:
        """Ask local LLM to score importance 0.0-1.0."""
        if not self._llm:
            return 0.5
        try:
            result = await self._llm.chat(
                messages=[{
                    "role": "user",
                    "content": f"Rate the importance of saving this to long-term memory (respond with only a number between 0.0 and 1.0, where 1.0=critical life event, 0.5=useful to remember, 0.0=trivial/conversational filler):\n\n{content}"
                }],
                model=f"local/{self._llm.config.DEFAULT_LOCAL_MODEL}"
            )
            score_str = result.get("content", "0.5").strip()
            match = re.search(r'\d+\.?\d*', score_str)
            score = float(match.group()) if match else 0.5
            return max(0.0, min(1.0, score))
        except Exception:
            return 0.5

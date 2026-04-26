from __future__ import annotations

import re
import json
import structlog
from collections import deque
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, delete, and_
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

    async def search_recall(self, query: str, limit: int = 10) -> list[dict]:
        """Semantic search over recall memory using pgvector cosine similarity."""
        if not self._db_factory or not self._llm:
            return []

        if not query or not query.strip():
            logger.warning("search_recall_empty_query")
            return []

        try:
            query_embedding = await self._llm.embed(query)
        except Exception as e:
            logger.warning("embed_query_failed", error=str(e))
            return []

        try:
            async with self._db_factory() as session:
                # pgvector cosine distance operator: <=>
                result = await session.execute(
                    select(MemoryEvent)
                    .where(MemoryEvent.type == "recall")
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
                    }
                    for e in events
                    if e.embedding is not None
                ]
        except Exception as e:
            logger.error("recall_search_failed", error=str(e))
            return []

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
        recall_results = await self.search_recall(query, limit=5)
        archival_results = await self.search_archival(query, limit=3)

        all_results = recall_results + archival_results
        if not all_results:
            return ""

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

        return "\n".join(lines)

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

"""Synthetic fixture corpus + retriever for the retrieval eval unit test.

Deliberately non-personal — covers the five eval categories with fake but
plausible memory items so CI can exercise the runner without a populated
DB or the gitignored real eval set.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from agent.tests.retrieval_eval import EvalQuery


@dataclass(frozen=True)
class FixtureMemory:
    id: int
    content: str
    age_days: int  # how old the memory is, for temporal queries

    @property
    def created_at(self) -> datetime:
        return datetime.utcnow() - timedelta(days=self.age_days)


# Fixture memory corpus. Spans the five eval categories.
FIXTURE_CORPUS: tuple[FixtureMemory, ...] = (
    # factual recall
    FixtureMemory(1, "The router uses qwen3-embedding:0.6b for 1024-dim vectors.", 120),
    FixtureMemory(2, "Memory subsystem embeddings come from nomic-embed-text at 768-dim.", 100),
    FixtureMemory(3, "Postgres pgvector HNSW indexes use m=16 ef_construction=64.", 90),
    # person-context
    FixtureMemory(4, "Matthew started a new role at the design studio in March.", 5),
    FixtureMemory(5, "Matthew shipped the onboarding redesign last week.", 2),
    FixtureMemory(6, "Matthew was on parental leave through January.", 95),
    # temporal — recent items the recency boost should surface
    FixtureMemory(7, "Yesterday's standup flagged a regression in the ingest pipeline.", 1),
    FixtureMemory(8, "This week's roadmap review moved Epic 02 ahead of Epic 03.", 3),
    # open-loop / behind-on
    FixtureMemory(9, "TODO: write up the post-incident review for the auth outage.", 14),
    FixtureMemory(10, "Promised to send Sarah the architecture deck by end of month.", 10),
    # hybrid keyword + semantic — distinctive phrase + topical content
    FixtureMemory(11, "The doc 'routing-from-april.md' covers the regex-to-semantic migration plan.", 25),
    FixtureMemory(12, "April's routing notes describe the fallback policy for OOD queries.", 28),
)


_CORPUS_BY_ID = {m.id: m for m in FIXTURE_CORPUS}


def _tokenize(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if t]


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def make_fixture_retriever(use_recency: bool = False, recency_tau_days: float = 30.0):
    """Token-overlap retriever over the fixture corpus.

    Stand-in for "current pgvector-only" retrieval. Optionally applies a
    recency boost — used by the unit test to verify the runner picks up the
    same metric movement we expect from #29. Honors a per-query
    `time_window_days` override via `EvalQuery.time_window_days`.
    """

    async def retrieve(eval_query, k: int) -> list[int]:
        q_tokens = _tokenize(eval_query.query)
        tau = eval_query.time_window_days or recency_tau_days
        scored: list[tuple[float, int]] = []
        for mem in FIXTURE_CORPUS:
            sem = _jaccard(q_tokens, _tokenize(mem.content))
            if use_recency:
                rec = math.exp(-mem.age_days / tau)
                score = 0.7 * sem + 0.3 * rec
            else:
                score = sem
            scored.append((score, mem.id))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [mid for _, mid in scored[:k]]

    return retrieve


# A fixture eval set. Stable IDs reference FIXTURE_CORPUS above. Real eval
# sets live at agent/tests/retrieval_eval_set.jsonl (gitignored).
FIXTURE_EVAL_SET: tuple[EvalQuery, ...] = (
    EvalQuery(
        query="what embedding model does the router use",
        expected_ids=(1,),
        category="factual",
        notes="exact factual lookup",
    ),
    EvalQuery(
        query="dimensions of memory subsystem embeddings",
        expected_ids=(2,),
        category="factual",
    ),
    EvalQuery(
        query="what's been going on with Matthew lately",
        expected_ids=(4, 5),
        category="person",
        notes="recency should surface 4 and 5 above 6",
    ),
    EvalQuery(
        query="recent updates from the team",
        expected_ids=(7, 8),
        category="temporal",
        notes="last few days only",
    ),
    EvalQuery(
        query="what am I behind on",
        expected_ids=(9, 10),
        category="open_loop",
    ),
    EvalQuery(
        query="the doc about routing from april",
        expected_ids=(11, 12),
        category="hybrid",
        notes="distinctive keyword 'routing-from-april' + topical match",
    ),
)


def get_fixture_corpus_by_id(memory_id: int) -> FixtureMemory:
    return _CORPUS_BY_ID[memory_id]

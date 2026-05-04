"""Strategy Hub — interpretable decision strategies (PEARL-inspired).

The third memory layer, distinct from facts (memory) and identity. Each
strategy is a short natural-language sentence describing how Pepper
chooses to act in a class of situations. Strategies are versioned,
scored, and source-linked so that a future contributor (or the operator)
can see *why* Pepper did what it did, and so the reflector can propose
revisions through the propose-then-approve queue established in
ADR-0008.

This package ships the data layer. The query/update tools and UI
inspector land in #54; this issue (#53) is the schema, repository, and
bootstrap loader.

Privacy posture: strategies are RAW_PERSONAL (they encode patterns about
people in the operator's life) and live in the same Postgres database
as traces and memory.
"""

from agent.strategies.bootstrap import (
    BOOTSTRAP_STRATEGIES,
    bootstrap_if_empty,
)
from agent.strategies.repository import (
    Strategy,
    StrategyCreatedBy,
    StrategyRepository,
    StrategyStatus,
)

__all__ = [
    "BOOTSTRAP_STRATEGIES",
    "Strategy",
    "StrategyCreatedBy",
    "StrategyRepository",
    "StrategyStatus",
    "bootstrap_if_empty",
]

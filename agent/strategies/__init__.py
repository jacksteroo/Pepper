"""Strategy Hub — #53 / #54.

Accumulated behavioral strategies that guide Pepper's responses.
New versions are appended (never updated in-place); superseded rows
are marked via ``status='superseded'``. Proposed changes flow through
``PendingActionsQueue`` and are never written directly.
"""
from __future__ import annotations

# Embedding dimension must match nomic-embed-text (768-dim) used by
# the memory subsystem.  Defined here so other modules can import it
# without triggering a circular import via models.py.
STRATEGY_EMBEDDING_DIM: int = 768

# Lazy imports so the models package can import STRATEGY_EMBEDDING_DIM
# without triggering a circular import chain.
def __getattr__(name: str):  # type: ignore[override]  # module __getattr__
    if name == "StrategyRow":
        from agent.strategies.models import StrategyRow  # noqa: PLC0415
        return StrategyRow
    if name == "StrategyRepository":
        from agent.strategies.repository import StrategyRepository  # noqa: PLC0415
        return StrategyRepository
    raise AttributeError(f"module 'agent.strategies' has no attribute {name!r}")


__all__ = [
    "STRATEGY_EMBEDDING_DIM",
    "StrategyRow",
    "StrategyRepository",
]

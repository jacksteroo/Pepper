"""Trace substrate — canonical per-turn record of agent behavior.

See `docs/adr/0005-trace-schema.md` and `docs/trace-schema.md` for the
schema contract. This package starts as a schema stub (#18) and grows
into a full append-only repository (#20), emitter (#22), compression
policy (#21), and read-only HTTP surface (#24).
"""
from agent.error_classifier import DataSensitivity
from agent.traces.repository import TraceRepository
from agent.traces.schema import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_DEFAULT,
    PROMPT_VERSION_UNVERSIONED,
    Archetype,
    Trace,
    TraceTier,
    TriggerSource,
)

__all__ = [
    "Archetype",
    "DataSensitivity",
    "EMBEDDING_DIM",
    "EMBEDDING_MODEL_DEFAULT",
    "PROMPT_VERSION_UNVERSIONED",
    "Trace",
    "TraceRepository",
    "TraceTier",
    "TriggerSource",
]

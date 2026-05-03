"""Structlog setup for agent processes.

Mirrors the configuration in `agent/start.py` so every archetype gets
the same console renderer, ISO timestamps, and stdlib-bridge as Pepper
Core. Lives in `_shared/` because every archetype needs identical
logging — divergence between archetypes would make cross-process
debugging painful — and because the configuration is pure (no state
held at module scope).

Callers: `agents/runner.py` invokes `configure_logging(...)` once at
startup; archetype `main.py` modules then `structlog.get_logger(...)`
as usual.
"""
from __future__ import annotations

import logging
import sys

import structlog

DEFAULT_LOG_LEVEL = "INFO"

_NOISY_THIRD_PARTY = (
    "httpcore",
    "httpx",
    "asyncio",
    "apscheduler",
    "tzlocal",
    "sqlalchemy.engine",
)


def configure_logging(level: str = DEFAULT_LOG_LEVEL, *, archetype: str | None = None) -> None:
    """Configure structlog + stdlib root logger for an agent process.

    Idempotent: callers may invoke it more than once (tests do).
    `archetype`, when provided, is bound as a default field on every
    log line so multi-archetype log aggregation can filter by source.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list = [
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
    ]

    renderer = structlog.dev.ConsoleRenderer(
        exception_formatter=structlog.dev.plain_traceback,
    )

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        processors=shared_processors + [renderer],
    )

    if archetype is not None:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(archetype=archetype)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=shared_processors + [renderer],
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    for noisy in _NOISY_THIRD_PARTY:
        logging.getLogger(noisy).setLevel(logging.WARNING)

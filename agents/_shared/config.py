"""Env-loading helpers for agent processes.

Lives in `_shared/` because every archetype reads the same `.env` file
that Pepper Core reads — duplicating the loading logic per archetype
would let configuration drift between processes (different default
log level, different Postgres URL, different timezone). The helper
returns plain values; nothing is cached at module scope.

The Pepper-Core `Settings` object in `agent/config.py` is the
authoritative shape for the full env. Agents typically only need a
narrow slice (Postgres URL, log level, archetype name) and read it
through these helpers rather than importing the full `Settings`
object — that import would pull in orchestrator-only dependencies
and weaken the archetype's standalone-ness.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_POSTGRES_URL = "postgresql+asyncpg://pepper:pepper@localhost:5432/pepper"
DEFAULT_LOG_LEVEL = "INFO"


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """Narrow slice of env every agent process needs.

    Frozen so a long-running process cannot silently mutate its own
    config mid-flight. Construct once at startup; pass to the
    archetype's `run()` entrypoint.

    `notify_channel` names the Postgres `LISTEN/NOTIFY` channel the
    archetype subscribes to (per ADR-0006). Defaults to
    `f"{archetype}_trigger"`. Note that LISTEN/NOTIFY requires a
    libpq-style connection (psycopg) and does not interoperate with
    the `+asyncpg` driver SQLAlchemy URL — the archetype is
    responsible for opening that connection separately if it wants to
    LISTEN; `postgres_url` is the SQLAlchemy URL for everything else.
    """

    archetype: str
    postgres_url: str
    log_level: str
    notify_channel: str


def load_runtime_config(archetype: str) -> AgentRuntimeConfig:
    """Read environment into a runtime config bundle.

    Reads from `os.environ`; any `.env` loading must happen before this
    is called (the runner relies on the shell / docker-compose
    `env_file:` directive to populate the environment).
    """
    return AgentRuntimeConfig(
        archetype=archetype,
        postgres_url=os.environ.get("POSTGRES_URL", DEFAULT_POSTGRES_URL),
        log_level=os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL),
        notify_channel=os.environ.get(
            f"PEPPER_AGENT_{archetype.upper()}_NOTIFY_CHANNEL",
            f"{archetype}_trigger",
        ),
    )

"""Reflector main loop.

Boots the reflections schema (idempotent), opens a Postgres LISTEN
connection on the trigger channel, and on each notify runs a single
reflection pass over the previous 24h of traces.

The pass:
  1. Compute the window: [now - 24h, now] in UTC.
  2. Read the last 24h of traces via `agent.traces.repository`.
  3. Read the previous daily reflection (continuity).
  4. Render the user prompt; call the LLM with the system prompt.
  5. Embed the reflection text (qwen3-embedding:0.6b, 1024 dims).
  6. Persist to the `reflections` table.

Privacy: reflections are RAW_PERSONAL. The LLM call is forced
local-only (the trace contents and previous reflection both contain
raw personal data). The embed call is local by construction —
`agent.llm.ModelClient.embed_router` only ever talks to Ollama.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from agent.db import Base
from agent.traces import TraceRepository
from agents._shared.config import AgentRuntimeConfig
from agents._shared.db import make_engine, make_session_factory
from agents.reflector import alerts as _alerts  # noqa: F401  side-effect import for ORM registration
from agents.reflector import rollup as _rollup
from agents.reflector import store as rstore
from agents.reflector.listener import listen_for_triggers
from agents.reflector.migration import apply_reflections_migration
from agents.reflector.pattern_detector import detect_patterns
from agents.reflector.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    render_user_prompt,
    summarize_trace,
    voice_violations,
)

logger = structlog.get_logger(__name__)

REFLECTION_WINDOW_HOURS: int = 24

# Bound the prompt: even on a busy day we don't shovel 1000 turns at
# the LLM. Newest-first traces, capped here, then sorted oldest-first
# in the prompt.
MAX_TRACES_PER_REFLECTION: int = 60

# Hard cap on reflection text length before persist. A runaway local
# model can emit megabytes; the operator-readable artefact never
# needs to exceed a few paragraphs. We truncate with an explicit
# marker so the operator can see it happened, and `metadata_` records
# the original length.
MAX_REFLECTION_TEXT_CHARS: int = 16_000

# Wall-clock budget for one reflection pass: prompt assembly, LLM
# call, embed, persist. The LLM and embed calls each carry their own
# inner timeouts (180/240s chat, 120s embed). This outer budget must
# be at least chat+embed+headroom or a fully-used inner pair would
# cancel the rollup mid-embed and silently drop the row. Monthly
# chat=240s + embed=120s + ~60s headroom = 420s.
PASS_TIMEOUT_S: float = 420.0

# Allowlisted Ollama hosts. The privacy invariant is that
# RAW_PERSONAL trace content NEVER leaves the box, so the Ollama
# base URL must point at a local-loopback or in-cluster destination.
# A hostile env override (`OLLAMA_BASE_URL=http://attacker.example/`)
# would otherwise silently exfiltrate everything in the prompt.
_OLLAMA_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        # Docker-on-host: maps to the host's loopback via the
        # `extra_hosts` directive in `docker-compose.yml`.
        "host.docker.internal",
        # In-cluster service names (sibling docker-compose service).
        "ollama",
        "pepper-ollama",
    }
)

# Postgres notify channel identifier — same shape Postgres itself
# requires. We validate the env-supplied value to avoid the listener
# silently subscribing to a name nobody ever NOTIFIes.
_PG_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$", re.IGNORECASE)

# Per-cadence rollup channels. Names are fixed (not env-tunable) so
# `agent/scheduler.py` can reference them as compile-time constants.
# The daily channel is still env-tunable via `notify_channel` because
# that contract was set in #39.
WEEKLY_CHANNEL: str = "reflector_weekly_trigger"
MONTHLY_CHANNEL: str = "reflector_monthly_trigger"


def _resolve_timezone() -> ZoneInfo:
    """Read the operator timezone the trigger was fired in.

    Mirrors `agent.config.Settings.TIMEZONE` (default
    `America/Los_Angeles`). The reflector aligns its window to local
    days because the trigger fires at local 23:55, not UTC.
    """
    tz_name = os.environ.get("TIMEZONE", "America/Los_Angeles")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        logger.warning("reflector_bad_timezone_env", tz_name=tz_name)
        return ZoneInfo("UTC")


def _window_for_payload(
    payload: str, *, tz: ZoneInfo, now: datetime
) -> tuple[datetime, datetime]:
    """Resolve [window_start, window_end] in UTC for a NOTIFY payload.

    The trigger payload is a `YYYY-MM-DD` date in the operator's local
    TZ (see `agent/scheduler.py:fire_reflector_trigger`). The window
    is the full local day:
        [local 00:00 of D, local 00:00 of D+1)
    converted to UTC for the trace store query.

    If `payload` is malformed, falls back to "the local day that ended
    most recently" relative to `now`. We never return a future
    `window_end` — both bounds are clipped to `now` so a backfill run
    in the morning still computes the previous day's window.
    """
    parsed: Optional[date] = None
    try:
        parsed = date.fromisoformat(payload.strip())
    except (ValueError, AttributeError):
        logger.warning("reflector_payload_unparseable", payload=payload[:64])

    if parsed is None:
        # Fall back: yesterday in local TZ.
        local_now = now.astimezone(tz)
        parsed = (local_now - timedelta(days=1)).date()

    local_start = datetime.combine(parsed, time.min, tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    window_start = local_start.astimezone(timezone.utc)
    window_end = min(local_end.astimezone(timezone.utc), now)
    return window_start, window_end


async def _ensure_schema(engine: AsyncEngine) -> None:
    """Create the reflections table + indexes if they don't exist.

    Imports the trace ORM so its model registers with `Base.metadata`
    too — that way `create_all` is a no-op for tables Pepper Core
    already created and creates the new `reflections` table only.
    """
    # Side-effect imports to populate Base.metadata. We don't reference
    # the symbols, but the imports must happen.
    import agent.traces.models  # noqa: F401
    from agents.reflector.alerts import PatternAlertRow  # noqa: F401
    from agents.reflector.store import ReflectionRow  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await apply_reflections_migration(conn)
    logger.info("reflector_schema_ready")


async def _ollama_chat(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    ollama_base_url: str,
    timeout_s: float,
) -> tuple[str, str]:
    """Talk to local Ollama. Returns `(content, model_used)`.

    Forced local — the reflector never routes RAW_PERSONAL content to
    a frontier model. The function is parametric on system + user
    prompt so daily / weekly / monthly all flow through the same
    code path.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(f"{ollama_base_url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = (data.get("message") or {}).get("content") or ""
    return content.strip(), model


async def _generate_reflection_text(
    *,
    user_prompt: str,
    model: str,
    ollama_base_url: str,
    timeout_s: float,
) -> tuple[str, str]:
    """Daily-reflection LLM call. Convenience wrapper around _ollama_chat."""
    return await _ollama_chat(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        model=model,
        ollama_base_url=ollama_base_url,
        timeout_s=timeout_s,
    )


async def _embed_reflection(
    *,
    text: str,
    ollama_base_url: str,
    model: str,
    timeout_s: float,
) -> Optional[list[float]]:
    """Generate the reflection embedding. Returns None on failure.

    Embedding the reflection is best-effort: the operator-readable
    text is the load-bearing artefact. If the embedding fails we
    persist the row with `embedding IS NULL` so the partial HNSW
    index correctly excludes it. A follow-up backfill can fill it in.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{ollama_base_url}/api/embeddings",
                json={"model": model, "prompt": text},
            )
            resp.raise_for_status()
            embedding = resp.json().get("embedding") or []
        if len(embedding) != rstore.REFLECTION_EMBEDDING_DIM:
            logger.warning(
                "reflection_embed_wrong_dim",
                got=len(embedding),
                want=rstore.REFLECTION_EMBEDDING_DIM,
            )
            return None
        return embedding
    except Exception as exc:
        logger.warning(
            "reflection_embed_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return None


async def _run_one_reflection(
    *,
    config: AgentRuntimeConfig,
    session_factory,
    payload: str = "",
    now: Optional[datetime] = None,
    tz: Optional[ZoneInfo] = None,
) -> Optional[rstore.Reflection]:
    """Compute and persist one daily reflection. Returns the stored row.

    Returns `None` when the LLM produces an empty string OR when the
    `(tier, window_start)` uniqueness constraint fires (a duplicate
    NOTIFY for the same local day). Either way the operator can see
    the skip in the logs.
    """
    now = now or datetime.now(timezone.utc)
    tz = tz or _resolve_timezone()
    window_start, window_end = _window_for_payload(payload, tz=tz, now=now)

    ollama_base_url = _ollama_url_from_env()
    chat_model = _chat_model_from_env()
    embed_model = rstore.REFLECTION_EMBEDDING_MODEL_DEFAULT

    async with session_factory() as session:
        traces_repo = TraceRepository(session)
        # Newest-first; we'll re-sort to chronological for the prompt.
        traces = await traces_repo.query(
            since=window_start,
            until=window_end,
            limit=MAX_TRACES_PER_REFLECTION,
        )
        traces = list(traces)
        truncated = len(traces) >= MAX_TRACES_PER_REFLECTION
        if truncated:
            logger.warning(
                "reflector_traces_truncated",
                limit=MAX_TRACES_PER_REFLECTION,
                window_start=window_start.isoformat(),
                window_end=window_end.isoformat(),
            )
        traces.sort(key=lambda t: t.created_at)
        digests = [summarize_trace(t) for t in traces]

        reflections_repo = rstore.ReflectionRepository(session)
        previous = await reflections_repo.latest(tier=rstore.TIER_DAILY)

    user_prompt = render_user_prompt(
        window_start=window_start,
        window_end=window_end,
        digests=digests,
        previous_reflection_text=previous.text if previous is not None else None,
    )

    logger.info(
        "reflector_pass_starting",
        n_traces=len(digests),
        previous_id=previous.reflection_id if previous else None,
    )

    text, model_used = await _generate_reflection_text(
        user_prompt=user_prompt,
        model=chat_model,
        ollama_base_url=ollama_base_url,
        timeout_s=180.0,
    )
    if not text:
        logger.warning("reflector_empty_response", n_traces=len(digests))
        return None

    original_len = len(text)
    if original_len > MAX_REFLECTION_TEXT_CHARS:
        logger.warning(
            "reflector_text_truncated",
            original_len=original_len,
            cap=MAX_REFLECTION_TEXT_CHARS,
        )
        text = text[: MAX_REFLECTION_TEXT_CHARS - 3].rstrip() + "..."

    violations = voice_violations(text)
    if violations:
        logger.warning(
            "reflector_voice_violations",
            phrases=violations,
            n_traces=len(digests),
        )

    embedding = await _embed_reflection(
        text=text,
        ollama_base_url=ollama_base_url,
        model=embed_model,
        timeout_s=120.0,
    )

    metadata = {
        "trace_truncated": truncated,
        "voice_violations": violations,
        "original_text_len": original_len,
    }

    reflection = rstore.Reflection(
        text=text,
        window_start=window_start,
        window_end=window_end,
        tier=rstore.TIER_DAILY,
        previous_reflection_id=previous.reflection_id if previous else None,
        trace_count=len(digests),
        model_used=model_used,
        prompt_version=PROMPT_VERSION,
        embedding=embedding,
        embedding_model_version=embed_model if embedding is not None else None,
        metadata_=metadata,
    )

    async with session_factory() as session:
        repo = rstore.ReflectionRepository(session)
        try:
            await repo.append(reflection)
        except rstore.DuplicateReflectionError as exc:
            logger.warning(
                "reflector_duplicate_skipped",
                tier=exc.tier,
                window_start=exc.window_start.isoformat(),
            )
            return None

    # Pattern detection runs AFTER the reflection persists (#41 spec:
    # "Pattern detector runs nightly after the reflector"). It is
    # best-effort: a detector failure must not invalidate the day's
    # reflection. The reflector still returns the reflection.
    try:
        await detect_patterns(
            window_start=window_start,
            window_end=window_end,
            session_factory=session_factory,
        )
    except Exception as exc:
        logger.error(
            "pattern_detector_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )

    return reflection


class ReflectorConfigError(Exception):
    """Raised on boot when the reflector's env is misconfigured.

    Surfaces the failure clearly to the runner so docker-compose's
    `restart: unless-stopped` doesn't quietly loop on a bad URL.
    """


def _validate_ollama_url(url: str) -> str:
    """Reject Ollama URLs that point off-box.

    Privacy invariant: the reflector handles RAW_PERSONAL trace
    content. The Ollama destination MUST be local-loopback or an
    in-cluster service we trust. A hostile env that points the URL
    at an attacker-controlled host is the easiest exfiltration path
    in this whole substrate; we close it at boot.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ReflectorConfigError(
            f"OLLAMA_BASE_URL must use http or https, got {parsed.scheme!r}"
        )
    host = (parsed.hostname or "").lower()
    if host not in _OLLAMA_ALLOWED_HOSTS:
        raise ReflectorConfigError(
            f"OLLAMA_BASE_URL host {host!r} is not in the local allowlist; "
            f"the reflector refuses to send RAW_PERSONAL content off-box. "
            f"Allowed hosts: {sorted(_OLLAMA_ALLOWED_HOSTS)}"
        )
    return url


def _validate_notify_channel(channel: str) -> str:
    if not _PG_IDENT_RE.match(channel):
        raise ReflectorConfigError(
            f"notify channel {channel!r} is not a valid Postgres identifier"
        )
    return channel


def _ollama_url_from_env() -> str:
    """Read the Ollama base URL from the env, mirroring `agent.config`'s default.

    Validation lives in `_validate_ollama_url`; this helper just reads.
    """
    return os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")


def _chat_model_from_env() -> str:
    """Read the local chat model the reflector should use.

    Mirrors `agent.config.DEFAULT_LOCAL_MODEL` but does not import it
    — the reflector deliberately keeps its env surface narrow.
    """
    return os.environ.get("DEFAULT_LOCAL_MODEL", "hermes-4.3-36b-tools:latest")


def _make_rollup_adapters(*, ollama_base_url: str):
    """Build the chat/embed callables `agents.reflector.rollup` expects.

    The rollup module is decoupled from `httpx` for testability; this
    factory binds the live local-Ollama URL into closures that match
    the keyword shape `rollup._run_rollup` invokes them with.
    """

    async def _chat_fn(
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        timeout_s: float,
    ) -> tuple[str, str]:
        return await _ollama_chat(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            ollama_base_url=ollama_base_url,
            timeout_s=timeout_s,
        )

    async def _embed_fn(
        *, text: str, model: str, timeout_s: float
    ) -> Optional[list[float]]:
        return await _embed_reflection(
            text=text,
            ollama_base_url=ollama_base_url,
            model=model,
            timeout_s=timeout_s,
        )

    return _chat_fn, _embed_fn


async def _dispatch_trigger(
    *,
    channel: str,
    payload: str,
    config: AgentRuntimeConfig,
    session_factory,
    tz: ZoneInfo,
    ollama_base_url: str,
    daily_channel: str,
) -> Optional[rstore.Reflection]:
    """Route a single (channel, payload) pair to the right pipeline."""
    if channel == daily_channel:
        return await _run_one_reflection(
            config=config,
            session_factory=session_factory,
            payload=payload,
            tz=tz,
        )

    chat_fn, embed_fn = _make_rollup_adapters(ollama_base_url=ollama_base_url)
    chat_model = _chat_model_from_env()
    if channel == WEEKLY_CHANNEL:
        return await _rollup.run_weekly_rollup(
            payload=payload,
            session_factory=session_factory,
            chat_fn=chat_fn,
            embed_fn=embed_fn,
            chat_model=chat_model,
            chat_timeout_s=180.0,
            embed_timeout_s=120.0,
            tz=tz,
        )
    if channel == MONTHLY_CHANNEL:
        return await _rollup.run_monthly_rollup(
            payload=payload,
            session_factory=session_factory,
            chat_fn=chat_fn,
            embed_fn=embed_fn,
            chat_model=chat_model,
            chat_timeout_s=240.0,
            embed_timeout_s=120.0,
            tz=tz,
        )
    logger.warning("reflector_unknown_channel", channel=channel)
    return None


async def run(config: AgentRuntimeConfig) -> None:
    """Reflector entrypoint, called by `agents.runner`.

    Loop:
      - validate env (privacy + identifier checks; refuse on failure)
      - boot schema
      - LISTEN on the daily channel + the weekly + monthly rollup channels
      - on each notify, dispatch by channel under a wall-clock cap
      - exit cleanly on SIGTERM (the runner sets the stop event)
    """
    # Boot-time validation: refuse to start with an off-box Ollama URL
    # or a malformed Postgres notify identifier. Both of these are
    # privacy-relevant — see _validate_ollama_url's docstring.
    ollama_base_url = _validate_ollama_url(_ollama_url_from_env())
    daily_channel = _validate_notify_channel(config.notify_channel)
    if daily_channel in {WEEKLY_CHANNEL, MONTHLY_CHANNEL}:
        # An env override that aliases the daily channel onto a rollup
        # channel would make `_dispatch_trigger` route every notify
        # to the daily branch — silently skipping all rollup work.
        raise ReflectorConfigError(
            f"daily notify channel {daily_channel!r} collides with a "
            f"rollup channel; pick a different value for "
            f"PEPPER_AGENT_REFLECTOR_NOTIFY_CHANNEL"
        )
    weekly_channel = _validate_notify_channel(WEEKLY_CHANNEL)
    monthly_channel = _validate_notify_channel(MONTHLY_CHANNEL)
    channels = [daily_channel, weekly_channel, monthly_channel]

    logger.info(
        "reflector_run_starting",
        archetype=config.archetype,
        channels=channels,
    )

    engine = make_engine(config.postgres_url)
    session_factory = make_session_factory(engine)

    try:
        await _ensure_schema(engine)
    except Exception as exc:
        logger.error(
            "reflector_schema_init_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        await engine.dispose()
        raise

    stop = asyncio.Event()
    tz = _resolve_timezone()
    try:
        async for channel, payload in listen_for_triggers(
            sqlalchemy_url=config.postgres_url,
            channels=channels,
            stop=stop,
        ):
            logger.info(
                "reflector_trigger_received",
                channel=channel,
                payload=payload[:64],
            )
            try:
                reflection = await asyncio.wait_for(
                    _dispatch_trigger(
                        channel=channel,
                        payload=payload,
                        config=config,
                        session_factory=session_factory,
                        tz=tz,
                        ollama_base_url=ollama_base_url,
                        daily_channel=daily_channel,
                    ),
                    timeout=PASS_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "reflector_pass_timeout",
                    channel=channel,
                    timeout_s=PASS_TIMEOUT_S,
                )
                continue
            except Exception as exc:
                # Crash-isolation: one bad reflection must not kill the
                # listener. Log and keep waiting for the next NOTIFY.
                logger.error(
                    "reflector_pass_failed",
                    channel=channel,
                    error_type=type(exc).__name__,
                    error=str(exc)[:300],
                )
                continue
            if reflection is not None:
                logger.info(
                    "reflector_pass_done",
                    channel=channel,
                    tier=reflection.tier,
                    reflection_id=reflection.reflection_id,
                    trace_count=reflection.trace_count,
                )
    except asyncio.CancelledError:
        # The runner cancels us on SIGTERM/SIGINT; let it propagate.
        raise
    finally:
        await engine.dispose()
        logger.info("reflector_run_stopped")

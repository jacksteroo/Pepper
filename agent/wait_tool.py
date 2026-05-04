"""Wait — a first-class non-action.

The reflector and the scheduled brief flows occasionally need a way to
say "I considered surfacing this, and chose not to, for the following
reason." Today that decision is implicit: the model produces an empty
string and the scheduler interprets the absence as a successful no-op.
The implicit-empty path conflates restraint with failure (a dropped
brief, a model that lost track, a context-overflow retry that produced
nothing) and erases the reason.

`wait` makes restraint observable. It is a tool the model calls during
a turn; the call is recorded in the trace's `tools_called` field with
the operator-readable reason. The wait panel (`/api/waits` +
`web/src/components/Waits.tsx`) surfaces recent waits with reasons.
The scheduler treats a wait-resolved scheduled run as success, not
failure, and suppresses the user-facing send.

Privacy posture: wait reasons are RAW_PERSONAL. They commonly carry
sensitive observation about the operator's affect ("Jack seemed off,
don't pile on with the email triage"). They live in the `traces` table
and follow the same access controls as any other trace contents.

This module exposes:
- `WAIT_TOOLS`: the tool schema list (one entry, registers the `wait` tool).
- `execute_wait(args, *, registry, session_id)`: validator + side-effect.
- `WaitsRegistry`: per-session in-memory registry of "this turn waited",
  consulted by the scheduler immediately after each chat() call.
- `Wait` dataclass: the payload shape both the registry and the trace
  carry.
"""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Per #55 spec the only currently-supported `until` shapes are:
#   1. ISO-8601 timestamp (parsed; used for #56 wait-window expiry).
#   2. Free natural-language string ("after the meeting"); kept as-is,
#      not interpreted by the scheduler. The reflector is welcome to
#      do something with it later.
#   3. Omitted (the wait has no specific expiry).
# An empty string is rejected — that's a `None` masquerading as a value.
MAX_REASON_LEN: int = 2000
MAX_UNTIL_LEN: int = 200
_RECENT_WAITS_PER_SESSION: int = 16


# ── Tool schema ──────────────────────────────────────────────────────────────


WAIT_TOOLS = [
    {
        "type": "function",
        "side_effects": False,
        "function": {
            "name": "wait",
            "description": (
                "Choose not to surface anything for this turn, with a logged "
                "reason. Use when restraint is the right call: a recent topic "
                "Jack already addressed, an affect signal that suggests not "
                "piling on, or a brief that has nothing time-sensitive. The "
                "reason is recorded in the trace and surfaced in the wait "
                "panel — it should be specific enough that you would "
                "recognise the call as your own next week. The wait is a "
                "non-action: it does not send anything to Jack and does not "
                "queue a draft. If you instead want to defer something to a "
                "specific later moment, use `until` with an ISO timestamp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why you chose not to surface this turn. "
                            "First-person, specific. Required."
                        ),
                    },
                    "until": {
                        "type": "string",
                        "description": (
                            "Optional. ISO-8601 timestamp (e.g. "
                            "'2026-05-04T17:00:00Z') or natural-language "
                            "phrase (e.g. 'after the meeting') describing "
                            "when the situation should be re-evaluated."
                        ),
                    },
                },
                "required": ["reason"],
            },
        },
    },
]


# ── Wait payload + registry ──────────────────────────────────────────────────


@dataclass
class Wait:
    """A single recorded wait.

    `created_at` is when the wait fired. `until_iso` is the parsed ISO
    timestamp if `until` happened to be one; `until_raw` is the string
    the model passed (which may be natural language). The reflector's
    expiry-detection (#56) reads `until_iso`; the panel and the
    operator read `until_raw`.
    """

    reason: str
    session_id: str = ""
    until_raw: Optional[str] = None
    until_iso: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # ISO datetimes serialise as strings.
        d["created_at"] = self.created_at.isoformat()
        d["until_iso"] = self.until_iso.isoformat() if self.until_iso else None
        return d


class WaitsRegistry:
    """In-memory ring buffer of recent waits per session.

    Used by the scheduler to ask "did this turn end in a wait?" right
    after `pepper.chat()` returns. Bounded so a chatty session cannot
    leak unbounded memory. The persistent record lives in the trace
    store; this registry is operational ephemera.
    """

    def __init__(self, *, per_session_capacity: int = _RECENT_WAITS_PER_SESSION) -> None:
        self._per_session_capacity = per_session_capacity
        self._by_session: dict[str, deque[Wait]] = {}

    def record(self, wait: Wait) -> None:
        sid = wait.session_id or ""
        bucket = self._by_session.get(sid)
        if bucket is None:
            bucket = deque(maxlen=self._per_session_capacity)
            self._by_session[sid] = bucket
        bucket.append(wait)

    def consume_latest(self, session_id: str) -> Optional[Wait]:
        """Return the most recent wait for this session and remove it.

        `consume_` semantics: the scheduler asks "did the just-finished
        chat() turn end in a wait?" exactly once per turn. Subsequent
        reads on the same session_id return None until a fresh wait
        fires. Other sessions' buckets are untouched.
        """
        bucket = self._by_session.get(session_id)
        if not bucket:
            return None
        return bucket.pop()

    def peek_latest(self, session_id: str) -> Optional[Wait]:
        """Read-only variant of `consume_latest` for tests + diagnostics."""
        bucket = self._by_session.get(session_id)
        if not bucket:
            return None
        return bucket[-1]


# ── Validators ───────────────────────────────────────────────────────────────


def _coerce_str(value: Any, *, field_name: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if len(value) > max_len:
        raise ValueError(f"{field_name} exceeds {max_len} chars")
    return value


# Strict-enough ISO-8601 parser. Falls back to None on any failure —
# `until` is allowed to be natural language; we just do not pretend to
# know when it expires.
_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?$"
)


def _try_parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if not _ISO_RE.match(value.strip()):
        return None
    raw = value.strip().replace(" ", "T")
    # Python's fromisoformat accepts "+00:00" but not "Z" until 3.11+;
    # handle both shapes deterministically.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # ISO without zone — treat as UTC; better a definite tz than a
        # silent timezone-naive datetime that bites us at compare time.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Executor ─────────────────────────────────────────────────────────────────


async def execute_wait(
    args: dict[str, Any],
    *,
    registry: WaitsRegistry,
    session_id: str = "",
) -> dict[str, Any]:
    """Execute a `wait` tool call.

    Returns the LLM-facing tool result. On validation failure returns
    `{"error": "..."}` (matches the convention used elsewhere in
    `agent/core.py`).

    Side effect: the wait is recorded into `registry` keyed by
    `session_id` so the scheduler can consume it after the turn.
    The trace's `tools_called` entry is populated by the existing
    chat-turn logger plumbing (no extra wiring needed here).
    """
    raw_reason = args.get("reason")
    raw_until = args.get("until")

    try:
        reason = _coerce_str(raw_reason, field_name="reason", max_len=MAX_REASON_LEN)
        until_raw = _coerce_str(raw_until, field_name="until", max_len=MAX_UNTIL_LEN)
    except ValueError as exc:
        return {"error": str(exc)}

    reason = reason.strip()
    if not reason:
        # Required field. Hard error so the model learns the contract.
        return {"error": "wait requires a non-empty 'reason'"}

    until_raw = until_raw.strip() or None
    until_iso = _try_parse_iso(until_raw) if until_raw else None

    wait = Wait(
        reason=reason,
        session_id=session_id,
        until_raw=until_raw,
        until_iso=until_iso,
    )
    registry.record(wait)

    logger.info(
        "wait_recorded",
        session_id=session_id,
        reason_preview=reason[:200],
        until_raw=until_raw,
        until_iso=until_iso.isoformat() if until_iso else None,
    )

    return {
        "ok": True,
        "waited": True,
        "reason": reason,
        "until": until_raw,
        "until_iso": until_iso.isoformat() if until_iso else None,
    }

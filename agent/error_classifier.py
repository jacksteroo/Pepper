"""
Phase 3.3 — Error Classifier + Smart Fallback.

Classifies LLM API errors into actionable categories and routes to
privacy-safe fallback paths.  The core invariant: a call tagged
``local_only`` can NEVER be routed to a frontier (Anthropic) model under
any failure mode.

Usage in llm.py::

    try:
        result = await self._ollama_chat(...)
    except Exception as exc:
        category = classify_error(exc)
        decision = decide_fallback(category, data_sensitivity, model, config)
        if not decision.should_retry:
            raise ClassifiedLLMError(category, decision.user_message) from exc
        # sleep and retry with decision.model
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import structlog

logger = structlog.get_logger()


# ── Error categories ──────────────────────────────────────────────────────────

class ErrorCategory(str, Enum):
    """Mutually-exclusive failure modes for LLM API calls."""
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    NETWORK = "network"
    AUTH = "auth"
    MODEL_UNAVAILABLE = "model_unavailable"
    UNKNOWN = "unknown"


# ── Data sensitivity ──────────────────────────────────────────────────────────

class DataSensitivity(str, Enum):
    """Privacy classification for an LLM call.

    Used by the fallback router to enforce the privacy invariant:
    a ``LOCAL_ONLY`` call can never cross to a frontier model.
    """
    LOCAL_ONLY = "local_only"   # raw personal data — Ollama only, always
    SANITIZED = "sanitized"     # summaries/structured data — frontier ok if configured
    PUBLIC = "public"           # no personal data — frontier always ok


# ── Decision + error types ────────────────────────────────────────────────────

@dataclass
class FallbackDecision:
    """Outcome of the fallback router for a single failed LLM call."""
    model: str              # resolved model to use on the next attempt
    should_retry: bool      # False → raise ClassifiedLLMError to caller
    backoff_seconds: float  # 0 for immediate
    user_message: str       # actionable, specific — shown to the user


class ClassifiedLLMError(Exception):
    """Raised when all retry/fallback attempts for an LLM call are exhausted
    or the error type does not permit retrying (e.g. AUTH).

    Callers should surface ``user_message`` to the end user rather than the
    raw exception text.
    """

    def __init__(self, category: ErrorCategory, user_message: str) -> None:
        super().__init__(user_message)
        self.category = category
        self.user_message = user_message


# ── Context-overflow hint patterns ───────────────────────────────────────────

_OVERFLOW_PATTERNS = re.compile(
    r"context.{0,20}(window|length|overflow|limit|exceed)|"
    r"(prompt|input).{0,10}too.{0,10}long|"
    r"max.{0,10}tokens|"
    r"token.{0,20}limit",
    re.IGNORECASE,
)


# ── Classifier ────────────────────────────────────────────────────────────────

def classify_error(exc: Exception) -> ErrorCategory:
    """Map an exception raised by Ollama/Anthropic to an :class:`ErrorCategory`.

    Inspection order:
    1. Anthropic SDK typed exceptions (most specific)
    2. httpx HTTP status codes (Ollama)
    3. httpx network-layer exceptions
    4. Message-string heuristics (fallback for any provider)
    """
    msg = str(exc).lower()

    # -- Anthropic SDK ---------------------------------------------------------
    try:
        import anthropic  # type: ignore[import]

        if isinstance(exc, anthropic.RateLimitError):
            return ErrorCategory.RATE_LIMIT
        if isinstance(exc, anthropic.AuthenticationError):
            return ErrorCategory.AUTH
        if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
            return ErrorCategory.NETWORK
        if isinstance(exc, anthropic.BadRequestError):
            if _OVERFLOW_PATTERNS.search(str(exc)):
                return ErrorCategory.CONTEXT_OVERFLOW
        if isinstance(exc, anthropic.APIStatusError):
            sc = getattr(exc, "status_code", None)
            if sc in (529, 503, 502, 504):
                return ErrorCategory.MODEL_UNAVAILABLE
            if sc in (401, 403):
                return ErrorCategory.AUTH
            if sc == 429:
                return ErrorCategory.RATE_LIMIT
    except ImportError:
        pass

    # -- httpx (Ollama) --------------------------------------------------------
    try:
        import httpx  # type: ignore[import]

        if isinstance(exc, httpx.HTTPStatusError):
            sc = exc.response.status_code
            if sc == 429:
                return ErrorCategory.RATE_LIMIT
            if sc in (401, 403):
                return ErrorCategory.AUTH
            if sc in (500, 502, 503, 504):
                return ErrorCategory.MODEL_UNAVAILABLE
            if sc == 400:
                body = ""
                try:
                    body = exc.response.text
                except Exception:
                    pass
                if _OVERFLOW_PATTERNS.search(body):
                    return ErrorCategory.CONTEXT_OVERFLOW
                return ErrorCategory.MODEL_UNAVAILABLE

        if isinstance(exc, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError)):
            return ErrorCategory.NETWORK
    except ImportError:
        pass

    # -- String heuristics (provider-agnostic) ---------------------------------
    if _OVERFLOW_PATTERNS.search(msg):
        return ErrorCategory.CONTEXT_OVERFLOW

    if any(k in msg for k in ("connection", "timeout", "unreachable", "network error")):
        return ErrorCategory.NETWORK

    if any(k in msg for k in ("rate limit", "rate-limit", "too many requests")):
        return ErrorCategory.RATE_LIMIT

    if any(k in msg for k in ("unauthorized", "forbidden", "invalid api key", "authentication")):
        return ErrorCategory.AUTH

    if any(
        k in msg
        for k in (
            "model not found",
            "model is not available",
            "model unavailable",
            "internal server error",
        )
    ):
        return ErrorCategory.MODEL_UNAVAILABLE

    return ErrorCategory.UNKNOWN


# ── Fallback router ───────────────────────────────────────────────────────────

def decide_fallback(
    category: ErrorCategory,
    data_sensitivity: str,   # DataSensitivity value or raw string
    original_model: str,
    config,
) -> FallbackDecision:
    """Return a :class:`FallbackDecision` for the given failure.

    Privacy invariant (enforced here AND in ModelClient.chat):
    - A call tagged ``local_only`` can NEVER be routed to a frontier model.
    - Frontier → local fallback is only allowed for ``sanitized`` / ``public`` data.

    Fallback matrix:
    ┌──────────────────────┬─────────────────────────────────────────────────┐
    │ Failure              │ Allowed fallback                                │
    ├──────────────────────┼─────────────────────────────────────────────────┤
    │ Ollama unavailable   │ Surface error — no cross-provider fallback      │
    │ Claude rate limit    │ Exponential backoff on Claude,                  │
    │                      │ OR fall back to Ollama if sanitized/public      │
    │ Context overflow     │ Raise — caller (core.py) handles compression    │
    │ Auth failure         │ Surface to user, no retry                       │
    │ Network              │ Retry same provider with jitter (up to caller)  │
    └──────────────────────┴─────────────────────────────────────────────────┘
    """
    is_local = original_model.startswith("local/")
    local_model = f"local/{config.DEFAULT_LOCAL_MODEL}"
    # Use == rather than str() to avoid Python-version differences in str(StrEnum)
    is_local_only = data_sensitivity == DataSensitivity.LOCAL_ONLY

    # -- AUTH: never retry, always surface -------------------------------------
    if category == ErrorCategory.AUTH:
        provider = "Ollama" if is_local else "Claude API"
        return FallbackDecision(
            model=original_model,
            should_retry=False,
            backoff_seconds=0.0,
            user_message=(
                f"{provider} authentication failed — check your API key or OAuth "
                "tokens. No retry attempted."
            ),
        )

    # -- CONTEXT OVERFLOW: bubble up to core.py for compression ---------------
    if category == ErrorCategory.CONTEXT_OVERFLOW:
        return FallbackDecision(
            model=original_model,
            should_retry=False,
            backoff_seconds=0.0,
            user_message=(
                "The conversation has grown too long for the model's context "
                "window. Compressing and retrying automatically..."
            ),
        )

    # -- RATE LIMIT ------------------------------------------------------------
    if category == ErrorCategory.RATE_LIMIT:
        if is_local:
            # Ollama local rate limit: retry same with short backoff
            return FallbackDecision(
                model=original_model,
                should_retry=True,
                backoff_seconds=5.0,
                user_message="Local model rate-limited; retrying in 5s.",
            )
        # Frontier rate limit
        if is_local_only:
            # Privacy constraint: cannot fall back to local (should never happen
            # — a local_only call should never have been routed to a frontier
            # model in the first place).  Retry the frontier model with backoff
            # rather than violate the invariant.
            logger.error(
                "rate_limit_local_only_on_frontier",
                model=original_model,
                sensitivity=data_sensitivity,
            )
            return FallbackDecision(
                model=original_model,
                should_retry=True,
                backoff_seconds=30.0,
                user_message=(
                    "Claude API rate-limited. Retrying in 30s. "
                    "(Privacy constraint prevents local fallback for this call.)"
                ),
            )
        # sanitized / public: fall back to local immediately
        return FallbackDecision(
            model=local_model,
            should_retry=True,
            backoff_seconds=1.0,
            user_message="Claude API rate-limited; falling back to local model.",
        )

    # -- MODEL UNAVAILABLE -----------------------------------------------------
    if category == ErrorCategory.MODEL_UNAVAILABLE:
        if is_local:
            return FallbackDecision(
                model=original_model,
                should_retry=True,
                backoff_seconds=2.0,
                user_message=(
                    "Local model (Ollama) is temporarily unavailable. "
                    "Pepper will retry automatically; if this keeps happening, "
                    "confirm Ollama is running with `ollama serve`."
                ),
            )
        # Frontier unavailable — fall back to local if privacy allows
        if is_local_only:
            return FallbackDecision(
                model=original_model,
                should_retry=False,
                backoff_seconds=0.0,
                user_message=(
                    "Claude API is unavailable. "
                    "Privacy constraint prevents local fallback for this call. "
                    "Please try again later."
                ),
            )
        return FallbackDecision(
            model=local_model,
            should_retry=True,
            backoff_seconds=1.0,
            user_message="Claude API unavailable; falling back to local model.",
        )

    # -- NETWORK ---------------------------------------------------------------
    if category == ErrorCategory.NETWORK:
        provider = "Ollama" if is_local else "Claude API"
        return FallbackDecision(
            model=original_model,
            should_retry=True,
            backoff_seconds=3.0,
            user_message=f"Network error reaching {provider}; retrying in 3s.",
        )

    # -- UNKNOWN ---------------------------------------------------------------
    return FallbackDecision(
        model=original_model,
        should_retry=False,
        backoff_seconds=0.0,
        user_message=(
            "An unexpected error occurred. Check the logs for details."
        ),
    )

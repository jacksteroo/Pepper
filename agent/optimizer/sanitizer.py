"""PII-in-prompt detector.

Acceptance criterion from #45: "PII-in-prompt detector catches the obvious
cases on a test set."

Strategy: load ``data/life_context.md`` (the canonical PII source for this
machine — owner name, addresses, contacts, etc.) and check whether any of
its tokens appear in a candidate prompt's text. The detector is
conservative — it errs toward false positives. False positives are cheap
(operator inspects + acknowledges); false negatives leak.

What counts as "obvious":

- Quoted-string literals (``"Jack Chan"``, ``'jack@example.com'``).
- Bare tokens >= 4 chars that appear in life_context (case-insensitive).
- Email-like and phone-like patterns regardless of life_context content.

What this is NOT:

- A general-purpose PII classifier. It does not detect arbitrary
  emails, names, or addresses unless they're in life_context. The point
  is to catch the obvious case where an optimizer transcribed a literal
  from a trace into the prompt — not to be a privacy oracle.
- A blocker. It returns the matches; the caller (eval gate, CLI) decides
  what to do. The acceptance flow blocks promotion on any match by
  default; #48 wires that policy.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_LIFE_CONTEXT_PATH = Path("data/life_context.md")

# Generic patterns we always flag, regardless of life_context content.
# Conservative — these are the patterns most likely to leak through an
# optimizer that scraped a trace verbatim into a candidate prompt.
#
# Both regexes use bounded-length character classes to keep backtracking
# linear; the caller additionally caps prompt input at MAX_SCAN_BYTES.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
# Phone — international or US format. Loose by design.
PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")

# Hard cap on what we'll scan. Prompts of legitimate optimization-target
# size are well under 100KB; anything larger is either a bug or a misuse
# pattern we don't want to spend regex-engine cycles on. Above the cap
# we flag the input itself as a finding and skip further scanning, which
# is fail-loud rather than fail-silent.
MAX_SCAN_BYTES: int = 256 * 1024

# Minimum token length for life-context-derived matches. Below this we get
# noise (single names, common words) without much signal.
_MIN_TOKEN_LEN = 4

# Tokens stripped from the life-context corpus before matching. Markdown
# noise + common English words that would otherwise drown the signal.
_STOPWORDS = frozenset({
    "this", "that", "with", "from", "have", "been", "they", "them", "their",
    "your", "would", "could", "should", "about", "what", "when", "where",
    "which", "while", "after", "before", "into", "over", "than", "then",
    "some", "more", "most", "such", "very", "much", "many", "just", "also",
    "even", "only", "well", "like", "true", "false", "none", "null",
    "user", "users", "data", "code", "http", "https", "html", "json",
    "note", "notes", "name", "names", "type", "types", "list", "lists",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'.-]+")


def _tokenize_life_context(text: str) -> set[str]:
    """Extract candidate-PII tokens from a life-context document.

    Lowercased; stopwords and tokens shorter than ``_MIN_TOKEN_LEN``
    dropped. Markdown headers/lists/code-fences contribute their content
    too (they often hold the names we care about).
    """
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(text):
        t = raw.lower().strip("'.-")
        if len(t) < _MIN_TOKEN_LEN:
            continue
        if t in _STOPWORDS:
            continue
        tokens.add(t)
    return tokens


def load_life_context_tokens(
    path: Path | str | None = None,
) -> frozenset[str]:
    """Load life_context.md and return its tokenset.

    Returns an empty frozenset if the file is missing — sanitization
    still runs (email/phone patterns), it just won't have life-context
    tokens to match against. Logged so the operator notices.
    """
    p = Path(path or DEFAULT_LIFE_CONTEXT_PATH)
    if not p.is_absolute():
        repo_root = Path(__file__).resolve().parent.parent.parent
        p = repo_root / p
    if not p.exists():
        logger.warning("optimizer.sanitizer.life_context_missing", path=str(p))
        return frozenset()
    return frozenset(_tokenize_life_context(p.read_text(encoding="utf-8")))


def scan(
    prompt_text: str,
    *,
    life_context_tokens: Optional[Iterable[str]] = None,
    life_context_path: Path | str | None = None,
) -> list[str]:
    """Return a list of human-readable findings; empty list ⇒ clean.

    Each finding is a short string suitable for logging or surfacing to
    the operator (e.g. ``"life_context token: 'jacksteroo'"``,
    ``"email: 'a@b.com'"``).

    Either pass ``life_context_tokens`` directly (preferred for tests
    and CLI re-use) or let ``scan`` load them from ``life_context_path``.
    """
    findings: list[str] = []
    prompt_bytes = len(prompt_text.encode("utf-8", errors="replace"))
    if prompt_bytes > MAX_SCAN_BYTES:
        return [
            f"oversized prompt ({prompt_bytes} bytes > {MAX_SCAN_BYTES}); "
            "refused to scan (potential ReDoS / OOM input)",
        ]
    lower_prompt = prompt_text.lower()

    if life_context_tokens is None:
        life_context_tokens = load_life_context_tokens(life_context_path)
    # Normalise to lowercase tokenset.
    tokens = {t.lower() for t in life_context_tokens}

    # 1. Life-context tokens that appear in the prompt.
    # Walk the prompt's tokens (not substring search) so "user" in life_context
    # doesn't match "userPrompt".
    for raw in _TOKEN_RE.findall(lower_prompt):
        t = raw.strip("'.-")
        if len(t) < _MIN_TOKEN_LEN:
            continue
        if t in tokens:
            findings.append(f"life_context token: {t!r}")

    # 2. Email-like patterns.
    for m in EMAIL_RE.finditer(prompt_text):
        findings.append(f"email-like: {m.group(0)!r}")

    # 3. Phone-like patterns.
    for m in PHONE_RE.finditer(prompt_text):
        findings.append(f"phone-like: {m.group(0)!r}")

    # Dedupe but preserve first-seen order.
    seen: set[str] = set()
    unique: list[str] = []
    for f in findings:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique

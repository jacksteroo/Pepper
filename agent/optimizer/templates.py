"""Active-template loader for versioned prompts.

Production code that wants the *current* prompt for an optimizer
target calls ``load_active_template(target, default)``. The loader
checks the prompt store for an ``ACCEPTED`` candidate and returns its
text, falling back to ``default`` if no accepted prompt exists.

Promotion semantics
-------------------

The optimizer writes ``CANDIDATE`` records to
``data/optimizer/candidates/<target>/`` (gitignored). Operator
promotion via the eval gate (#48) writes ``ACCEPTED`` records to
``agent/prompts/<target>/`` (committed). This loader reads only the
committed accepted set — never the local candidate set — so production
code can never accidentally consume an unvetted prompt.

Multiple ``ACCEPTED`` records can coexist for one target (e.g. during
A/B comparison). The loader picks the most recent by ``created_at``;
older accepted prompts remain on disk for rollback via the
``rollback`` CLI subcommand (#46).

Caching
-------

Templates are read every call by default — the file system access is
cheap (≤ 100 small files per target) and the loader's callers are not
on the hot path. Tests can monkeypatch
``ACCEPTED_PROMPTS_DIR`` to swap the read root.
"""
from __future__ import annotations

from pathlib import Path

import structlog

from agent.optimizer.schema import PromptStatus
from agent.optimizer.storage import DEFAULT_ACCEPTED_DIR, PromptStore

logger = structlog.get_logger(__name__)

# Re-exported so tests can monkeypatch a single name.
ACCEPTED_PROMPTS_DIR: Path = DEFAULT_ACCEPTED_DIR


def load_active_template(target: str, default: str) -> str:
    """Return the text of the most-recent ACCEPTED prompt for ``target``.

    Returns ``default`` if:
      - the accepted-prompts directory does not exist,
      - the target subdirectory does not exist,
      - no ACCEPTED candidate exists for the target.

    Logs a single ``optimizer.template.loaded`` event with the
    version_hash so the trace ledger can correlate which prompt was
    active at the moment a turn ran.
    """
    if not ACCEPTED_PROMPTS_DIR.exists():
        return default
    store = PromptStore(ACCEPTED_PROMPTS_DIR)
    accepted = store.list(target, status=PromptStatus.ACCEPTED)
    if not accepted:
        return default
    chosen = accepted[0]  # store.list is sorted newest-first
    logger.debug(
        "optimizer.template.loaded",
        target=target,
        version_hash=chosen.version_hash,
    )
    return chosen.prompt_text


def list_accepted_versions(target: str) -> list[str]:
    """Return version_hashes of ACCEPTED prompts, newest first.

    Used by the ``rollback`` CLI subcommand and by operators inspecting
    promotion history.
    """
    if not ACCEPTED_PROMPTS_DIR.exists():
        return []
    store = PromptStore(ACCEPTED_PROMPTS_DIR)
    return [c.version_hash for c in store.list(target, status=PromptStatus.ACCEPTED)]

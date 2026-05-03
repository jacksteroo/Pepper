"""LifeContextSelector — wraps :func:`agent.life_context.build_system_prompt`.

The selector is intentionally thin: ``build_system_prompt`` already does the
heavy lifting (load soul + life context + capability block + schedule). The
selector adds a JSON-serializable provenance record that names which life
context sections were available so #33 can attribute prompt chunks back to
sources for trace analysis.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import SelectorRecord
from agent.life_context import (
    build_system_prompt,
    get_life_context_sections,
    get_owner_name,
)


class LifeContextSelector:
    """Build the base system prompt from soul + capabilities + life context.

    The selector caches the rendered system prompt across turns — same as
    the previous ``self._system_prompt`` attribute on Pepper — because the
    underlying file rarely changes mid-session. Callers must invalidate by
    calling :meth:`refresh` after a successful ``update_life_context`` write.
    """

    name = "life_context"

    def __init__(
        self,
        life_context_path: str,
        config: Any,
        capability_registry: Any | None = None,
    ) -> None:
        self._life_context_path = life_context_path
        self._config = config
        self._capability_registry = capability_registry
        self._cached_prompt: str | None = None
        self._cached_sections: dict[str, str] | None = None
        # ``owner_name`` is read on every turn for provenance. Resolving it
        # falls back to ``get_owner_name`` which re-parses life_context.md
        # — cheap once but expensive on every turn for cached prompts.
        # Cache it next to ``_cached_sections`` and invalidate together.
        self._cached_owner_name: str | None = None

    def refresh(self) -> None:
        """Drop cached prompt + sections so the next ``select`` call rebuilds.

        Called after the model rewrites ``life_context.md`` so subsequent
        turns pick up the new content.
        """
        self._cached_prompt = None
        self._cached_sections = None
        self._cached_owner_name = None

    def prime(self, prompt: str) -> None:
        """Inject a pre-built system prompt, bypassing the lazy rebuild.

        Used by ``PepperCore.initialize`` so the assembler shares the exact
        string that's pinned to ``self._system_prompt`` for backwards-compat
        introspection. Also gives unit tests that set ``_system_prompt``
        directly (without invoking ``initialize``) a way to keep the
        assembler in sync without re-reading the life-context file.
        """
        self._cached_prompt = prompt
        # Sections + owner_name caches stay None so the first reader still
        # parses the file — neither is reachable from a primed prompt string
        # alone. They populate on the next ``select`` call.

    def _resolve_owner_name(self) -> str:
        """Resolve owner_name with a fail-soft fallback to empty string."""
        try:
            return get_owner_name(self._life_context_path, self._config)
        except Exception:
            # get_owner_name does its own fallback; defence in depth.
            return ""

    def select(self) -> SelectorRecord:
        if self._cached_prompt is None:
            self._cached_prompt = build_system_prompt(
                self._life_context_path,
                self._config,
                self._capability_registry,
            )
            self._cached_sections = get_life_context_sections(
                self._life_context_path
            )
            # Refresh owner_name on the cache miss path so the next N
            # cache hits don't re-read life_context.md.
            self._cached_owner_name = self._resolve_owner_name()
        elif self._cached_owner_name is None:
            # Prompt was primed (so prompt cache is hot) but owner_name was
            # never resolved. Resolve once and cache.
            self._cached_owner_name = self._resolve_owner_name()

        sections = self._cached_sections or {}
        owner = self._cached_owner_name or ""

        # ``build_system_prompt`` embeds the entire ``life_context.md`` body
        # verbatim into the system prompt — every section parsed from the
        # file is therefore "used". ``life_context_sections_used`` (the #33
        # required key) is the same list as ``sections_loaded``; we emit
        # both so legacy consumers of ``sections_loaded`` keep working.
        section_names = sorted(sections.keys())
        provenance = {
            "selector": self.name,
            "life_context_path": self._life_context_path,
            "owner_name": owner,
            "sections_loaded": section_names,
            "life_context_sections_used": section_names,
            "section_count": len(sections),
            "system_prompt_chars": len(self._cached_prompt or ""),
        }
        return SelectorRecord(
            name=self.name,
            content=self._cached_prompt or "",
            provenance=provenance,
        )

"""Human-readable reason annotations for assembly decisions.

Issue #33 (E3) wires raw provenance into traces so the reflector and
optimizer can do their work. Humans inspecting a single trace want
something more legible than ``{"memory_ids": [["uuid", 0.71], ...]}``.

This module provides :func:`annotate` — a thin wrapper that walks an
:class:`AssembledContext` and produces a ``selector_name -> reason``
map suitable for the upcoming inspector UI (#34). It does NOT mutate
the assembler or the persisted trace shape — it's a read-only
projection over the same provenance dict that already lives on the
trace.

Privacy: same constraint as the rest of provenance. We name memory IDs
and life-context section names, never raw memory text.
"""

from __future__ import annotations

from typing import Any

from agent.context.types import AssembledContext, SelectorRecord


def annotate(context: AssembledContext) -> dict[str, str]:
    """Return a dict of ``selector_name -> human-readable reason``.

    Every named selector currently in ``context.selectors`` gets a
    string. Selectors that produced no content still return a string
    explaining the absence — the dict is shape-stable across turns.
    """
    out: dict[str, str] = {}
    for name, record in context.selectors.items():
        out[name] = _explain(name, record)
    return out


def _explain(name: str, record: SelectorRecord) -> str:
    explainer = _EXPLAINERS.get(name, _explain_default)
    try:
        return explainer(record.provenance or {})
    except Exception:
        # Defence in depth — annotation is a debug aid, never break the
        # turn or the trace persist path on a bad provenance dict.
        return f"{name}: provenance present"


def _explain_life_context(p: dict[str, Any]) -> str:
    sections = list(p.get("life_context_sections_used") or [])
    n = len(sections)
    if n == 0:
        return (
            "life_context: no sections found in life_context.md "
            "(file empty or missing)"
        )
    preview = ", ".join(sections[:3])
    if n > 3:
        preview += f", … (+{n - 3})"
    return (
        f"life_context: included {n} section(s) from life_context.md — "
        f"{preview}"
    )


def _explain_capability_block(p: dict[str, Any]) -> str:
    available = list(p.get("available_sources") or [])
    version = p.get("capability_block_version") or "unknown"
    if not available:
        return (
            f"capability_block: no live sources (version={version}); "
            "registry empty or all subsystems unavailable"
        )
    preview = ", ".join(available[:5])
    if len(available) > 5:
        preview += f", … (+{len(available) - 5})"
    return (
        f"capability_block: {len(available)} live source(s) "
        f"(version={version}) — {preview}"
    )


def _explain_retrieved_memory(p: dict[str, Any]) -> str:
    ids = list(p.get("memory_ids") or [])
    n = len(ids)
    if n == 0:
        if p.get("present"):
            # Caller threaded a context string but no structured rows —
            # legacy code path. Be explicit so optimizer dashboards can
            # see the gap.
            return (
                "retrieved_memory: context block included, "
                "no structured memory IDs threaded"
            )
        return "retrieved_memory: no recall hits for this query"
    return (
        f"retrieved_memory: {n} memory ID(s) selected by top-{n} "
        "score on query embedding (recall + archival, RRF blended)"
    )


def _explain_skill_match(p: dict[str, Any]) -> str:
    if not p.get("included"):
        return (
            "skill_match: skills index suppressed for this turn "
            "(ANSWER_FROM_CONTEXT or scheduler turn)"
        )
    match = p.get("skill_match")
    if match is None:
        n = int(p.get("n_skills") or 0)
        return (
            f"skill_match: skills index exposed ({n} skill(s)); "
            "no per-turn match — model selects via skill_view "
            "(progressive disclosure)"
        )
    return (
        f"skill_match: matched {match.get('skill_name')!r} "
        f"on trigger {match.get('trigger')!r} "
        f"(similarity={match.get('similarity_score')})"
    )


def _explain_last_n_turns(p: dict[str, Any]) -> str:
    if p.get("isolated"):
        return "last_n_turns: isolated turn — no working-memory history included"
    n = int(p.get("last_n_turns") or 0)
    msgs = int(p.get("n_messages") or 0)
    limit = int(p.get("limit") or 0)
    return (
        f"last_n_turns: included {n} turn(s) ({msgs} messages) "
        f"capped at limit={limit} from working memory"
    )


def _explain_default(p: dict[str, Any]) -> str:
    sel = p.get("selector") or "selector"
    return f"{sel}: provenance present"


_EXPLAINERS = {
    "life_context": _explain_life_context,
    "capability_block": _explain_capability_block,
    "retrieved_memory": _explain_retrieved_memory,
    "skill_match": _explain_skill_match,
    "last_n_turns": _explain_last_n_turns,
}

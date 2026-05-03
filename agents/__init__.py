"""Cognitive specialists.

`agents/` (plural) is the home for long-running cognitive processes —
reflectors, monitors, researchers — that consume traces and produce
reflections, compressions, summaries, or research outputs.

Distinct from `agent/` (singular, the orchestrator) and from
`subsystems/` (capability boundaries). Each module here runs as its own
OS process per ADR-0006; isolation rule per ADR-0004:

1. No cross-imports between agent modules. `agents/reflector/` cannot
   import from `agents/monitor/` or any other archetype.
2. No imports from `agent/core.py` or any `subsystems/`. Capabilities
   are reached the same way the orchestrator reaches them — via tool
   calls / MCP, not direct Python imports.
3. Imports from `agents/_shared/` are allowed for utilities only —
   never for state. See `agents/_shared/__init__.py` for safeguards.
4. Each archetype is independently replaceable.

These rules are enforced by `agent/tests/test_agents_isolation.py`.

See `agents/README.md` for the longer rationale and ADR-0004/0006 for
the source-of-truth decisions.
"""

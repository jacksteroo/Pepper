"""Utilities shared across cognitive archetypes.

This carve-out is an intentional, narrow exception to the rule that
agents under `agents/` do not import from each other (ADR-0004 §
"Isolation rule"). It exists so each archetype does not have to redo
logging setup, config loading, and Postgres connection plumbing inside
its own module.

Three discipline rules apply, enforced by
`agent/tests/test_agents_isolation.py`:

1. **Utilities only.** Modules here MUST be pure helpers — logging
   setup, env loading, connection factories, formatting. No state.
2. **Mandatory module docstring.** Every file added under
   `agents/_shared/` must carry a top-of-file docstring naming the
   utility and explaining why it is shared rather than living inside a
   single agent.
3. **No module-level mutable state.** No `_cache: dict = {}`, no
   singletons, no shared session objects at module scope. Anything
   that holds state must move out of `_shared/` into its own
   subsystem with its own MCP contract.

If `_shared/` ever drifts into holding state across agents, the
escalation path is to split that state out as a subsystem — not to
relax these rules. See ADR-0004 §"`agents/_shared/` discipline".
"""

# `agents/` — Cognitive specialists

This directory is the home for Pepper's cognitive archetypes:
long-running, specialised AI processes that consume traces and produce
reflections, compressions, summaries, or research outputs.

It is parallel to — and intentionally distinct from:

- **`agent/`** (singular) — the orchestrator and its supporting code.
- **`subsystems/`** — capability boundaries (Calendar, Communications,
  Knowledge, Health, Finance, eventually People).

## Why a separate directory

ADR-0004 introduces this directory to keep cognitive specialisation
from collapsing into either the orchestrator (`agent/`) or capability
boundaries (`subsystems/`). The OJ-calibration thread documents why a
single-orchestrator pattern fails once reflection, monitoring, and
salience scoring need different cadences and prompt windows.

ADR-0006 ratifies the operating shape: each archetype runs as its own
OS process, triggered from APScheduler in core via a thin Postgres
`LISTEN/NOTIFY` shim. Crash isolation, memory independence, and per-
archetype observability fall out of that decision.

## Isolation rule

Every module under `agents/` follows four rules. `_shared/` exists as
the only narrow exception (see below).

1. **No cross-imports between archetypes.** `agents/reflector/` cannot
   import from `agents/monitor/`. Archetypes communicate via the trace
   store and persisted artefacts (reflections, summaries), not via
   shared code.
2. **No imports from `agent/core.py` or any `subsystems/`.** The
   orchestrator is downstream of cognitive agents; agents must never
   reach back into it. Subsystem capabilities are reached via tool
   calls / MCP, not via direct Python imports.
3. **`agents/_shared/` is allowed for utilities only.** Logging setup,
   config loading, db connection management, pure helpers. Never
   state. See `_shared/__init__.py` for the safeguards.
4. **Each archetype is independently replaceable.** Swapping the
   reflector for a different implementation must not require touching
   any other archetype.

These rules are enforced by `agent/tests/test_agents_isolation.py`.
That test fails CI on any violation, including:

- An archetype importing from a sibling archetype.
- An archetype importing from `agent/core` or `subsystems/`.
- A new file under `_shared/` without a module-level docstring.
- A module-level mutable assignment under `_shared/` (the state
  smell — `_cache: dict = {}`, `state = {}`, etc.).

## `agents/_shared/` carve-out

`_shared/` is a narrow exception. It carries three layered safeguards
(per ADR-0004 §"`agents/_shared/` discipline"):

1. **Mandatory docstring rationale.** Every file under `_shared/`
   must carry a top-of-file docstring naming the utility and
   explaining why it is shared rather than living inside one agent.
   The lint test fails CI without it.
2. **PR-review checklist item.** Every PR that adds or modifies a
   file under `_shared/` answers the question:
   *"Does this `_shared/` addition store state, or does it just
   provide utilities?"* If the answer is "stores state," the change
   is rejected and the state is moved to its own subsystem.
3. **Quarterly audit.** Once per quarter, `_shared/` is reviewed and
   anything that has drifted toward coupling is split out as a
   subsystem with its own MCP contract.

If `_shared/` ever drifts into holding state, the escalation path is
to **split it out as a subsystem** — not to relax these rules.

## Running an archetype

Archetypes are launched through the generic runner entrypoint:

```bash
python -m agents.runner --archetype reflector
```

In production, each archetype runs as its own `docker-compose`
service (see the `agents/` block in `docker-compose.yml`). The runner
configures shared logging via `agents/_shared/logging.py`, installs
SIGTERM/SIGINT handlers, and dispatches to `agents/<name>/main.py:run(
config)`.

The substrate phase ships one archetype: `agents-reflector` (issue
#39). `agents-monitor` and `agents-researcher` are reserved names —
the runner will accept `--archetype monitor`/`--archetype researcher`
once their implementations land, and emits a clear "not yet
implemented" error in the meantime.

## See also

- ADR-0004 — directory rule and isolation.
- ADR-0006 — separate process per archetype.
- `agent/tests/test_agents_isolation.py` — the lint check.
- `agents/_shared/__init__.py` — `_shared/` discipline rules.

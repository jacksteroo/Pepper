# ADR-0004: Introduce `agents/` directory parallel to `subsystems/`

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

Pepper's existing structural rule is that `subsystems/` is the home for *capability boundaries*: People, Calendar, Communications, Knowledge, Health, Finance. Each subsystem is independently replaceable, never imports from another subsystem, and never imports from `agent/core.py`. That rule has held up well in daily code review.

ADR-0001 commits Pepper to a substrate phase whose deliverables include long-running cognitive processes that are not capability subsystems: a reflector that reviews traces nightly, a continuous monitor that compresses memory in the background, and (eventually) on-trigger researchers and similar specialised agents. These cognitive processes have all the properties that motivated the subsystem-isolation rule — they should be independently replaceable, should not entangle with each other, and must not reach into `agent/core.py` — but they are not subsystems. Calling them `subsystems/reflector/` would conflate cognitive specialisation with capability decomposition and erode the meaning of both directories.

Today these cognitive processes have nowhere clean to live. Putting them inside `agent/` blurs them into the orchestrator. Putting them under `subsystems/` overloads that directory's meaning. The OJ-calibration thread surfaced the same tension: a single orchestrator does not scale to inner-life work because reflection, salience scoring, self-modelling, and restraint run on different cadences and need separate prompts and memory windows. Without an explicit home, the substrate phase's deliverables would land scattered.

## Decision

Introduce a new top-level directory `agents/` parallel to `subsystems/`. It is the home for *cognitive functions* — long-running, specialised AI processes that consume traces and produce reflections, compressions, summaries, or research outputs.

`agents/` carries the same isolation rule as `subsystems/`:

1. No cross-imports between agent modules. `agents/reflector/` cannot import from `agents/monitor/`. They communicate via the trace store and persisted artefacts (summaries, identity-doc updates), not via shared code.
2. No imports from `agent/core.py`. The orchestrator is downstream of cognitive agents; agents must never reach back into it.
3. Each agent module is independently replaceable. Swapping the reflector for a different implementation must not require touching any other module.

A lint check enforces the isolation rule. As of this ADR, no such check exists in the repo: the project's lint config is `[tool.ruff]` in `pyproject.toml`, which configures formatting and basic style rules but no import-graph constraints. The likely home for the new check is therefore one of: a `[tool.ruff.lint.flake8-tidy-imports.banned-api]` block in `pyproject.toml` covering both `subsystems/` and `agents/` boundaries, a small custom checker invoked from CI, or both. This ADR creates the obligation to add the check before the first `agents/` module lands; the choice of mechanism is left to the implementing PR. The lint check itself is not in this PR — this PR ratifies the rule, and the next implementation PR brings the check.

First inhabitants once substrate work begins:

- `agents/reflector/` — nightly trace review, summarisation of failure modes and recurring patterns.
- `agents/monitor/` — long-running memory-compression and salience scoring.
- `agents/researcher/` — on-trigger multi-hop investigation; lower priority, may not land in the substrate phase.

`agent/` (singular, no trailing `s`) is unchanged: it remains the orchestrator and its supporting code. `subsystems/` is unchanged: it remains capability boundaries (Calendar, Communications, Knowledge, Health, Finance, eventually People). `agents/` (plural) is new: cognitive specialisation.

## Consequences

**Positive.**

- Substrate-phase deliverables (reflector, monitor) have a structural home that keeps the orchestrator from accreting cognitive functions.
- The isolation rule that has worked for `subsystems/` is reused, not reinvented. Reviewers already understand the shape of the boundary.
- The directory naming (`agent/` for the orchestrator, `agents/` for cognitive specialists, `subsystems/` for capabilities) creates three coexisting concepts that are visibly distinct.
- ADR-0002's "compounding capability" principle gets a concrete structural anchor: self-improvement processes live somewhere, in their own modules, with their own boundaries.

**Negative.**

- A new top-level directory expands the project's surface and adds a concept new contributors must learn. The mitigation is keeping the rule identical to `subsystems/` so the cognitive load is "another boundary you've seen before," not "a new architectural pattern."
- Lint enforcement is now an obligation. The first PR that adds an `agents/` module must also bring the lint check, or the boundary erodes immediately.
- Cross-cutting concerns that would naturally span agents (e.g., shared trace-reading helpers) need a designated home. The likely answer is to expose those helpers from the trace-store module rather than from a shared agents-only utility module, but the right shape will only become clear when the first two agents land.

**Neutral.**

- Existing code under `agent/` and `subsystems/` is untouched. This ADR creates a directory rule, not a migration.
- The decision does not pick a process model (separate process, APScheduler job, daemon thread). That is left to the substrate-phase implementation PRs.

## Alternatives considered

- **Status quo: keep cognitive processes inside `agent/`.** Rejected — that is the path that produces a single orchestrator stuffed with reflection, monitoring, salience scoring, and restraint, all sharing one prompt and one memory window. The OJ-calibration thread documents why that pattern produces incoherent agents. Keeping the directory clean before the work starts is far cheaper than untangling it later.
- **Place cognitive processes under `subsystems/` (e.g., `subsystems/reflector/`).** Rejected — `subsystems/` means *capability boundaries*. Adding cognitive functions there overloads the meaning of the directory and weakens the existing isolation rule for both. Two clear concepts beats one ambiguous one.
- **Use a `runtime/` or `cognition/` directory instead of `agents/`.** Rejected — `agents/` matches the language used internally and in the calibration thread, and matches industry usage (OpenJarvis archetypes, Generative Agents). New names would need new explanations.
- **Defer the directory decision until the first cognitive process is built.** Rejected — the structural decision drives the implementation. Building the reflector first and *then* deciding where it lives produces churn (rename, re-import, re-lint) and risks the first implementation accreting cross-cuts that the isolation rule would have prevented.

## References

- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — §4 "Single orchestrator may not scale to inner life"; cognitive-specialization rule open question
- [Agent Pepper hub](https://www.notion.so/jacksteroo/Agent-Pepper-353fb7367390806a88addf0430118d34)
- [ADR-0001](0001-resequence-around-oj-calibration.md) — substrate phase deliverables that will populate `agents/`
- [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) — compounding-capability principle that `agents/` modules instantiate
- [docs/GUARDRAILS.md](../GUARDRAILS.md) — existing subsystem boundary rules that the `agents/` rule mirrors
- [ADR-0000 template](0000-template.md)
- Source PR: [#14](https://github.com/jacksteroo/Pepper/issues/14)

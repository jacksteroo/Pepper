# Architectural Decision Records (ADRs)

This directory is the canonical home for Pepper's architectural decisions.

An ADR captures a single decision, the forces that drove it, the alternatives considered, and the consequences. ADRs are immutable once accepted: if a future decision overturns them, write a new ADR with status `Superseded by ADR-XXXX` and update the old one's status accordingly.

ADRs are not design docs, planning docs, or how-to guides. Those belong in `docs/`. ADRs only exist to record decisions whose rationale would otherwise rot in a Slack thread, a Notion page, or somebody's head.

## When to write an ADR

Write one whenever a decision meets all of:

- It is **architectural** — it shapes the system's structure, boundaries, dependencies, or operating principles.
- It is **non-obvious** — a future contributor reading the code alone could not reconstruct the rationale.
- It **survives a Notion thread** — the discussion has converged and is about to land in code.

`docs/GUARDRAILS.md` formalizes the rule: any decision that survives a Notion thread becomes an ADR before the implementation PR opens.

## Lifecycle

Each ADR has a `Status` field that follows this state machine:

- **Proposed** — drafted, in review. The decision is not yet binding.
- **Accepted** — merged to `main`. The decision is binding for new work.
- **Superseded by ADR-XXXX** — a newer ADR replaced it. The old ADR stays in the directory for historical context; only the status changes.
- **Rejected** — drafted but explicitly not adopted. Kept so the same idea is not re-litigated from scratch later.

ADRs do not get deleted. Their numbers do not get reused.

## How to add a new ADR

1. Pick the next free number `NNNN`. Numbers are zero-padded to four digits and assigned in the order ADRs are proposed.
2. Copy `0000-template.md` to `NNNN-<slug>.md` where `<slug>` is a short kebab-case description (e.g. `0005-introduce-event-bus.md`). Do not edit `0000-template.md` itself.
3. Fill in every section. If a section genuinely does not apply, write `N/A — <reason>` rather than deleting it.
4. Open a PR with `Status: Proposed`. The PR is the discussion venue.
5. On merge, flip the status to `Accepted` (or `Rejected` and merge anyway, so future contributors see the rejected option).
6. If the ADR makes a roadmap-level commitment, add a one-line reference to it from `docs/ROADMAP.md`.

## Index

- [0000-template.md](0000-template.md) — template (do not edit; copy it)
- [0001-resequence-around-oj-calibration.md](0001-resequence-around-oj-calibration.md) — re-sequence around OJ-calibration third option
- [0002-fifth-anchoring-principle-compounding-capability.md](0002-fifth-anchoring-principle-compounding-capability.md) — add fifth anchoring principle (compounding capability)
- [0003-layer-2-is-the-active-surface.md](0003-layer-2-is-the-active-surface.md) — Layer 2 (Intelligence) is the active surface
- [0004-introduce-agents-directory.md](0004-introduce-agents-directory.md) — introduce `agents/` directory parallel to `subsystems/`
- [0005-trace-schema.md](0005-trace-schema.md) — canonical `Trace` record (Epic 01)
- [0006-reflector-process-model.md](0006-reflector-process-model.md) — agents run as separate processes per archetype, scheduler-triggered (Epic 04)
- [0007-optimizer-framework-gepa.md](0007-optimizer-framework-gepa.md) — GEPA selected as the prompt-optimization framework (Epic 05)
- [0011-layer-3-thin-clients-tailscale.md](0011-layer-3-thin-clients-tailscale.md) — Layer 3 master ADR: thin clients on a Tailscale-accessible server (Epic 08)

The four foundational ADRs above (0001–0004) are tracked in Epic 00: Foundations & ADRs (issue #9). ADR-0005 is the first decision record produced under Epic 01: Trace Substrate (issue #17). ADR-0006 is the first decision record produced under Epic 04: Reflection Runtime (issue #36). ADR-0007 is the framework decision for Epic 05: DSPy/GEPA Optimization Loop (issue #43). ADR-0011 is the master ADR for Epic 08: Layer 3 — Mobile + Desktop Thin Clients on Tailscale-Accessible Server (issue #70); sibling ADRs 0012–0016 land under that epic.

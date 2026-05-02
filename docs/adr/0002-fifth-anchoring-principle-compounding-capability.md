# ADR-0002: Add fifth anchoring principle — compounding capability

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

Pepper has four anchoring principles: privacy-first, sovereignty, additive memory, pluggable subsystems. Together they describe what the system *protects* (the operator's data) and what it *preserves* (its own context over time). They do not describe how the system *improves*.

Today the only mechanism by which Pepper gets better is a developer changing code, tests passing, and a release shipping. Between commits, behaviour is static. The semantic router learns nothing from its own decisions; prompts do not adapt to recurring failure modes; the agent cannot reason about its own past behaviour because there is no behavioural trace store to reason over.

This is a strategic gap. ADR-0001 commits Pepper to a substrate phase whose deliverables — trace store, hybrid retrieval, reflection runtime, learned routing — only make sense if "the system improves itself in response to its own behaviour" is a first-class principle. Without that principle stated, those deliverables look like opportunistic features rather than the substrate they are. The OJ-calibration thread surfaces this directly: *compounding context* is in the existing principles, *compounding capability* is not, and the gap shows up everywhere it matters.

The principle has to be added now, before substrate work lands, so that future PRs in that workstream are reviewable against an explicit anchor instead of being argued from first principles each time.

## Decision

Add a fifth anchoring principle to the project's stated invariants:

> **Compounding capability.** The system improves itself in response to its own behaviour, within human-reviewable bounds.

Operationally, this principle means:

- Pepper records structured behavioural traces of her own actions and outcomes.
- Periodic reflection processes consume those traces and surface recurring failure modes, useful patterns, and proposed adjustments.
- Optimization processes (DSPy-style or equivalent) tune routing decisions, prompts, and skill selection from local traces.
- All such self-improvement is bounded: changes are versioned, human-reviewable, and revertable. The agent does not silently rewrite production prompts.

This principle is not in conflict with the existing four anchoring principles (privacy-first, sovereignty, additive memory, pluggable subsystems) called out in `CLAUDE.md`. Privacy and sovereignty are preserved because optimization runs locally on local traces; only optimized prompts (artefacts, not raw data) ever leave the machine, and only when the operator chooses to ship them. Additivity is preserved because traces are append-only and optimized prompts are versioned. Pluggable subsystems are preserved because trace collection and reflection live in their own modules, not inside `agent/core.py`.

`CLAUDE.md`'s Pepper Core Principles list is updated in this PR to add the new principle alongside the existing entries. `docs/PRINCIPLES.md` is the broader, operational-principles document and is intentionally not modified here — its scope (data sovereignty, durability, versioning, etc.) is wider than the CLAUDE.md anchor list, and folding "compounding capability" into it without restructuring would dilute both. A future ADR can revisit the relationship between the two if the breadth of `PRINCIPLES.md` becomes a problem.

ADR-0001's substrate phase now has an anchor it can be reviewed against. The bound (*"within human-reviewable bounds"*) is enforced by `docs/GUARDRAILS.md`, which takes precedence over any ADR including this one — any self-improvement feature that breaches the bound is rejected at code review per the existing GUARDRAILS precedence rule.

## Consequences

**Positive.**

- Substrate work (trace store, reflection runtime, learned routing) is now anchored to a stated principle, not argued ad hoc.
- The bound *"within human-reviewable bounds"* gives reviewers an explicit rejection criterion for self-improvement features that would silently mutate behaviour.
- Future ADRs that propose feedback loops, learning components, or DSPy-style optimization can cite this principle directly instead of re-justifying the entire posture.

**Negative.**

- The "five anchoring principles" framing must now be reflected in onboarding docs, PRINCIPLES.md, CLAUDE.md, and any future architecture write-ups. Doc churn is a one-time cost.
- The principle creates an obligation: any self-improvement feature must demonstrate human-reviewability, which is a real engineering requirement (versioning, diff surfaces, rollback). Cheap "just have the agent rewrite its prompts" implementations are now out of bounds.

**Neutral.**

- The four existing principles are unchanged in scope and meaning.
- The principle does not commit to a specific implementation (DSPy, GEPA, lighter-weight prompt evolution, learned routers). It commits only to the *shape* of the invariant.

## Alternatives considered

- **Status quo: keep four anchoring principles, treat self-improvement as a feature category.** Rejected — every substrate-phase deliverable in ADR-0001 ends up arguing the same posture from first principles. The cost of stating the principle once, in the canonical list, is far lower than the cost of re-litigating it on every PR. Worse: without an explicit principle, a single reviewer's intuition becomes the bar.
- **State the principle in `docs/ROADMAP.md` rather than as an anchoring principle.** Rejected — roadmap items rotate; anchoring principles do not. Compounding capability is an invariant about *how the system works over time*, not a phase deliverable.
- **State the principle but defer the "human-reviewable bounds" clause.** Rejected — without the bound, the principle authorizes silent self-mutation, which conflicts with the existing privacy/sovereignty posture and with safe operation. The bound is what makes the principle compatible with the others.
- **Adopt OpenJarvis-style autonomous learning loops without an anchoring principle.** Rejected — that posture (sensible defaults, opt-out) is a framework's posture. Pepper is a single-operator product and has chosen "you cannot opt out without breaking a test" as its enforcement model. Importing the loop without importing the posture would create a hidden divergence from the existing principles.

## References

- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — §3 "No learning loop — Pepper is static between commits"
- [Agent Pepper hub](https://www.notion.so/jacksteroo/Agent-Pepper-353fb7367390806a88addf0430118d34)
- [docs/PRINCIPLES.md](../PRINCIPLES.md) — canonical list of anchoring principles
- [ADR-0001](0001-resequence-around-oj-calibration.md) — substrate phase that this principle anchors
- [ADR-0000 template](0000-template.md)
- Source PR: [#12](https://github.com/jacksteroo/Pepper/issues/12)

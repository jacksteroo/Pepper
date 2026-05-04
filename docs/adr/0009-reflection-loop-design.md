# ADR-0009: Reflection loop design and cadence

- **Status:** Accepted
- **Date:** 2026-05-03

## Context

The reflector archetype (`agents/reflector/`) was established by ADR-0006 as a separate OS process that runs on a defined cadence and writes interior-voice notes to `reflection_notes`. ADR-0006 settled the *process model* (separate process, Postgres LISTEN/NOTIFY trigger, own docker service). It did not settle the *loop design*: when the reflector runs, what it reads, what it writes, what it is biased to notice, and what it is explicitly forbidden from doing.

Epic 06 ("Inner Life Moves", #49) requires the reflector to operate on a well-specified loop before identity-doc governance (ADR-0008, #52), strategy invocation, and continuity-of-self evaluation can be built on top of it. Without a formal loop design:

- The cadence choice (daily vs weekly vs event-triggered) is implicit, and any contributor can change it without understanding the tradeoff.
- The privileged-content list is absent, meaning the reflector prompt has no bias toward the signals that make compounding capability (ADR-0002) meaningful.
- The output contract is open-ended — nothing prevents a future PR from adding outbound API calls or direct identity-doc writes to the reflector, which would violate both ADR-0008 and the privacy invariants in `docs/GUARDRAILS.md`.
- The security and privacy classification of reflection notes is undocumented, creating ambiguity about whether they are RAW_PERSONAL or a less sensitive artifact.

Issue #50 captured this gap.

## Decision

Adopt the loop design specified in `docs/reflection-loop-design.md` (shipped alongside this ADR). The key commitments are:

**Cadence:** daily-fixed (02:00 local, configurable) plus event-triggered for designated trace event types (`hard_conversation`, `identity_challenge`, `commitment_made`). Event-triggered runs are rate-limited to one per two hours and do not replace the next scheduled daily run. Daily is the correct default cadence for a personal assistant: sub-daily has poor signal-to-cost ratio (individual sessions are thin); weekly is too coarse to detect mood-state shifts or multi-day commitment drift.

**Inputs (in priority order):** (1) traces from the last 24 hours, summarised before LLM ingestion; (2) prior reflection note, as continuity anchor; (3) `data/life_context.md`; (4) `data/pepper_identity.md` when it exists.

**Outputs:** a single reflection note (200–600 words, interior voice, RAW_PERSONAL); optional pattern alerts to `pattern_alerts` (deterministic pre-LLM); optional candidate identity-doc diff to `pending_identity_diffs` (propose-then-approve per ADR-0008, rate-limited to one per daily run).

**Privileged content:** the reflector is explicitly biased toward — recurring people, unresolved commitments, mood-state shifts, recurring tool failures, topics Jack mentions repeatedly, and tensions with the identity document. These are named categories with examples in `docs/reflection-loop-design.md §Privileged content`.

**Scope exclusions (hard):** the reflector does not produce briefs for Jack, does not take outbound actions, does not modify any table except `reflection_notes`, `pattern_alerts`, and `pending_identity_diffs`, does not call external APIs, and does not approve its own identity-doc diffs.

**Privacy:** reflection notes are RAW_PERSONAL. LLM routing defaults to local (Ollama). Frontier escalation requires explicit opt-in (`PEPPER_REFLECTION_LLM=frontier`).

This decision applies to the `agents/reflector/` archetype only. It does not govern other agent archetypes (monitor, researcher) that may follow a different cadence or output contract.

## Consequences

**Positive.**

- The cadence is justified and documented, not implicit. Any change to daily-fixed requires understanding and updating the tradeoff argument here.
- The privileged-content list gives the reflector prompt a concrete bias, moving it from "summarise the day" to "notice the things that accumulate into self-knowledge."
- The hard scope exclusions make it impossible to accidentally extend the reflector into outbound or identity-mutating territory without an ADR update and explicit review.
- The identity-doc diff output path is defined here and governed by ADR-0008. The two ADRs compose without conflict.
- Reflection notes are unambiguously RAW_PERSONAL, so their access controls are governed by the same policy as traces — no separate decision is needed.

**Negative.**

- The event-triggered path adds complexity to the scheduler and requires a `reflection_type` field in `reflection_notes` to distinguish daily from event runs. Small implementation cost; needed for downstream analytics.
- Rate-limiting event-triggered runs to one per two hours means a burst of flagged events in a single afternoon will not all generate reflections. The daily run will capture the full window; the intra-day runs are supplemental.
- The 200-word floor and 600-word ceiling are enforced by the prompt, not by the schema. A sufficiently instruction-following model will respect them; a weaker local model may not. The eval rubric's length-appropriateness dimension (dimension 5 in `docs/reflection-eval-rubric.md`) catches persistent violations.

**Neutral.**

- The four inputs (traces, prior reflection, life_context, identity doc) are additive. Adding a fifth input in the future requires a design-doc update but not a new ADR unless the input changes the privacy boundary.
- The local-LLM default for the reflector is consistent with the existing local default for Pepper Core. No new policy is established; an existing policy is restated in the reflector context.

## Alternatives considered

- **Weekly cadence as default.** Rejected. See `docs/reflection-loop-design.md §Cadence` for the full argument. Summary: weekly aggregates too broadly to detect mood-state shifts, multi-day commitment drift, or recurring-person patterns at a resolution that is useful. The pattern detector in #41 catches some of this deterministically, but the LLM-generated reflection needs a narrow enough window to be grounded.
- **Sub-daily (per-session) cadence.** Rejected. Individual sessions are thin; the reflector's signal is cumulative. Per-session reflection competes with the live interaction for LLM capacity and produces noise-level output most of the time.
- **Event-triggered only (no fixed daily).** Rejected. A day with no flagged events would produce no reflection, breaking the continuity-anchor property — the prior-reflection input would become stale at unpredictable intervals. The fixed daily run is the backbone; event-triggered runs are supplemental.
- **No output constraints (open-ended writes).** Rejected. An unconstrained output contract creates a surface through which future contributors can accidentally connect the reflector to outbound actions, external APIs, or identity-doc direct writes — all of which violate existing architectural commitments. The hard exclusion list exists precisely to make those violations explicit rather than accidental.
- **Reflection notes as non-personal / shareable data.** Rejected. Reflections are high-density compressions of Jack's private life — they contain more interpretive signal per word than raw traces, not less. Classifying them as non-personal would weaken the privacy invariants in `docs/GUARDRAILS.md` at the layer most visible to future contributors.

## References

- Issue: [#50 — Reflection loop — formal design (cadence, content)](https://github.com/jacksteroo/Pepper/issues/50).
- Parent epic: [#49 — Inner Life Moves](https://github.com/jacksteroo/Pepper/issues/49).
- Design document: [`docs/reflection-loop-design.md`](../reflection-loop-design.md).
- [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) — compounding capability (the principle this loop design serves).
- [ADR-0005](0005-trace-schema.md) — trace store and Postgres roles; read-only contract for agent processes.
- [ADR-0006](0006-reflector-process-model.md) — reflector process model; this ADR specifies the loop the process runs.
- [ADR-0008](0008-pepper-identity-governance.md) — propose-then-approve identity governance; the candidate-diff output path from this ADR composes with ADR-0008's queue.
- [`docs/reflection-eval-rubric.md`](../reflection-eval-rubric.md) — rubric for scoring individual reflection notes; the loop design must produce notes that are scoreable against that rubric.
- [`docs/GUARDRAILS.md`](../GUARDRAILS.md) — privacy boundaries that constrain the reflector's LLM routing and data classification decisions.

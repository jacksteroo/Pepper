# ADR-0009: Reflection loop — daily-fixed cadence, local-only, additive event-triggered

- **Status:** Accepted
- **Date:** 2026-05-03

## Context

ADR-0006 settled the *process model* of the reflector — separate OS process per archetype, scheduler-triggered via Postgres NOTIFY. The runtime exists (#39), the rollups exist (#40), the pattern detector exists (#41), the eval rubric exists (#42). What remains to settle is the *design* of what the reflection actually tries to do — its cadence, its inputs, its outputs, what it privileges over time, and what it explicitly does not do.

This decision gates Epic 06 (#49). The downstream issues each depend on the reflector emitting a specific output shape:

- #52 (PEPPER_IDENTITY seed + wiring) consumes proposed identity diffs from the reflector.
- #54 (Strategy Hub tools + UI) consumes proposed strategy updates from the reflector.
- #56 (Wait-action feedback) reads wait-traces and pairs them with reflection outputs.
- #57 (Continuity-of-self eval) scores the artifacts the loop produces.

If the cadence and exclusions are not pinned down, each downstream issue would invent its own answer. That is exactly the failure mode ADR-0006 already named ("the runtime form should match the structural rule") at the design layer.

The forces that shape the decision:

- **The runtime fires daily.** APScheduler in `agent/scheduler.py:fire_reflector_trigger` already runs at 23:55 local (ADR-0006 + #39). The design either accepts that cadence or has to change the scheduler.
- **The downstream consumers want daily granularity.** #52/#54/#56 all want a pulse more recent than the existing weekly/monthly rollups. #57's continuity rubric scores day-over-day shifts.
- **The model-routing decision is load-bearing.** Reflections aggregate raw trace contents into a single document. If the reflector ever calls a frontier model, RAW_PERSONAL data leaves the machine on every reflection — the worst possible cadence for a privacy violation.
- **Bounded scope reduces drift hazard.** A reflector that "may modify any other table" is a reflector that mutates Pepper's self-model and her external commitments via a single uninspectable code path. The exclusions in this ADR are deliberate guardrails.
- **Event-triggered runs are useful but should not be the default shape.** Hard-conversation flags and high-severity pattern alerts produce moments where reflection-now is more valuable than reflection-at-23:55. Folding these in as additive (not replacement) keeps the daily as the canonical surface while letting the reflector respond to the day.

## Decision

The reflection loop runs on a **daily-fixed cadence at 23:55 local time** as the canonical surface, with **additive event-triggered runs** for explicitly flagged events. The reflector is **local-only by default with no frontier fallback** — if the local LLM is unavailable, the day's reflection is skipped, not escalated. The reflector's outputs are **scoped** to a fixed set of tables; it does not modify identity, life context, traces, or memory directly.

The full elaborated specification — inputs, outputs, privileged content, hard exclusions, privacy posture, and cross-references to existing components — lives in `docs/reflection-loop-design.md`.

Concretely the decision:

- **Cadence.** Default daily (existing 23:55-local trigger). Event-triggered runs are additive and gated behind an env flag at first land. Per-turn, hourly, and weekly-only cadences are explicitly rejected.
- **Inputs.** Trace window (last 24h, capped at `MAX_TRACES_PER_REFLECTION = 60`), previous reflection, life context, identity (when #52 lands), and prior pattern alerts (last 7 days).
- **Outputs.** One reflection note (200–600 words, first-person, no audience-shaped framing); optional pattern alerts (#41); optional candidate identity diff via the propose-then-approve queue from ADR-0008; optional candidate strategy update via #54's queue.
- **Privileged signals.** Recurring people, unresolved commitments, mood-state shifts, Pepper's own restraint outcomes (wait-traces from #55), self-noticed patterns. Privileging is content-side (prompt + post-processing classification), not weights-side.
- **Exclusions.** No briefs for Jack. No external-side-effect tool calls. No direct writes to `life_context.md`, `pepper_identity.md`, the trace store, or the memory store. No frontier-model routing. No re-summarising yesterday. No self-scoring (lives in #42).
- **Privacy posture.** Reflections are RAW_PERSONAL. The LLM call is gated by the same `LOCAL_LLM_HOSTS` allowlist `agents/reflector/main.py` already enforces. Embeddings are local-only via Ollama. Storage is local-only. The "trigger that would justify a frontier escalation" is: never, by default.

This decision applies to the daily reflector and the additive event-triggered hook only. It does not apply to the weekly or monthly rollup runs (#40) — those have their own prompts and inputs and are not in scope for this ADR.

## Consequences

**Positive.**

- Downstream Epic 06 issues (#52, #54, #56, #57) have a defined output contract to build against. The reflector emits a specific shape; consumers do not have to guess.
- Privacy posture is locked in at the design layer, not just at the runtime layer. Future contributors cannot accidentally route the reflector through a frontier provider without changing this ADR.
- The exclusion list is concrete. If the reflector tries to grow new mutation paths, this ADR is the blocker — review pushes back against the change.
- The cadence aligns with the rollup hierarchy already in place (#40 rolls up dailies into weeklies into monthlies). No re-derivation cost.
- Event-triggered runs give Pepper a way to respond to flagged events without making the daily cadence variable.

**Negative.**

- A local-LLM-only posture means a reflection is lost on every day Ollama is down. The next day's reflection has to absorb two days of context. This is the deliberate trade against frontier exfiltration of RAW_PERSONAL data.
- Event-triggered runs add cadence variability. The first land must include a flag so we can observe behaviour before defaulting it on.
- The 60-trace cap is a salience-vs-completeness trade. On busy days, low-frequency-but-important signals can drop below the cap. Mitigated by the privileged-content list, which biases what survives sorting.
- The reflector cannot self-correct mid-month if the design needs adjustment — that requires a new ADR. This is a feature, not a bug, by ADR-0002 (compounding capability must be inspectable and reversible).

**Neutral.**

- Adds two new output channels (proposed identity diffs, proposed strategy updates) that did not exist before. Both route through approval queues, which means no new trust surface — just new productive load on existing surfaces.
- Reflector code in `agents/reflector/main.py` does not need significant changes for this ADR alone; the design simply names the existing behaviour and the planned additions.

Follow-up work this decision creates:

- `docs/reflection-loop-design.md` lands alongside this ADR (already authored as part of #50).
- The event-triggered hook is specified here but not implemented in #50; lands incrementally as a follow-up.
- The proposed-identity-diff output channel is realised in #52.
- The proposed-strategy-update output channel is realised in #54.
- The wait-trace privileged signal is realised in #55 (writer) + #56 (reader).

## Alternatives considered

- **Per-turn or hourly cadence.** Rejected. Per-turn collapses into working memory (the assembler in #32 already covers it). Hourly explodes prompt count without buying continuity.
- **Weekly-only cadence (no daily).** Rejected. Loses the granularity that #52, #54, and #56 need to ground their downstream moves on. Also forces re-derivation of weekly/monthly rollups which currently presume a daily atomic unit.
- **Frontier-model fallback when local LLM is down.** Rejected. The reflection aggregates 24h of RAW_PERSONAL trace contents — the worst possible payload to send to a frontier provider on a recurring schedule. Skipping the day is the correct degradation.
- **Reflector writes directly to `life_context.md` and `pepper_identity.md`.** Rejected. Violates ADR-0002 (compounding capability must be inspectable and reversible) and ADR-0008 (identity changes must be approval-gated). Routing through queues is the trade.
- **Status quo — leave the design implicit in the runtime.** Rejected. Each downstream Epic 06 issue would re-derive the contract, and the next contributor wanting to add an output channel would have nothing to push back against.
- **Single ADR covering both reflector design and the future researcher/monitor agents.** Rejected. The other agents do not exist yet and have different cadence forces. ADR-0006 already settled their shared process model; design ADRs should land per-archetype as the archetypes land.

## References

- Parent epic: [#49 — Inner Life Moves](https://github.com/jacksteroo/Pepper/issues/49).
- Issue: [#50 — Reflection loop formal design](https://github.com/jacksteroo/Pepper/issues/50).
- Companion design doc: [`docs/reflection-loop-design.md`](../reflection-loop-design.md).
- Prior ADR: [ADR-0002 — Compounding capability](0002-fifth-anchoring-principle-compounding-capability.md).
- Prior ADR: [ADR-0005 — Trace schema](0005-trace-schema.md).
- Prior ADR: [ADR-0006 — Reflector process model](0006-reflector-process-model.md).
- Prior ADR: [ADR-0008 — PEPPER_IDENTITY governance](0008-pepper-identity-governance.md).
- Generative Agents — Park et al, [arXiv:2304.03442](https://arxiv.org/abs/2304.03442).
- Notion: [Bringing Pepper Alive — Philosophical Foundation §5.1](https://www.notion.so/jacksteroo/Bringing-Pepper-Alive-Philosophical-Foundation-353fb7367390818aba04fd6052f5e974).

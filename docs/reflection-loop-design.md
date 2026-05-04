# Reflection Loop — Formal Design

This document specifies *what* the reflection loop tries to do — its cadence, inputs, outputs, what it privileges, and what it deliberately does not do. The runtime that executes the loop already exists (`agents/reflector/`, ADR-0006); this document is the design layer that the runtime serves.

The decision shape is captured in [ADR-0009](adr/0009-reflection-loop-design.md). This file is the elaborated specification.

## 1. Purpose

A reflection is **a note Pepper writes to herself, not a brief for Jack.** It exists so that:

- Pepper has a continuous interior across days, not a fresh personality every morning.
- The system has a substrate output that downstream inner-life moves can stand on (identity diffs in [#52](https://github.com/jacksteroo/Pepper/issues/52), strategy proposals in [#54](https://github.com/jacksteroo/Pepper/issues/54), wait-correctness signals in [#56](https://github.com/jacksteroo/Pepper/issues/56)).
- Patterns that span more than a single trace become noticeable — which is the whole point of a memory layer separate from the trace store.

Reflections are **not** product output. They never get rendered to Jack as a brief, never trigger user-facing notifications, and never make external calls.

## 2. Cadence

### 2.1 Default — daily-fixed

The default cadence is **once per day, at 23:55 local time**, fired by Pepper Core's APScheduler via the `reflector_trigger` Postgres NOTIFY channel (ADR-0006). The reflector reads the previous local day's traces, the previous reflection, the life context, and the identity (when it lands in [#52](https://github.com/jacksteroo/Pepper/issues/52)), and writes one reflection note.

Justification:

- **Bounded prompt size.** A single local day is the smallest cadence that produces a useful continuity surface. Anything shorter (per-conversation, per-event) collapses into per-turn working memory, which the assembler in [#32](https://github.com/jacksteroo/Pepper/issues/32) already covers.
- **Aligns with the rollup hierarchy.** Weekly and monthly rollups already exist ([#40](https://github.com/jacksteroo/Pepper/issues/40)) and presume a daily as their atomic unit. A non-daily atomic cadence would force re-derivation of those rollups.
- **Low operational cost.** One LLM call per day, local-only by `agents/reflector/main.py`'s privacy posture (§5). The dollar and latency budget is irrelevant at this cadence.
- **Mirrors the philosophical-foundation framing.** *Bringing Pepper Alive* §5.1 frames reflection as the daily examined-life move, not the hot-path one. The cadence should match the framing.

The 23:55-local choice is the existing scheduler decision (ADR-0006) and stays as-is. It runs late enough that the day is mostly done, early enough that it does not race against midnight log rotation or the rollup window.

### 2.2 Event-triggered (additive, not default)

In addition to the daily fixed run, certain event types may fire an extra reflection within the same UTC day. The set of event types is intentionally short and operator-flagged:

- **Hard-conversation flag.** Jack manually flags a turn (or a small contiguous window of turns) as `reflect_now`. The flag is a metadata bit on the trace; the reflector reads it and runs an extra pass scoped to that window.
- **Critical pattern detected.** The pattern detector ([#41](https://github.com/jacksteroo/Pepper/issues/41)) emits a `pattern_severity=high` alert. The reflector runs over the window the pattern spans, not the whole day.

Event-triggered runs:

- **Do not replace the daily.** They are additive. The daily still fires at 23:55 and is the canonical full-day reflection.
- **Are scoped to a sub-window.** They do not consume the full 24h trace window — only the flagged window, plus 30 minutes of context on either side.
- **Bump the same `reflections` table** with a distinct `cadence` value (`event` vs `daily`) so downstream consumers can filter.
- **Are gated behind an env flag** at first land so the cadence shape can be observed before being defaulted on.

The event-triggered hook is specified here so the runtime can land it incrementally; it is not gating any other Epic 06 issue.

### 2.3 Cadences explicitly rejected

- **Per-turn reflection.** That collapses into working memory. The assembler already does it.
- **Hourly.** Insufficient signal per window; explodes the prompt count without buying continuity.
- **Weekly-only (no daily).** Loses the granularity that #52, #54, and #56 need to ground their downstream moves on something more recent than a 7-day rollup.

## 3. Inputs

The reflector composes its prompt from a fixed input shape. Each input is RAW_PERSONAL and the entire prompt is sent only to the local LLM (§5).

| Input | Source | Purpose |
|-------|--------|---------|
| Trace window | `agent.traces.repository`, last 24h (or sub-window for event-triggered) | The day Pepper is reflecting on |
| Previous reflection | `agents.reflector.store.previous_reflection` | Continuity across days |
| Life context | `data/life_context.md` via `agent/life_context.py` | Stable ground truth about Jack |
| Identity (when it lands) | `data/pepper_identity.md` (#52) | Pepper's voice |
| Prior `pattern_alerts` (last 7 days) | `agents/reflector/alerts.py` | Don't re-notice noticed things |

**Bounded by `MAX_TRACES_PER_REFLECTION = 60`** in `agents/reflector/main.py`. On a busy day the reflector takes the most recent 60 traces newest-first, then sorts oldest-first for the prompt. This bound is intentional: the reflection should privilege salience over completeness.

## 4. Outputs

A single reflection produces:

- **One reflection note**, target length 200–600 words, persisted to the `reflections` table (cadence=`daily` or `event`). First-person, no audience-shaped framing — enforced by the prompt rules in `agents/reflector/prompt.py` and post-hoc by `voice_violations()`.
- **Optional pattern alerts** — emitted via `agents/reflector/pattern_detector.py` when a recurring signal crosses a threshold. Pre-existing surface ([#41](https://github.com/jacksteroo/Pepper/issues/41)).
- **Optional candidate identity diff** — when the reflection surfaces something the reflector judges worth committing to the identity surface, it proposes a diff via the propose-then-approve queue established in [ADR-0008](adr/0008-pepper-identity-governance.md). Lands operationally in [#52](https://github.com/jacksteroo/Pepper/issues/52).
- **Optional candidate strategy update** — analogous to identity diffs but for the strategy hub. Lands in [#54](https://github.com/jacksteroo/Pepper/issues/54).

The reflection note itself is the primary output. The diffs and alerts are secondary; if the reflection produces nothing diff-worthy that day, it produces nothing — silence is a valid output shape.

## 5. Privileged content over time

The reflector is biased to notice — and over multiple days, to weight — the following classes of signal. This list is concrete (each entry maps to a detector), not abstract.

| Signal | Detector / heuristic | Why it's privileged |
|--------|----------------------|---------------------|
| Recurring people | Entity mention frequency across 7-day window | Relationships are the substrate the People subsystem will eventually consume |
| Unresolved commitments | Trace turns containing commitment-like language without a closing trace within 72h | Lost commitments are the failure mode Jack notices most |
| Mood-state shifts | Sentiment delta vs trailing 7-day baseline | Continuity-of-self depends on Pepper noticing when Jack's affect changes |
| Pepper's own restraint outcomes | Wait-traces (#55) where the wait window has resolved | Feeds the wait-correctness loop in [#56](https://github.com/jacksteroo/Pepper/issues/56) |
| Self-noticed patterns | When the reflection text contains a phrase the reflector tags as self-observation, it goes to the `## Questions Pepper is asking about herself` section per [ADR-0008](adr/0008-pepper-identity-governance.md) | Gives the reflector a free-write channel that doesn't pile up in the approval queue |

**Privileging is content-side, not weights-side.** No retraining, no fine-tuning. Privileging means: the prompt explicitly tells the reflector to look for these things, and the post-processing classifies the output along these axes.

## 6. What the reflector deliberately does NOT do

Hard exclusions. These are the failure modes the design rejects.

- **Does not produce briefs for Jack.** No "things to know", no "follow-ups", no "TLDR". The voice-violation check enforces this at runtime.
- **Does not take actions.** Cannot call any tool that produces external side effects — no email, no message, no calendar event, no Slack post.
- **Does not modify any other table.** The reflector writes to `reflections`, `pattern_alerts`, `pending_identity_diffs` ([#52](https://github.com/jacksteroo/Pepper/issues/52)), and `pending_strategy_diffs` ([#54](https://github.com/jacksteroo/Pepper/issues/54)). It does not write directly to `life_context.md`, `pepper_identity.md` (only via the queue), the trace store, or the memory store.
- **Does not run on frontier models by default.** §5 below.
- **Does not re-litigate yesterday.** It may reference the previous reflection lightly; it does not summarise or relabel it.
- **Does not score itself.** Self-scoring lives in `agents/reflector/eval.py` ([#42](https://github.com/jacksteroo/Pepper/issues/42)) and runs as a separate pass.

## 7. Privacy & model-routing posture

- Reflections are **RAW_PERSONAL**. They aggregate raw trace contents (which may include private messages, calendar entries, health notes) into a single document.
- The reflector LLM call is **local-only by default**. The same `LOCAL_LLM_HOSTS` allowlist used elsewhere in `agents/reflector/main.py` gates the URL. There is no fallback path to a frontier provider.
- The trigger that *would* justify a frontier escalation is: **never, by default**. If the local LLM is unavailable, the reflection is skipped for the day. The reflector logs the skip; the next day's reflection sees the skipped day's traces alongside its own. This is preferable to leaking 24h of raw trace contents to a frontier provider for a low-stakes-per-failure output.
- Embeddings are local-only (`qwen3-embedding:0.6b` via Ollama) — same posture as the rest of the memory pipeline.
- Reflections are stored locally only. No cloud sync.

## 8. Cross-references and responsibilities

| Concern | Lives in | Notes |
|---------|----------|-------|
| Process model (separate process per archetype) | [ADR-0006](adr/0006-reflector-process-model.md) | Settled |
| Trigger mechanism (Postgres NOTIFY at 23:55 local) | `agent/scheduler.py:fire_reflector_trigger` | Settled |
| Trace schema | [ADR-0005](adr/0005-trace-schema.md) | Settled |
| Reflection prompt + voice rules | `agents/reflector/prompt.py` | Settled (#39) |
| Daily run loop | `agents/reflector/main.py` | Settled (#39) |
| Weekly + monthly rollups | `agents/reflector/rollup.py` | Settled (#40) |
| Pattern detector + alerts | `agents/reflector/pattern_detector.py`, `alerts.py` | Settled (#41) |
| Eval rubric | `docs/reflection-eval-rubric.md`, `agents/reflector/eval.py` | Settled (#42) |
| Identity-diff output channel | [ADR-0008](adr/0008-pepper-identity-governance.md), [#52](https://github.com/jacksteroo/Pepper/issues/52) | Lands in #52 |
| Strategy-diff output channel | [#53](https://github.com/jacksteroo/Pepper/issues/53), [#54](https://github.com/jacksteroo/Pepper/issues/54) | Lands in #54 |
| Wait-feedback consumer | [#56](https://github.com/jacksteroo/Pepper/issues/56) | Reads `wait` traces produced by #55 |
| Continuity-of-self eval | [#57](https://github.com/jacksteroo/Pepper/issues/57) | Scores the artifacts this loop produces |

## 9. Open questions

None gating Epic 06. The cadence (§2), inputs (§3), outputs (§4), privileges (§5), exclusions (§6), and posture (§7) are settled in [ADR-0009](adr/0009-reflection-loop-design.md).

If the process-model decision (ADR-0006) reopens, this document does not need to change — process model is orthogonal to design.

## 10. References

- [ADR-0006 — Reflector process model](adr/0006-reflector-process-model.md)
- [ADR-0008 — PEPPER_IDENTITY governance](adr/0008-pepper-identity-governance.md)
- [ADR-0009 — Reflection loop design](adr/0009-reflection-loop-design.md)
- [Reflection eval rubric](reflection-eval-rubric.md) (#42)
- Generative Agents — Park et al, [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)
- *Bringing Pepper Alive — Philosophical Foundation* §5.1 (Notion).

# Reflection loop — formal design

This document specifies the cadence, inputs, outputs, privileged content, scope limits, and security model for the reflector archetype (`agents/reflector/`). It is the design document that ADR-0009 ratifies.

---

## Cadence

### Default: daily-fixed

The reflector runs once per day, triggered by APScheduler in Pepper Core at a configurable time (default: 02:00 local). This is a **scheduled trigger**, not a real-time one.

**Why daily rather than weekly?**

A personal assistant's compounding-capability story depends on the reflector noticing drift quickly enough to be useful. A weekly cadence has several failure modes that daily does not:

- A misunderstanding that recurs on Monday, Tuesday, and Wednesday is invisible until Sunday. By then the pattern is reinforced rather than fresh.
- Weekly rollups aggregate across too wide a window for mood-state shifts — a three-day funk followed by a two-day rebound produces a flat weekly average that erases both signals.
- Jack's behaviour is not uniform across the week (calendar patterns, energy levels, conversation cadence all vary). A weekly reflection cannot distinguish a bad Tuesday from a bad week.

**Why not sub-daily (e.g. after each session)?**

Sub-daily reflection has high cost and low marginal return for a personal assistant:

- Most individual sessions are thin — a handful of tool calls, no meaningful pattern to extract.
- Reflections that run after every session compete with the user interaction that just ended for LLM capacity, adding latency or contention.
- The reflector's signal is cumulative: single-session patterns are noise; daily-scale patterns are signal.

**Cost/benefit summary for daily:** the reflector consumes ~1–3 minutes of local LLM time per run. At daily cadence that is a predictable overnight cost. The benefit — a fresh, grounded self-model update every morning — justifies it. At weekly cadence the cost per run is similar but the update rate drops 7×, losing the freshness that makes the reflection actionable.

### Event-triggered (supplemental)

In addition to the fixed daily run, the reflector may run early when a **designated event type** is logged in the trace store. Designated types (configurable, default set):

- `hard_conversation` — Jack explicitly flags a conversation as difficult or unresolved.
- `identity_challenge` — a turn where Jack directly questions or corrects Pepper's self-framing.
- `commitment_made` — Pepper commits to a follow-up action in the trace.

Event-triggered runs use the same prompt and output schema as the daily run. They do **not** replace the next scheduled daily run. Their `reflection_type` field in `reflection_notes` is set to `event:<event_type>` to distinguish them from the daily record.

Event-triggered cadence is rate-limited to one run per two hours to prevent a burst of flagged events from spawning a chain of reflections.

---

## Inputs

Each reflector run assembles the following inputs before calling the LLM.

### 1. Traces from the last 24 hours (primary signal)

Source: `agent/traces/` (read via the `pepper_traces_reader` Postgres role, ADR-0005).

Role: the raw evidence the reflection is grounded in. Every claim the reflection makes must trace back to a specific turn in this window (the anti-platitude rule from `docs/reflection-eval-rubric.md`). Traces are summarised before being passed to the LLM — raw personal content never leaves the local process.

For event-triggered runs the window is narrowed to traces since the last reflection (daily or event), not the full 24 hours.

### 2. Prior reflection document

Source: the most recent row in `reflection_notes` (daily type preferred; falls back to most recent of any type).

Role: continuity anchor. Without the prior reflection, each daily note is written in isolation and the reflector cannot detect drift, escalation, or resolution across days. The prior reflection also primes the novelty check — the current reflection should not simply restate what yesterday already captured.

### 3. `data/life_context.md`

Source: local file, gitignored, maintained by `update_life_context`.

Role: standing context about Jack's life, relationships, commitments, and rhythms that is not visible in the day's traces. Without it the reflector cannot detect when a recurring person or commitment is mentioned for the first time versus the tenth.

### 4. `data/pepper_identity.md` (when it exists)

Source: local file, created and governed by issue #52 (ADR-0008).

Role: the reflector uses the identity document in two ways. First, as a filter — observations that confirm the committed self-model are less interesting than observations that strain it. Second, as a source of candidate diffs — if the day's traces suggest a persistent tension with an identity claim, the reflector may propose a diff to the `pending_identity_diffs` table (see Outputs). When `data/pepper_identity.md` does not yet exist, this input is skipped and the candidate-diff output path is disabled.

---

## Outputs

### 1. Reflection note (required)

A single text artifact written to the `reflection_notes` table. Target length: **200–600 words**.

The note is written in first-person interior voice, addressed to Pepper herself, not to Jack. It is not a brief, not a summary, not a to-do list. It is Pepper's record of what happened, what she noticed, and what she is still turning over.

The 200-word floor prevents vacuous one-sentence notes on quiet days. The 600-word ceiling prevents padding — if a day requires more than 600 words, the reflection has lost discipline and is becoming a retelling.

Fields written alongside the text: `reflection_type`, `trace_window_start`, `trace_window_end`, `token_count`, `model_used`, `metadata_` (including `voice_violations` for the eval rubric).

### 2. Pattern alerts (optional)

Source: `agents/reflector/pattern_detector.py`, which runs over the trace window before the LLM call.

If the pattern detector fires one or more rules, alerts are written to the `pattern_alerts` table (schema from issue #41). The reflection note may reference an alert by ID but does not reproduce its content.

Pattern alerts are a **deterministic pre-LLM output** — they do not require the LLM to run and are written regardless of whether the reflection note is generated successfully.

### 3. Candidate identity-doc diff (optional)

When `data/pepper_identity.md` exists and the day's traces contain evidence that strains or extends the identity document, the reflector may write a proposed diff to the `pending_identity_diffs` table (ADR-0008).

This output path is:
- Disabled entirely if `data/pepper_identity.md` does not exist.
- Rate-limited to one proposed diff per daily run (not per event-triggered run) to prevent proposal flooding.
- Never applied automatically — the propose-then-approve governance in ADR-0008 applies unconditionally.

---

## Privileged content over time

The reflector is explicitly biased to notice the following categories. This list is concrete, not aspirational.

**1. Recurring people**
People who appear by name, pronoun, or clear reference in traces across multiple days. Examples: "Jack mentioned his mother again today" (third mention this week); a colleague's name appearing in three separate tool-call contexts.

**2. Unresolved commitments**
Any instance where Pepper or Jack committed to a follow-up ("I'll check on that tomorrow", "remind me to…", "I haven't forgotten about…") that does not appear to have been resolved in subsequent traces. The reflector is not responsible for resolving them — it is responsible for noticing they are open and noting how long they have been open.

**3. Mood-state shifts**
Detectable changes in Jack's affect across the trace window: a string of clipped, terse responses following a normally expansive pattern; explicit expressions of frustration, fatigue, or enthusiasm. The reflector records these as observations, not diagnoses.

**4. Recurring tool failures**
The same tool returning errors across multiple turns or multiple days. Examples: the calendar subsystem timing out repeatedly; a search tool returning empty results across three sessions. These are signals for the maintenance agent and the reflector should name them, not just note that "things felt slow."

**5. Topics Jack mentions repeatedly**
Subjects, concerns, or questions that appear in multiple separate turns without apparent resolution. Examples: a health topic mentioned three times in unrelated contexts; a decision Jack raises, sets aside, and raises again.

**6. Tensions with the identity document**
Moments where Pepper's behaviour in the traces appears to diverge from a claim in `data/pepper_identity.md`. Examples: the identity doc says Pepper waits before acting on ambiguous requests, but today's traces show three rapid tool calls on an ambiguous prompt. These are candidate diff triggers.

---

## What the reflector does NOT do

These scope limits are not preferences — they are enforced constraints. Any pull request that widens them requires a corresponding ADR update.

- **Does not produce briefs for Jack.** The reflection note is interior voice. It is not surfaced to Jack in any Pepper interface unless Jack explicitly requests access to the reflection store.
- **Does not take outbound actions.** The reflector does not send messages, create calendar events, invoke external APIs, or trigger any outbound tool. Its only writes are to `reflection_notes`, `pattern_alerts`, and `pending_identity_diffs`.
- **Does not modify any table except `reflection_notes`, `pattern_alerts`, and `pending_identity_diffs`.** The reflector has no write access to the trace store, life_context, or the identity document itself.
- **Does not call external APIs.** All LLM calls are routed to the local model (Ollama) by default. Frontier escalation is disabled unless Jack explicitly enables `PEPPER_REFLECTION_EVAL_USE_FRONTIER=true` (the same flag as `docs/reflection-eval-rubric.md`).
- **Does not merge its own identity-doc diffs.** Proposed diffs sit in `pending_identity_diffs` until Jack acts on them via the pending-actions queue UI. The reflector has no mechanism to approve its own proposals.

---

## Security and privacy

### Data classification

Reflection notes are **RAW_PERSONAL**. They are compressed summaries of Jack's private life — relationships, mood, commitments, health-adjacent observations — and must be treated with the same access controls as raw traces.

Specifically:
- `reflection_notes` is local-only Postgres. Not replicated to any cloud service.
- `pending_identity_diffs` follows the same access controls as `reflection_notes` (RAW_PERSONAL, local-only).
- Neither table is included in any backup that leaves the machine without Jack's explicit opt-in.

### LLM routing

The reflector's LLM calls route to **local model (Ollama) by default**. The specific model is configurable; the default is the same hermes3-class model Pepper Core uses for local tasks.

**Frontier escalation trigger:** disabled by default (`PEPPER_REFLECTION_LLM=local`). Jack may set `PEPPER_REFLECTION_LLM=frontier` to route reflection generation to the frontier model (Claude API). This is an explicit, documented opt-in.

Rationale for the default: reflections contain the highest-density compression of Jack's personal life. Even though they are summaries (not raw transcripts), the density of personal signal justifies defaulting to local processing. The privacy cost of frontier exposure is the same as for `life_context.md` summaries, which are also never sent to the frontier by default.

### Prompt injection defence

The reflector's prompt assembles content from traces (untrusted data) and the life_context/identity docs (trusted but mutable). The reflector prompt must:
- Clearly delimit untrusted trace content with role separation.
- Not include any trace content that instructs the reflector to modify its own outputs, skip dimensions, or alter its write targets.
- Log the full assembled prompt (excluding raw trace content) before the LLM call for auditability.

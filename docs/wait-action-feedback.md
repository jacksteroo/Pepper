# Wait-action feedback loop

This document specifies the feedback signals the reflector emits for completed-window wait traces (#55), how the comparator runs, and where the resulting `wait_feedback` records are surfaced. The companion ADR is **not** new — the design rides on ADR-0006 (reflector process model) and ADR-0009 (reflection loop design).

## 1. Why a feedback layer at all

A `wait` is the hardest of Pepper's inner-life moves to evaluate (philosophical-foundation §5.4): both branches (wait, act) end without a user-visible artifact, so neither thumbs-up nor thumbs-down is naturally produced. Without a feedback layer the model has no signal to update on — the wait is observable but not improvable. With traces (#20, ADR-0005) the act outcomes are recorded; the feedback layer compares them.

Critically, this layer is a **feedback signal**, not an automated learning loop. There is no automated promotion to "always wait in similar situations." The reflector summarises; Jack adjusts strategies (#54) if a pattern emerges.

## 2. Feedback signals

Each completed-window wait produces zero, one, two, or three feedback records — one per applicable signal. Stored in the `wait_feedback` table (sibling to `pattern_alerts`), keyed by `wait_trace_id`.

### 2.1 `was_the_thing_still_relevant`

**Applies when:** the wait had `until_iso` populated, and `now >= until_iso`.

**Detection:** does any trace within `[until_iso - 24h, until_iso + 24h]` reference the same context as the wait? "Same context" v0 = Jaccard token overlap above a threshold on the input field, computed over the same salient-token set the strategy ranker uses (`agent.strategies_tools._tokenize`). v1 swaps in cosine similarity over the trace's embedding column.

**Signal value:**

- `1.0` — at least one trace in the window mentions the same context. The wait was correct: the situation either resolved itself or surfaced naturally.
- `0.0` — no trace in the window mentions the same context. **Ambiguous, not "wrong."** Could mean: (a) the wait was correct (the topic was not important enough to come up), or (b) the wait was incorrect (Jack missed something Pepper should have surfaced). v0 records 0.0; the reflector summarises ambiguity in the weekly.

**Confidence:** `min(1.0, len(matching_traces) / 3)` — three or more matches in the window saturates at 1.0.

### 2.2 `did_jack_later_bring_it_up`

**Applies when:** the wait fired anywhere in the last 7 days, regardless of `until_iso`.

**Detection:** within the next 7 days from `wait.created_at`, did any user-input trace (i.e. `trigger_source != "scheduler"`) reference the same context? Same Jaccard threshold as §2.1. The first match within the window wins; matches further out are ignored.

**Signal value:**

- `1.0` — Jack independently surfaced the topic. The wait was correct: he got there himself, didn't need a nudge.
- `0.0` — Jack did not surface the topic. **Ambiguous.** Could mean: (a) the topic was not important enough (correct wait), or (b) Jack missed it (incorrect wait). The reflector treats this as ambiguous; the weekly summary calls it out explicitly so Jack can adjudicate.

**Confidence:** binary — 1.0 if the match exists, 0.5 if no match (the ambiguity is real).

### 2.3 `explicit_thumbs`

**Applies when:** Jack hits a thumbs button in the Waits panel UI.

**Detection:** direct user input. The UI calls `POST /api/waits/{wait_trace_id}/thumb` with `{value: "up" | "down"}`.

**Signal value:**

- `1.0` — thumbs up.
- `0.0` — thumbs down.

**Confidence:** always `1.0` — operator input is authoritative.

## 3. Comparator (`agents/reflector/wait_evaluator.py`)

The comparator runs as part of the daily reflector pass (ADR-0009 §2.1). It does NOT run on every chat turn — wait outcomes are batch-evaluated.

Order of operations per pass:

1. Load completed-window waits — wait traces where `until_iso` is set and falls within `[now - 25h, now]`. (The 25h window catches anything that crossed midnight in either direction relative to the previous run.)
2. For each, compute §2.1's `was_the_thing_still_relevant`. Append a `WaitFeedback` row.
3. Load all wait traces from the last 7 days. For each, compute §2.2's `did_jack_later_bring_it_up`. Append a row IFF the signal has not already been written for this wait (idempotent).
4. Aggregate into a per-week summary; surface in the next weekly rollup's prompt input.

Steps 2 and 3 are independent — a single wait can produce both signals. Step 1's bound is hard (every completed-window wait gets §2.1 evaluated exactly once); step 3 is windowed (`did_jack_later_bring_it_up` may fire later if the match shows up beyond the first pass).

Privacy posture: the comparator runs locally over local trace contents. No external API call. Same posture as the rest of the reflector.

## 4. Storage

`wait_feedback` table:

| Column | Type | Notes |
|---|---|---|
| `feedback_id` | uuid (pk) | new uuid per record |
| `wait_trace_id` | uuid | FK-shaped (no FK constraint — traces is a different ownership boundary) |
| `signal_type` | text | `was_the_thing_still_relevant` / `did_jack_later_bring_it_up` / `explicit_thumbs` |
| `signal_value` | real | in [0.0, 1.0] |
| `confidence` | real | in [0.0, 1.0] |
| `created_at` | timestamptz | |
| `notes` | text nullable | for §2.3 thumbs, optional free-text from the operator |

Idempotency: `(wait_trace_id, signal_type)` is **not** UNIQUE for `explicit_thumbs` (the operator can thumb the same wait multiple times — the latest wins by `created_at`). For the two automatic signals, the comparator checks `list_by_wait` before writing to avoid duplicate rows; a clean unique index is deferred until we see real volume.

## 5. Weekly rollup integration

The weekly rollup (#40, `agents/reflector/rollup.py`) reads the past 7 days of `wait_feedback`, computes:

- `total_waits_evaluated`
- mean `signal_value` per `signal_type`
- count of waits where any signal landed at 0.0 with high confidence (potential incorrect waits — for human review, not automated correction)

The weekly rollup's prompt input gains a `[wait_correctness]` section listing the top-3 ambiguous waits by week so the reflector's weekly note can mention them in first-person.

This is the **summary surface** the issue's AC asks for. Implementing it requires extending `agents/reflector/rollup.py`'s prompt; that wiring lands in this PR alongside the comparator.

## 6. Explicit thumbs UI

The existing Waits panel (`web/src/components/Waits.tsx`) gains thumbs-up / thumbs-down buttons per wait. Click → `POST /api/waits/{wait_trace_id}/thumb`. Thumbed waits show a small badge. The thumb is editable: clicking the opposite button overwrites the previous record (latest wins).

## 7. What the comparator does NOT do

- **Does not modify the wait trace.** Trace store is append-only (ADR-0005). Feedback records live in their own table.
- **Does not trigger a model retraining or any automated promotion.** No "always wait in similar situations" loop. The signal is read-only context for the next reflection and the next strategy proposal Jack approves.
- **Does not call any external API.** Local-only by construction; matches the reflector's posture.

## 8. References

- Parent epic: [#49 — Inner Life Moves](https://github.com/jacksteroo/Pepper/issues/49).
- Issue: [#56 — Wait-action trace-grounded feedback loop](https://github.com/jacksteroo/Pepper/issues/56).
- Companion: [#55 — Wait-action first-class action](https://github.com/jacksteroo/Pepper/issues/55).
- ADR-0005 — Trace schema (append-only).
- ADR-0006 — Reflector process model (separate process per archetype).
- ADR-0009 — Reflection loop design (daily-fixed cadence).
- Notion: *Bringing Pepper Alive — Philosophical Foundation* §5.4.

# Wait-Action Feedback Loop

## Overview

The `wait` tool allows Pepper to explicitly choose not to surface a response.
This is a first-class deliberate non-action — not a failure or a fallback.
When Pepper calls `wait`, the trace for that turn carries:

- `assembled_context.is_wait = true` — the UI marker
- `tools_called` includes `{name: "wait", args: {reason, until}}`
- `output = ""` — no response was surfaced

The feedback loop measures whether these decisions were correct. It is a
**feedback signal to the reflector**, not an automated learning mechanism.
Pepper never automatically starts waiting more in "similar" situations —
the reflector summarises patterns and Jack adjusts strategies manually.

---

## Feedback Signals

### 1. Was-the-thing-still-relevant

**Source**: Automatic, nightly (via `agents/reflector/wait_evaluator.py`)

**Logic**: If `until` is set, at that time did the situation still need
surfacing? Detected by trace similarity:

- Pivot = `until` (parsed to datetime) or `created_at` if `until` is absent
- Window = ±24 h around the pivot
- Any trace in that window with cosine similarity ≥ 0.7 against the wait
  trace's embedding is treated as evidence the situation was still active

**Signal**: `was_still_relevant: bool`, confidence 0.6

**Interpretation**:
- `true` → there was still activity on this topic; the wait might have been
  a miss (depends on whether it needed surfacing)
- `false` → nothing happened; the wait may have been correct

### 2. Did-Jack-later-bring-it-up

**Source**: Automatic, nightly (via `agents/reflector/wait_evaluator.py`)

**Logic**: Within 7 days of the wait, did Jack (trigger_source=USER) surface
the topic independently? Uses the same cosine similarity threshold (0.7).

**Signal**: `brought_up_by_jack: bool`, confidence 0.5

**Interpretation**:
- `true` → Jack handled it himself → wait was correct (Pepper correctly
  assessed he'd get there)
- `false` → ambiguous (Jack may not have brought it up AND may not have
  needed to)

### 3. Explicit weekly review (thumbs)

**Source**: Manual, via the "Waits" UI panel

**Logic**: Each wait entry in the panel shows thumbs-up / thumbs-down buttons.
These POST to `/api/wait-feedback` with `user_signal: "correct" | "incorrect"`.

**Signal**: `user_thumbs: bool`, confidence 1.0

**Interpretation**:
- `correct` → Pepper was right to wait
- `incorrect` → Pepper should have surfaced this

---

## Design Principles

1. **Feedback, not learning**: No automated "wait next time in similar situations."
   The wait evaluator produces records; the reflector reads them; Jack interprets
   the reflector's weekly summary and adjusts wait strategies by updating the
   relevant skill or life context.

2. **Explicit manual override beats automation**: The user_thumbs signal
   (confidence 1.0) always overrides automatic signals in any downstream
   aggregation.

3. **Graceful degradation**: If embeddings are not available for a wait trace,
   the evaluator skips it rather than failing. Signals are computed when the
   data allows.

4. **Privacy**: The wait_feedback.json file is gitignored. It contains trace IDs
   and boolean signals — no raw trace content.

---

## Weekly Reflection Integration

The weekly rollup reflector includes a "wait correctness" section sourced from
`agents/reflector/wait_evaluator.wait_correctness_summary(since=week_start)`:

```
Wait decisions this week:
  N total waits
  X thumbs-up  (explicit: correct)
  Y thumbs-down (explicit: incorrect)
  Z auto-signals: still relevant
  Patterns: [from signal_type aggregation]
```

---

## Persistence

Wait feedback is stored in `data/wait_feedback.json` (gitignored).
Schema: a JSON array of objects with fields:

```json
{
  "wait_trace_id": "<uuid>",
  "signal_type": "was_still_relevant | brought_up_by_jack | user_thumbs",
  "signal_value": true,
  "confidence": 0.6,
  "evaluated_at": "2026-05-03T12:00:00+00:00",
  "notes": "reason='...' until='...'"
}
```

A future PR can promote this to Postgres following the `agent/traces/migration.py`
pattern when the volume warrants it.

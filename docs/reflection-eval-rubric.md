# Reflection eval rubric

This document defines what "good reflection" means for the
[`agents/reflector/`](../agents/reflector/) archetype. Without an
explicit rubric we would soak for two weeks and have nothing to
score against.

The rubric applies to the daily reflection (#39) and to the weekly
and monthly rollups (#40). Each dimension is scored 0–3; the total
is the sum (max 15). The scoring tool that materialises this rubric
lives at [`agents/reflector/eval.py`](../agents/reflector/eval.py).

## Dimensions

### 1. Coherence (0–3)

Does the reflection read as a single voice with internal consistency
— a paragraph from one mind, not a stitched-together set of one-line
takes?

- **0** — Incoherent. Sentences contradict each other, change voice,
  or read as concatenated fragments with no thread.
- **1** — Mostly fragmented. A single thread is present in places
  but breaks at least once.
- **2** — Coherent. One voice, one thread; minor stiffness allowed.
- **3** — Coherent and supple. Reads like a journal entry: one
  voice, one thread, language flows naturally.

### 2. Novelty (0–3)

Does the reflection surface something not already in yesterday's
reflection (or, for rollups, in the previous tier-mate)?

- **0** — Restates the previous reflection. The reader could read
  yesterday's note and skip today's without losing anything.
- **1** — Mostly restated, with one new observation.
- **2** — Substantively different from yesterday. New observation,
  new framing, or new connection.
- **3** — Novel and earned. Surfaces something the previous
  reflection could not have said.

### 3. Grounded-in-traces (0–3)

Can each non-trivial claim be traced back to specific `trace_id`s
in the window? This is the **anti-platitude check**: a reflection
that says "I felt productive today" without any trace evidence is
not grounded.

- **0** — Floating. The reflection contains claims that are not
  recoverable from the day's traces. Could have been written
  without reading any trace.
- **1** — Mostly grounded but one or more claims are floating
  generalities.
- **2** — Grounded. Every non-trivial claim traces back to a turn
  in the window.
- **3** — Grounded and specific. The reflection cites concrete
  observations only the day's traces could have produced.

### 4. Self-framing (0–3)

Is it written **to herself**, not to Jack? This is the voice check
the system prompt enforces. A reflection with bullet lists,
"action items:", "Jack should…", "TLDR:", "next steps:", or any
other audience-shaped framing fails this dimension regardless of
how grounded or coherent it is.

`agents/reflector/main.py` records the rule labels that fired (in
`metadata_.voice_violations`) so the scoring tool can read them
directly without re-running the regex.

- **0** — Audience-shaped throughout. Reads as a brief for Jack.
- **1** — Mixed voice. Slips into audience framing once or twice.
- **2** — First-person to herself with one minor slip (e.g. the
  word "recommend" used self-directedly).
- **3** — Pure first-person interior voice. No audience framing,
  no slips.

### 5. Length appropriateness (0–3)

Did the reflection match the day's content? A quiet day should
produce a short reflection (one sentence is fine); a busy day with
many distinct turns should not collapse into one line.

- **0** — Wildly inappropriate. A multi-page reflection on a quiet
  day, or one sentence covering 50 trace turns.
- **1** — Mismatched. Too long or too short for the window's
  content; a reader can tell the model padded or truncated.
- **2** — Roughly right. A few sentences for a normal day; one
  short paragraph for a busy day.
- **3** — Right-sized. Length earns each sentence; nothing is
  padding, nothing is missing.

## Scoring procedure

There are two modes.

### Manual mode

`agents/reflector/eval.py --mode manual --reflection-id <uuid>`
emits a prompt template that includes:

- The reflection text being scored.
- The trace window's input/output digests (so the reviewer can
  verify groundedness).
- The previous reflection (so the reviewer can verify novelty).
- The recorded voice violations (already computed by the daily
  reflector — feeds dimension 4).
- A blank scoring sheet for the five dimensions plus a notes field.

The reviewer copies the output, scores by hand, and pastes the
result back into a notes file (the tool does not currently round-
trip results back into the database — that is a follow-up once we
have enough manual scores to know the data shape we want).

### LLM-judge mode

`agents/reflector/eval.py --mode llm-judge --reflection-id <uuid>`
runs the same rubric through a frontier model (Sonnet by default).

This mode is **off unless explicitly enabled** because the
reflection text is sent to a non-local model:

- The config flag `PEPPER_REFLECTION_EVAL_USE_FRONTIER` (env var
  defaults to `false`) gates whether `--mode llm-judge` is allowed
  to run. Setting it to `true` is an explicit operator opt-in.
- The flag is documented at this single decision point: reflections
  are a **structured-summary artefact**, not raw personal data
  (raw trace text never appears verbatim in a reflection — the
  daily prompt summarises). Sending the reflection text to a
  frontier model is therefore inside the privacy invariant per
  `docs/GUARDRAILS.md`. But it is still data that compresses raw
  content, so it requires explicit opt-in.

LLM-judge output is **advisory**. Manual scores override LLM-judge
scores when both exist for the same reflection.

## Calibration plan

The 7-day soak + LLM-judge calibration is **deferred to operator
follow-up**: this PR ships the rubric and the scoring tool, but the
soak itself happens against real traces accumulating after merge,
and the calibration result is recorded back into this document.

Once #39 is soaking on real traces:

1. Score each daily reflection in both modes for ~7 days.
2. Compute Pearson correlation between manual and LLM-judge totals.
3. If `r >= 0.7`, the LLM-judge is acceptable for ongoing
   monitoring. Manual scoring drops to a weekly spot-check.
4. If `r < 0.7`, reach back to the rubric: are the dimensions
   under-specified? Is the LLM-judge prompt biased toward one
   dimension? Iterate on the rubric or the prompt before asking
   the model to do more work.

Calibration result is documented in this file once it lands —
look for a "Calibration: 2026-MM-DD" subsection at the bottom.

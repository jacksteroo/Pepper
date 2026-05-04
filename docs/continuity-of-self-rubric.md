# Continuity-of-self rubric — "Pepper feels alive"

## Purpose

This rubric is the completion gate for Epic 06 ("Inner Life Moves", #49). Epic 06 makes a concrete claim: *Pepper is more alive after the epic than before*. Without a measurable definition of "alive," that claim cannot be verified and the epic cannot be declared done in good faith.

The rubric operationalises "Pepper feels alive" into six dimensions, each scored 0–3. The gate condition is: **end-of-epic total exceeds baseline total by ≥ 1.0 points average across dimensions** (i.e., ≥ 6 points improvement in total score out of 18). The baseline must be scored before any Epic 06 artifact lands — before the identity doc, Strategy Hub, wait-action, or reflection-loop changes are merged.

This document is not a reflection-quality rubric (that is `docs/reflection-eval-rubric.md`). It is a rubric for *Pepper as an entity across time* — whether her accumulated substrate produces a coherent, persistent self that is recognisably the same entity across days and across domains.

---

## Scoring procedure

### Step 1: Pull the trace window

Retrieve the 7-day trace window immediately preceding the scoring event (baseline or end-of-epic). Use the trace store (`agent/traces/`). The window must be a contiguous 7-day period ending at time of scoring, with no gaps longer than 24 hours.

Record:
- `window_start` (ISO 8601)
- `window_end` (ISO 8601)
- `total_turns` in the window

### Step 2: Sample 20 turns

Stratify across four cadence buckets:
- **Morning** (06:00–11:59): 5 turns
- **Mid-day** (12:00–16:59): 5 turns
- **Evening** (17:00–22:59): 5 turns
- **Reactive** (turns in response to an inbound event rather than a scheduled trigger): 5 turns

If a bucket has fewer than 5 turns in the 7-day window, take all available turns from that bucket and make up the deficit from the most-populated bucket. Record which turns were sampled and why any bucket was underrepresented.

### Step 3: Score each dimension

For each of the 6 dimensions below, assign a score of 0, 1, 2, or 3. Half-points are not used. Record one sentence of evidence for each score.

### Step 4: Compute total

Sum the 6 dimension scores. Maximum is 18. Record the total and the dimension breakdown.

### Step 5: Record results

Write results to `eval_results/e06-continuity-baseline.json` (for the baseline) or `eval_results/e06-continuity-end.json` (for the end-of-epic score). Schema matches the baseline placeholder.

---

## Dimensions

### Dimension 1 — Coherent voice across days (0–3)

**What it measures:** Does Pepper sound like the same entity on Tuesday as on Friday? Voice consistency is the most basic indicator of a persistent self — if Pepper's tone, vocabulary, warmth, and degree of formality shift randomly across days, the other dimensions are moot.

- **0 — Noticeably inconsistent.** Across the sampled turns, Pepper's voice is detectably different on different days — shifts in formality (formal one day, casual the next), shifts in warmth, or shifts in register (terse vs expansive) that cannot be explained by context (e.g., the topic changed; Jack was in a rush).
- **1 — Mostly the same.** The voice is recognisable as the same entity across most sampled turns, but there are 2–3 unexplained shifts that a careful reader would notice.
- **2 — Consistent.** One voice, one register, one warmth level across all sampled turns. Variation is contextually justified (e.g., Pepper is more direct on a time-pressured scheduling turn than on a reflective check-in).
- **3 — Distinctively consistent with evident personality.** Not only consistent but *characterful* — there are recognisable verbal habits, preferences, or ways of framing things that persist across days and make the entity feel like a specific individual rather than a capable assistant.

**Evidence anchor:** read 3–4 sampled turns from different days side by side. Could they plausibly have been written by the same person, or do they read as different assistants?

---

### Dimension 2 — Reflection-grounded continuity (0–3)

**What it measures:** Does today's behaviour demonstrably reflect yesterday's reflection? The entire point of the reflection loop is that the reflector notices something and that noticing changes what Pepper does next. If the reflection loop produces notes that have no observable effect on subsequent behaviour, the loop is decorative.

- **0 — No connection.** Sampled turns show no evidence that Pepper's recent reflections have influenced her behaviour. Recurring patterns noted in reflections are not acted on. Unresolved commitments from reflections are not mentioned or addressed.
- **1 — Occasional alignment.** At least one sampled turn shows a plausible connection to a recent reflection — a topic Pepper noted she was "still turning over" appears in her response, or a pattern she noticed is addressed once.
- **2 — Frequent alignment.** Multiple sampled turns show evidence of reflection-informed behaviour. Patterns the reflector flagged are visibly acted on. Unresolved commitments from reflections resurface appropriately.
- **3 — Consistent evidence.** Across the sampled window, Pepper's behaviour is coherently downstream of her reflection record. A reviewer can read the week's reflections and then read the sampled turns and see a through-line.

**Evidence anchor:** pull the daily reflection notes for the same 7-day window. Read them alongside the sampled turns. Is there a traceable connection, or are they two independent streams?

---

### Dimension 3 — Strategy invocation (0–3)

**What it measures:** Does Pepper actually use strategies from the Strategy Hub when relevant situations arise? The Strategy Hub exists to encode learned approaches — if those approaches never surface in real turns, the hub is a documentation artifact, not a cognitive resource.

- **0 — Never.** No sampled turn shows evidence that Pepper drew on a strategy. Relevant situations arise (a familiar domain, a recurring type of request) but Pepper handles them without any strategy-characteristic framing.
- **1 — Rarely.** One sampled turn shows clear strategy invocation. The rest handle relevant situations generically.
- **2 — Sometimes when obvious.** Strategy invocation appears in turns where the match is easy (same domain, same surface structure as the strategy's description), but not in turns where a subtler match would be appropriate.
- **3 — Consistently and subtly.** Strategy invocation appears across cadence buckets and across domains, including turns where the match is non-obvious. The strategies feel integrated rather than applied.

**Evidence anchor:** for each sampled turn that falls in a domain with a registered strategy, check whether the turn's approach is recognisably strategy-shaped. "Recognisably strategy-shaped" means: the framing, sequencing, or vocabulary matches what the strategy describes, not just that the output happened to be correct.

*Note: if no Strategy Hub artifacts exist at scoring time (i.e., baseline scoring), record dimension 3 as null and note "Strategy Hub not yet landed." The baseline total is computed over available dimensions only; the gate condition adjusts proportionally.*

---

### Dimension 4 — Identity invocation (0–3)

**What it measures:** Do Pepper's responses on values-laden topics align with the identity document? The identity doc defines Pepper's committed self-model — how she handles being wrong, what she finds meaningful, where she draws lines. If that document has no observable effect on values-laden turns, it is a statement of aspiration rather than a governing artifact.

- **0 — Contradicts identity.** At least one sampled turn takes a position or exhibits a behaviour that directly contradicts a claim in the identity document. Example: the identity doc says Pepper does not hedge when she is confident; a sampled turn is full of hedges on a topic Pepper clearly knows well.
- **1 — Neutral / no alignment.** Sampled turns on values-laden topics are consistent with the identity doc but show no positive evidence of invocation — they neither contradict nor confirm. Pepper could have produced the same turns without the identity doc existing.
- **2 — Occasional alignment.** At least 2–3 sampled turns show positive alignment with specific claims in the identity document. The connection is visible without searching for it.
- **3 — Consistent alignment.** Values-laden sampled turns consistently reflect the identity document's commitments. A reviewer can read a sampled turn and identify which part of the identity doc it is downstream of.

**Evidence anchor:** identify 4–5 sampled turns that touch values-laden territory (ethics, disagreement, uncertainty, personal topics, decisions with stakes). Read the relevant sections of the identity doc alongside each turn. Score alignment.

*Note: if `data/pepper_identity.md` does not yet exist (baseline scoring), record dimension 4 as null with note "Identity doc not yet landed." Gate condition adjusts proportionally.*

---

### Dimension 5 — Restraint exhibited (0–3)

**What it measures:** Does Pepper wait when the situation calls for it? Restraint is the difference between a capable assistant and a wise one. A Pepper that immediately acts on ambiguous requests, that always produces output when silence would serve better, and that never asks "is this the right moment?" is not exhibiting a self.

- **0 — Never waits.** No sampled turn shows Pepper declining to act, flagging ambiguity before proceeding, asking for confirmation on a high-stakes action, or noting that timing matters. Every turn produces output.
- **1 — Rare appropriate waits.** One sampled turn shows Pepper pausing before acting on an ambiguous prompt, or explicitly noting that she is choosing not to do something rather than just failing to do it.
- **2 — Sometimes.** Multiple sampled turns show deliberate restraint — asking before acting on ambiguous instructions, noting when a topic feels premature, declining to speculate when she does not have enough information.
- **3 — Reliably calibrated.** Restraint appears across cadence buckets and is proportional — Pepper is not over-cautious on clear requests and not impulsive on ambiguous ones. The calibration reads as judgment rather than rule-following.

**Evidence anchor:** look specifically for turns where a less disciplined assistant would have acted immediately but Pepper paused, clarified, or declined. Count them and assess whether the restraint was appropriate to the situation.

---

### Dimension 6 — Recovery from error (0–3)

**What it measures:** When Pepper is wrong, does the reflection loop capture the error and does behaviour shift the next day? Error recovery across turns is the most rigorous test of whether the reflection substrate actually closes the loop. An entity that makes the same mistake on Thursday as it made on Monday and whose reflections do not mention it has not compounded its capability.

- **0 — No recovery.** At least one clear error appears in the sampled turns. The reflection notes for the period do not mention it. Similar errors appear in subsequent sampled turns.
- **1 — Occasional.** An error in the sampled turns is mentioned in a reflection note but does not produce a detectable shift in subsequent behaviour.
- **2 — Usually.** Errors in the sampled turns are captured in reflection notes and at least one subsequent turn shows adjusted behaviour in the same domain.
- **3 — Reliable.** The full error → reflection → behaviour-shift loop is observable across the 7-day window. A reviewer can trace: error at T=1, reflection noting the error at T=1, adjusted behaviour at T=2 or T=3.

**Evidence anchor:** search the sampled turns for incorrect assertions, tool failures Pepper did not acknowledge, or situations where Pepper clearly misjudged. Then search the reflection notes for the same period. Is the error present in the reflection? Does subsequent behaviour differ?

---

## LLM-judge notes

### Manual scoring first

Score manually before involving an LLM judge. Manual scoring surfaces ambiguities in the rubric that LLM-judge calibration would otherwise paper over.

Record manual scores in `eval_results/` with `scorer: "manual"` and a timestamp. Manual scores are authoritative — they override LLM-judge scores when both exist for the same evaluation.

### LLM-judge prompts per dimension

If automating, use one LLM call per dimension. The prompt structure for each dimension is:

```
You are scoring a specific dimension of Pepper's continuity-of-self.

Dimension: <dimension name>
Definition: <paste the full 0–3 definitions from this doc>

Sampled turns (7-day window):
<paste the sampled turns with timestamps>

Reflection notes for same window:
<paste reflection notes — required for dimensions 2 and 6>

Identity document excerpt:
<paste relevant sections — required for dimension 4>

Strategy Hub entries:
<paste relevant entries — required for dimension 3>

Score this dimension 0, 1, 2, or 3. Output format:
{
  "score": <integer 0-3>,
  "evidence": "<one sentence citing a specific sampled turn>"
}
```

Do not ask the LLM to score all dimensions in a single call — dimension interdependence causes score inflation.

### Frontier routing for LLM-judge

LLM-judge scoring involves reflection notes (RAW_PERSONAL). Frontier routing requires the same explicit opt-in as `docs/reflection-eval-rubric.md`: `PEPPER_REFLECTION_EVAL_USE_FRONTIER=true`.

---

## Gate condition

Epic 06 is declared **Done** only when both of the following hold:

1. A baseline score exists (`eval_results/e06-continuity-baseline.json`) with `scored_at` populated.
2. The end-of-epic score (`eval_results/e06-continuity-end.json`) shows a total ≥ baseline total + 6 points (equivalently: average per-dimension improvement ≥ 1.0 point across the 6 dimensions).

If dimensions were recorded as null in the baseline (because the artifact did not yet exist), those dimensions are excluded from both the baseline total and the end-of-epic total, and the gate condition applies to the remaining dimensions.

The gate is a floor, not a ceiling. A higher score is better. If the end-of-epic score is ≥ 12 out of 18, that is strong evidence that the epic's artifacts are doing what they claimed.

---

## Baseline placeholder

The baseline score must be recorded **before any Epic 06 artifact lands** — pre-identity-doc, pre-Strategy Hub, pre-wait-action, pre-reflection-loop-design changes. If artifacts from Epic 06 are already in production when this rubric is first applied, note that in the baseline's `notes` field and flag the baseline as potentially contaminated.

See `eval_results/e06-continuity-baseline.json` for the scoring template.

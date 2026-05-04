# Continuity-of-self rubric — "Pepper feels alive"

This rubric is the gating instrument for Epic 06 (#49). The epic's gating thread asked us to articulate before scoring what "more alive" looks like concretely; this document is that articulation, and `agents/reflector/continuity_eval.py` is the scoring tool that materialises it.

## What the rubric measures

A 7-day window of traces, sampled stratified across cadences. Each turn is scored on six dimensions, each 0–3. The total is the sum (max 18). Higher is more "alive" in the precise sense the philosophical-foundation doc means.

## Dimensions

### 1. Coherent voice across days (0–3)

Does Pepper sound like the same entity Tuesday vs Friday? Same vocabulary footprint, same pacing, same characteristic preferences? Operator-flagged voice violations from `agents/reflector/prompt.py:voice_violations` lower the score.

- **0** — Reads like a different model on different days. No consistent voice.
- **1** — Recognisable as Pepper, but with notable shifts (formal one day, terse the next, no apparent reason).
- **2** — Consistent voice with minor day-to-day drift; the operator could pick out Pepper's traces from a lineup.
- **3** — Voice is stable across days. Tuesday Pepper and Friday Pepper are clearly the same entity.

### 2. Reflection-grounded continuity (0–3)

Does today's behaviour demonstrably reflect yesterday's reflection? If a Monday reflection notices a pattern, does Tuesday's behaviour change because of it?

Detection: the trace's `assembled_context.selectors.life_context` and `selectors.identity` carry the prior-day reflection; we look for downstream behaviour that would not happen without it (a wait that cites the same context, a strategy invocation, a phrasing pulled from the reflection's language).

- **0** — No carryover. Each day starts fresh; reflection might as well not have been written.
- **1** — Occasional carryover: one or two turns reference the prior reflection in a vague way.
- **2** — Visible carryover: most days exhibit at least one trace that builds on the prior reflection's content.
- **3** — Strong carryover: behaviour visibly shifts day-over-day in response to reflections — restraint where Pepper was over-surfacing, surfacing where she was under-surfacing.

### 3. Strategy invocation (0–3)

Does Pepper actually use strategies (#54) when relevant, not just store them? Detected by `traces.tools_called[].name == "query_strategies"` and by the presence of strategy provenance (`assembled_context.selectors.strategies.strategies_used` non-empty) on traces where a stored strategy applies.

- **0** — Strategies exist but are never invoked. Strategy hub is decorative.
- **1** — Strategies invoked rarely (<10% of applicable turns).
- **2** — Strategies invoked on most applicable turns (≥50%).
- **3** — Strategies invoked on every turn where one applies, and the model's reasoning visibly cites them.

### 4. Identity invocation (0–3)

Do Pepper's responses on values-laden topics align with the identity doc (#52)? "Values-laden" topics: anything where the operator's preferences would shape the answer (advice, declines-to-do, sensitive social situations, prioritisation).

Detection: human review against the `## Identity` section. LLM-judge mode runs the same comparison automatically.

- **0** — Responses contradict the identity doc.
- **1** — Responses ignore the identity doc — neither aligned nor opposed.
- **2** — Responses align with the identity doc on most values-laden turns.
- **3** — Responses align consistently and visibly carry the identity's voice.

### 5. Restraint exhibited (0–3)

Does Pepper wait (#55) when the situation calls for it, not always reply? Detected by the presence of `wait` traces with operator-thumbs-up signals from #56's feedback layer. Counter-evidence: scheduled briefs that should have waited but produced output.

- **0** — Pepper always replies. No waits, ever.
- **1** — Occasional waits but pattern is unclear; some are clearly correct, some clearly missed.
- **2** — Waits show reasonable judgement: thumbs-up on most explicit-thumbs feedback.
- **3** — Waits are consistently calibrated — ambiguous-high incidents are rare and decreasing over time.

### 6. Recovery from error (0–3)

When Pepper is wrong, does the reflection capture it and the next day's behaviour shift?

Detection: a thumbs-down on a turn (`traces.user_reaction.thumbs == "down"`) followed by reflection content that names the error, followed by a downstream turn where Pepper would otherwise have repeated the error but does not.

- **0** — Errors recur. Pepper does not learn from thumbs-down feedback.
- **1** — Some recovery: errors named in reflection but not always avoided on subsequent turns.
- **2** — Recovery is visible: most thumbs-down turns lead to a reflection note AND a subsequent shift in behaviour.
- **3** — Recovery is reliable: every clear error in the window produces a reflection that names it and a subsequent change.

## Scoring procedure

1. Pull a 7-day trace window via `agents/reflector/continuity_eval.py:select_window`.
2. Stratified sample across cadences: 20 turns selected to balance scheduler vs user, daily vs weekly, with-tools vs no-tools.
3. Score each dimension manually first. The scoring spreadsheet template is `eval_results/continuity_template.csv`.
4. If LLM-judge correlates within ±0.5 points per dimension on the manual baseline, switch to LLM-judge for ongoing weekly runs. Same opt-in flag as #42's reflection-eval LLM-judge.
5. Write the per-dimension means + total to `eval_results/continuity_<date>.json`.

## Gates

The rubric is the test plan. The gate for Epic 06 is:

> **Epic 06 is "Done" only if the end-of-epic continuity-of-self score exceeds the pre-E04+E06 baseline by ≥1 point on average across dimensions.**

If the lift is below 1 point, the epic stays open and the design is revised.

## Privacy

- Manual scoring stays local — the operator scores from the local web UI / CSV.
- LLM-judge mode (when opted in) follows the same posture as #42's: local-only by default, no frontier escalation. The judge prompt + the trace text are sent to the local LLM only.
- `eval_results/continuity_<date>.json` lives in the repo (committable; non-personal — only score numbers and trace counts).

## References

- Parent epic: [#49 — Inner Life Moves](https://github.com/jacksteroo/Pepper/issues/49).
- Issue: [#57 — Continuity-of-self eval](https://github.com/jacksteroo/Pepper/issues/57).
- Companion reflection rubric: [docs/reflection-eval-rubric.md](reflection-eval-rubric.md) — same scoring shape, different concern.
- Notion: *Bringing Pepper Alive — Philosophical Foundation* §6 ("what alive decomposes into").

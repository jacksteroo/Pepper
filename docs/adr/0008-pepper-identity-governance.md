# ADR-0008: PEPPER_IDENTITY governance — propose-then-approve

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

`PEPPER_IDENTITY.md` is the artifact that holds Pepper's self-model: values, voice, things she finds boring, how she handles being wrong. Once the substrate work in Epic 04 (reflector) and Epic 05 (optimizer) lands, the identity doc has three plausible governance shapes, and Epic 06 (#49) cannot proceed on `data/pepper_identity.md` without picking one.

The three options were:

- **Option A — Jack-only.** Identity is hand-authored. Pepper never proposes changes. Reflector outputs do not feed it. Mechanically identical to `LIFE_CONTEXT.md`'s governance.
- **Option B — Pepper-only-after-seed.** Jack writes a seed, then the reflector mutates the file directly based on accumulated traces, with no per-change approval. Self-model evolves silently from experience.
- **Option C — Propose-then-approve.** Reflector writes proposed identity diffs to a queue. Jack approves, edits, or rejects each diff before it touches the file.

Forces that drove the decision:

- **Drift risk at the level of self.** The philosophical-foundation doc (Notion: *Bringing Pepper Alive* §5.2) explicitly warns about silent self-drift driven by hermes3-class hallucination — a known live hazard captured in memory (see `feedback_hermes3_hallucination_patterns.md`). Option B is precisely the failure mode that warning describes.
- **Mechanism reuse.** The Phase 6.7 pending-actions queue (`agent/pending_actions.py`) already exists and is currently under-exercised; identity diffs are a productive load.
- **No new mechanism for Option A.** Option A duplicates `LIFE_CONTEXT.md`'s governance and adds no new capability. It also forecloses on the substrate's reason to exist — the reflector cannot influence Pepper's self-model at all under Option A.
- **Sub-decision: split low- vs high-stakes self-observation.** A naive "everything goes through approval" creates Jack-as-bottleneck for ordinary self-noticing ("I tend to over-explain on technical topics"). Splitting the file into a committed self-model section (approval-gated) and an observational "Questions Pepper is asking about herself" section (free-write by reflector) resolves this without weakening the high-stakes path.

## Decision

Adopt **Option C — propose-then-approve**, with an explicit two-section split inside `data/pepper_identity.md`:

- `## Identity` — committed self-model. Approval-gated. Reflector writes proposed diffs to a `pending_identity_diffs` table; diffs surface in the existing pending-actions queue UI; Jack approves / edits / rejects each one. Approved diffs apply atomically and bump an `identity_version` counter.
- `## Questions Pepper is asking about herself` — observational. Reflector writes freely (append/replace). No approval gate. Bumps a separate `questions_version` counter. Reversible by clearing the section.

Both sections render into the system prompt on every turn. The Questions section is framed in the prompt as "things you are still working out about yourself" so the model treats it as exploratory rather than committed.

The optimizer (#46) is forbidden from optimizing either section. Identity is a sacred artifact, not a tunable prompt; this exclusion is enforced in the optimizer's selector list with a docstring citing this ADR.

Scope limits:

- This ADR governs `data/pepper_identity.md` only. `data/life_context.md` keeps its existing Jack-only governance.
- This ADR does not specify the seed content of the identity doc — that is product authoring work tracked in #52.
- This ADR does not specify the wire format of pending diffs or the UI surface — those land in #52 alongside the seed and prompt-wiring work.

## Consequences

**Positive.**

- Every change to the committed self-model is reviewable, diffable, and reversible. Hermes3-class hallucination at the level of identity cannot land silently.
- The pending-actions queue gains a second productive consumer beyond Phase 6.7's outbound-write drafts, exercising a surface that needed exercise.
- Low-stakes self-observation has a free-write path, so Jack is not the bottleneck for everyday "Pepper noticed something about herself" updates.
- The reflector gets a defined output channel into Pepper's self-model — the substrate's compounding-capability story (ADR-0002) connects all the way through to Pepper's voice.
- Optimizer's exclusion list is explicit, so the "compounding capability must be inspectable and reversible" rule from ADR-0002 holds at the identity layer.

**Negative.**

- Approval latency. A reflector-proposed identity diff cannot influence Pepper's behaviour until Jack acts on it. This is the deliberate trade — the hazard of silent self-drift outweighs the cost of approval delay.
- Two version counters (`identity_version`, `questions_version`) instead of one. Implementations must not conflate them. Tested explicitly in #52.
- Identity content goes into the system prompt on every turn, including frontier-routed turns. Identity is therefore exposed to the frontier model on those turns. This is the deliberate trade-off (Pepper needs her identity on every turn she runs as herself); documented in the file's header comment and in `docs/GUARDRAILS.md`.

**Neutral.**

- The pending-actions queue gains a new entry type (`identity_diff`); the in-memory store from Phase 6.7 is reused with a typed entry, not refactored.
- `pending_identity_diffs` is a new persistence surface but follows the same access controls as #24 (RAW_PERSONAL, local-only).

Follow-up work this decision creates:

- #52 — seed authoring, system-prompt wiring, `pending_identity_diffs` table, diff-application atomicity, gitignore rule for `data/pepper_identity.md`, optimizer exclusion entry.
- Cross-reference from `docs/GUARDRAILS.md` about the frontier-exposure trade-off in §2 (privacy boundaries).
- Cross-reference from `docs/ROADMAP.md` if the roadmap surfaces the inner-life work.

## Alternatives considered

- **Option A — Jack-only governance.** Rejected. Adds no new mechanism over `LIFE_CONTEXT.md`'s posture and forecloses on the reflector's ability to influence Pepper's self-model — which is the substrate's reason to exist.
- **Option B — Pepper-only-after-seed.** Rejected. Exactly the silent-self-drift failure mode the philosophical-foundation doc warns about, and the same hazard captured in the hermes3 hallucination memory. Even with a frontier-routed reflector, an approval gate is cheap insurance against the worst class of failure (Pepper quietly becomes someone else).
- **Single-section file with everything approval-gated.** Rejected as the sub-decision: makes Jack the bottleneck for ordinary self-noticing and weakens the value of the reflector for low-stakes observations. The two-section split keeps the high-stakes path strict while letting the low-stakes path flow.
- **Status quo — defer the decision.** Rejected. #52 cannot start without it; deferring blocks Epic 06.

## References

- Parent epic: [#49 — Inner Life Moves](https://github.com/jacksteroo/Pepper/issues/49).
- Issue: [#51 — PEPPER_IDENTITY.md governance](https://github.com/jacksteroo/Pepper/issues/51).
- Implementation: [#52 — seed authoring + system-prompt wiring](https://github.com/jacksteroo/Pepper/issues/52).
- Notion: [Bringing Pepper Alive — Philosophical Foundation §5.2](https://www.notion.so/jacksteroo/Bringing-Pepper-Alive-Philosophical-Foundation-353fb7367390818aba04fd6052f5e974).
- Notion: [OpenJarvis calibration — re-sequencing](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0).
- Notion: [Pepper identity document](https://app.notion.com/p/353fb7367390816eb614ec3ea74dc4ed).
- Anthropomorphic CAI — Wei et al, [arXiv:2503.04787](https://arxiv.org/abs/2503.04787).
- Prior ADR: [ADR-0002 — Compounding capability](0002-fifth-anchoring-principle-compounding-capability.md) (inspectable, reversible self-modification only).
- Prior ADR: [ADR-0007 — Optimizer framework (GEPA)](0007-optimizer-framework-gepa.md) (the optimizer this ADR excludes from the identity surface).

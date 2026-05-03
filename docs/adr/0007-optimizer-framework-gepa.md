# ADR-0007: Use GEPA as the prompt-optimization framework

- **Status:** Accepted
- **Date:** 2026-05-03

## Context

Epic 05 (#43) commits Pepper to a closed optimization loop: traces from #20–#22 feed a framework that produces candidate prompt versions, gated by the eval surfaces from #30 (retrieval), #42 (reflector rubric), and the existing pre-commit router gate. The framework choice is the open question that the rest of the epic depends on — #45's `runners.py`, #46's context-assembly target, and #47's intent-classification target all import the chosen library.

The OJ-calibration import for this epic instructs us to *"vendor patterns, not the framework"* — i.e. take the technique that OJ uses, but stay free to swap libraries. Three viable shapes were on the table:

- **(A) DSPy.** Mature, well-known, large community. Operates on `Signature` classes — strongly-typed input/output declarations that DSPy compiles into prompts. To use it, every optimization target must be re-expressed as a `Signature`. For the context-assembly selectors that #32 just extracted, this means another rewrite within weeks of the previous one.
- **(B) GEPA (Genetic-Pareto, Khattab et al, 2025).** What OJ actually uses. Operates directly on prompt strings through an evolutionary loop with a reflection model that explains failures. No `Signature` refactor. Smaller community than DSPy. Reflection-guided mutation aligns structurally with the Epic 04 reflector — they're complementary rather than competing.
- **(C) Lighter-weight in-house prompt evolution.** Hand-rolled mutation + scoring against the eval set. Cheapest to write today; no third-party dep. Reproduces the well-known weaknesses of naïve random search (no gradient signal, exponential search-space growth, no principled stopping criterion).

The choice has consequences beyond which `import` line `runners.py` carries:

- **Refactor budget.** E03 (#32) shipped two weeks ago. DSPy's `Signature` model would force re-expressing the selectors a second time, expanding E03's surface by another 2–3 days for no compounding gain. GEPA preserves the just-shipped shape.
- **Reflection coupling.** GEPA's "reflection model that explains failures" is structurally the same idea as the Epic 04 reflector — both consume failures and produce structured explanations. The same local-model class can serve both, which keeps the privacy invariant intact (no frontier API in the optimization inner loop) and reduces the total prompt-engineering surface.
- **Community / safety net.** DSPy's larger community is a real cost we accept. If GEPA hits a serious bug we have less of a safety net. Mitigations are in the Decision section.
- **Reversibility.** The OJ guidance (vendor the *pattern*) implies the framework choice should not be load-bearing. Pepper isolates the framework behind `agent/optimizer/runners.py` — the rest of the optimizer module imports from `runners`, not from the framework directly. Swapping GEPA for DSPy later is a single-file rewrite, not a system-wide one.

## Decision

The optimization framework is **GEPA** (Genetic-Pareto prompt optimization, Khattab et al, 2025).

Concrete operating model:

- `agent/optimizer/runners.py` is the single entry point that imports GEPA. Every other module under `agent/optimizer/` (datasets, storage, sanitizer, audit) is framework-agnostic.
- GEPA is pinned to a known-good version in `pyproject.toml`. Version bumps are gated by the eval-gate from #48, same as any prompt change.
- The reflection model GEPA uses to explain failures is the same local-model class used elsewhere in Pepper. No frontier-API call inside the inner optimization loop. This preserves the privacy invariant: trace content never leaves the machine during optimization.
- If GEPA hits a serious bug we cannot work around: capture a minimal repro, file upstream, and fork in-tree if the upstream timeline does not match Pepper's. The `runners.py` boundary makes a clean swap to DSPy a single-file rewrite of last resort.

This decision applies to every optimization target in Epic 05 (context assembly, intent classification, future targets). It does not apply to the eval gate (#48) which is framework-independent, or to candidate-prompt storage (#45's `storage.py`) which is intentionally a flat versioned-file layout.

## Consequences

**Positive.**

- E03's just-shipped context-assembly shape is preserved. No second rewrite of the selectors to satisfy a framework's type model.
- The reflection-guided mutation in GEPA aligns with the Epic 04 reflector. Both consume failures and produce explanations; sharing the local-model class reduces total prompt-engineering surface and keeps the privacy invariant honest.
- The `runners.py` isolation point keeps the framework choice reversible. Swapping it for DSPy later is one file, not a system rewrite.
- Optimization runs fully local on local traces, with the same local model class already in use. No new outbound data flow is introduced by adopting an optimizer.
- OJ's calibration thread already validated GEPA in a similar problem shape; we inherit that signal rather than re-discovering it.

**Negative.**

- Smaller community than DSPy. Fewer Stack Overflow answers, fewer downstream integrations, slower upstream bug fixes. Mitigated by the version pin, the in-tree fork plan, and the `runners.py` swap-out point.
- Less third-party tooling around GEPA (visualizers, tracing integrations) than around DSPy. Pepper's own audit log (#45) substitutes for this; we capture every optimizer run with seed, dataset hash, candidate scores, and accepted-or-not.
- The "evolutionary loop" cost profile is empirical, not theoretical. We don't yet know how many GEPA generations are needed to beat baseline on Pepper's eval set. #46 will produce the first data point; if cost is prohibitive, we bound generations rather than swap framework.
- Dependency surface grows by one library plus its transitive deps. Reviewed at pin time; tracked under the existing dep-update cadence.

**Neutral.**

- The trace store contract is unchanged. Optimizer reads from `traces` via the existing `pepper_traces_reader` role; no schema changes to support GEPA.
- Eval-gate (#48) thresholds are independent of framework choice. Switching to DSPy later would not change the gate.
- Versioned-prompt storage layout (#45 `storage.py`) is intentionally framework-agnostic. Candidate prompts are stored as flat strings with metadata, regardless of who produced them.

## Alternatives considered

- **(A) DSPy.** Mature, well-known, large community. Rejected. Its `Signature`-class model would force re-expressing the context-assembly selectors that #32 just extracted — adding 2–3 days of refactor that produces no compounding capability gain. Reconsidered as the fallback if GEPA proves unworkable; the `runners.py` boundary preserves this option.
- **(B) GEPA.** Selected — see Decision.
- **(C) Lighter-weight in-house prompt evolution.** Hand-rolled mutation + scoring loop. Rejected. Reproduces the well-known weaknesses of naïve random search: no principled stopping criterion, exponential search-space growth, no reflection signal to guide mutation. The "save a dependency" upside does not pay for re-discovering published technique.
- **Status quo: keep the hand-curated router exemplars and skip optimization.** Rejected. The whole point of Epic 05 is replacing hand-curated maintenance with trace-driven optimization. Skipping it leaves the system dependent on operator vigilance for every router regression.
- **Defer the framework choice and ship the eval gate first.** Rejected. The optimizer module (#45) cannot land without choosing what `runners.py` imports. Deferring re-creates the same blocker the Q4 question is meant to resolve.

## References

- Parent epic: [#43](https://github.com/jacksteroo/Pepper/issues/43).
- Source issue: [#44](https://github.com/jacksteroo/Pepper/issues/44).
- Implementing issues: [#45](https://github.com/jacksteroo/Pepper/issues/45) (optimizer module), [#46](https://github.com/jacksteroo/Pepper/issues/46) (context-assembly target), [#47](https://github.com/jacksteroo/Pepper/issues/47) (router target), [#48](https://github.com/jacksteroo/Pepper/issues/48) (eval gate).
- [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) — compounding-capability principle (versioned, inspectable, reversible self-improvement). This ADR ratifies an artifact-only optimization mechanism in line with that principle.
- [ADR-0005](0005-trace-schema.md) — the trace-store contract the optimizer reads from.
- [ADR-0006](0006-reflector-process-model.md) — reflector process model; GEPA's reflection-mutation step shares its local-model class with the reflector.
- Khattab et al, GEPA: Genetic-Pareto Prompt Optimization (2025).
- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — Open question Q4 (framework choice) and the "vendor patterns, not the framework" directive.

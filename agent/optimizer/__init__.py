"""Prompt-optimization module — Epic 05 (#43, #45).

This package owns the trace-driven prompt-optimization loop.

Module map:
- ``schema``     — dataclasses (CandidatePrompt, OptimizerRunRecord, TraceExample).
- ``datasets``   — pulls examples from the trace store, filtered by archetype/
                   prompt_version/window. Framework-agnostic.
- ``storage``    — versioned-file store for candidate prompts. Framework-agnostic.
- ``sanitizer``  — flags candidates that embed PII strings from
                   ``data/life_context.md``. Framework-agnostic.
- ``audit``      — append-only JSONL log of every optimizer run.
- ``runners``    — the *only* module that imports the framework (GEPA, per
                   ADR-0007). Other modules depend on the ``OptimizerRunner``
                   protocol it exposes — never on ``gepa`` directly.
- ``__main__``   — CLI entrypoint: ``python -m agent.optimizer optimize ...``.

Privacy invariant
-----------------
The optimizer reads local traces and runs the optimizer locally. It never
ships trace content to a frontier API in its inner loop (ADR-0007). All
candidate prompts pass through ``sanitizer`` before they are eligible for
promotion through the eval gate (#48).

Promotion (candidate → accepted) is the eval gate's concern (#48), not this
package's.
"""
from agent.optimizer.schema import (
    CandidatePrompt,
    OptimizerRunRecord,
    PromptStatus,
    TraceExample,
)

__all__ = [
    "CandidatePrompt",
    "OptimizerRunRecord",
    "PromptStatus",
    "TraceExample",
]

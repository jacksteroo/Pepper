"""Per-target optimizer-adapter registry.

Targets register their adapter factory + eval runner from their own
module at import time. The CLI looks up `--target` here to construct
the adapter for an `optimize` run; the eval gate looks up the runner
via ``agent.optimizer.eval_gate.EVAL_RUNNERS`` (which the adapter
modules also populate).

Two-step API:

- ``register_adapter(target, factory)`` — for the optimization side.
- ``eval_gate.register_runner(target, runner)`` — for the gate.

Both are called from each adapter module's import.

To make sure the registries are populated before the CLI consults
them, import this package's ``__init__`` triggers a side-effect
import of every shipped adapter module (currently just
``context_assembly``). New target modules need a corresponding line
below.
"""
from __future__ import annotations

from collections.abc import Callable

from agent.optimizer.runners import OptimizerAdapter

AdapterFactory = Callable[[], OptimizerAdapter]
"""A zero-arg callable returning a fresh adapter instance."""

ADAPTERS: dict[str, AdapterFactory] = {}


def register_adapter(target: str, factory: AdapterFactory) -> None:
    """Register or replace an adapter factory for ``target``.

    Idempotent — re-registration replaces the previous factory.
    """
    ADAPTERS[target] = factory


def get_adapter(target: str) -> OptimizerAdapter:
    """Resolve the adapter for ``target``.

    Raises KeyError if no factory is registered. Callers handle the
    error and surface a clear message so the operator knows which
    target needs an adapter module.
    """
    if target not in ADAPTERS:
        raise KeyError(
            f"no adapter registered for target {target!r}. "
            f"Known targets: {sorted(ADAPTERS)!r}. "
            "Targets register from their own module's import-time call to "
            "agent.optimizer.adapters.register_adapter().",
        )
    return ADAPTERS[target]()


# ── Side-effect imports ──────────────────────────────────────────────────────
# Each adapter module registers itself at import time. Adding a new target
# adds one line here.

from agent.optimizer.adapters import context_assembly  # noqa: E402, F401 — registers on import

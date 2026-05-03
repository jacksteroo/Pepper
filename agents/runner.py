"""Generic entrypoint for agent processes.

Per ADR-0006, each cognitive archetype runs as its own OS process. This
module is the single CLI entrypoint every process boots from:

    python -m agents.runner --archetype reflector

It loads the named archetype from `agents/<name>/main.py`, configures
shared logging, installs SIGTERM/SIGINT handlers for graceful
shutdown, and then awaits the archetype's `run(config)` coroutine.

The archetype implementations are imported lazily — referencing an
archetype that does not yet exist (e.g. `monitor` in the substrate
phase) returns a clear "not yet implemented" error rather than an
ImportError stack.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import signal
import sys
from typing import Awaitable, Callable, Protocol

import structlog

from agents._shared.config import AgentRuntimeConfig, load_runtime_config
from agents._shared.logging import configure_logging

logger = structlog.get_logger(__name__)

# Archetypes whose main.py is allowed to be discovered. Per ADR-0006:
#   - reflector lands in #39
#   - monitor + researcher are listed but not yet implemented
# Adding a new archetype means adding the directory `agents/<name>/`
# with a `main.py:run(config)` async entrypoint AND adding the name
# here. The allow-list is the operational analogue of ADR-0004's
# enumeration of inhabitants.
_KNOWN_ARCHETYPES: tuple[str, ...] = ("reflector", "monitor", "researcher")


class _AgentMain(Protocol):
    """Shape every archetype's `main.py` must conform to."""

    async def run(self, config: AgentRuntimeConfig) -> None: ...


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="agents.runner",
        description="Run a Pepper cognitive archetype as a long-lived process.",
    )
    parser.add_argument(
        "--archetype",
        required=True,
        choices=_KNOWN_ARCHETYPES,
        help="Which archetype to run. Must match a directory under agents/.",
    )
    return parser.parse_args(argv)


def _load_archetype_run(name: str) -> Callable[[AgentRuntimeConfig], Awaitable[None]]:
    """Import `agents.<name>.main:run` and return the coroutine fn.

    Raises `RuntimeError` with a clear message ONLY when the archetype
    module itself is missing. If the archetype module exists but a
    transitive import fails (a real bug in the archetype), the
    `ModuleNotFoundError` is re-raised unchanged so the dev-loop sees
    the real cause instead of a misleading "not implemented".
    """
    module_name = f"agents.{name}.main"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        # Only treat as "not implemented" when it's THIS module that's
        # missing (or its parent package). A transitive failure inside
        # the archetype keeps `exc.name` pointing at the true missing
        # dep — let it propagate so the operator sees the real error.
        missing = getattr(exc, "name", None)
        owns_failure = missing in {module_name, f"agents.{name}"}
        if owns_failure:
            raise RuntimeError(
                f"archetype {name!r} is not yet implemented "
                f"(expected module {module_name}; ADR-0006)"
            ) from exc
        raise

    run = getattr(module, "run", None)
    if not callable(run):
        raise RuntimeError(
            f"archetype {name!r} module {module_name} does not expose run(config)"
        )
    return run  # type: ignore[return-value]


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / some CI environments — fall back to default.
            signal.signal(sig, lambda *_: stop.set())


async def _run(name: str) -> int:
    config = load_runtime_config(archetype=name)
    configure_logging(level=config.log_level, archetype=name)

    logger.info("agent_starting", archetype=name)

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    try:
        run_fn = _load_archetype_run(name)
    except RuntimeError as exc:
        logger.error("agent_not_implemented", archetype=name, reason=str(exc))
        return 2

    archetype_task = asyncio.create_task(run_fn(config), name=f"agent-{name}")
    stop_task = asyncio.create_task(stop.wait(), name="signal-stop")

    done, pending = await asyncio.wait(
        {archetype_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if stop_task in done and not archetype_task.done():
        logger.info("agent_shutdown_signal", archetype=name)
        archetype_task.cancel()
        try:
            await archetype_task
        except asyncio.CancelledError:
            pass

    for t in pending:
        t.cancel()

    if archetype_task.done() and not archetype_task.cancelled():
        exc = archetype_task.exception()
        if exc is not None:
            logger.error("agent_crashed", archetype=name, error=str(exc))
            raise exc

    logger.info("agent_stopped", archetype=name)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_run(args.archetype))


if __name__ == "__main__":
    raise SystemExit(main())

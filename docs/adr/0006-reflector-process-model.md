# ADR-0006: Reflector and other agents run as separate processes per archetype

- **Status:** Accepted
- **Date:** 2026-05-02

## Context

ADR-0004 introduced `agents/` as the structural home for cognitive specialists — long-running processes that consume traces and produce reflections, compressions, summaries, or research outputs. The directory and isolation rule are settled; the operating shape of those processes is not.

Three viable shapes were on the table once substrate work begins:

- **(A)** APScheduler job inside Pepper Core — the reflector is a function the existing scheduler invokes nightly, in-process.
- **(B)** Daemon thread inside Pepper Core — the reflector runs as a long-lived thread alongside the orchestrator.
- **(C)** Separate process per archetype — `agents-reflector`, `agents-monitor` (later), `agents-researcher` (later) each run as their own OS process / docker service, triggered by a thin shim from APScheduler in core.

The choice has consequences that the directory rule alone does not settle:

- **Crash isolation.** Once reflection is consuming Claude tokens overnight, an OOM or unhandled exception in the reflector should not take Pepper Core down. Options A and B share an interpreter with core; C does not.
- **Memory and dependency surface.** Reflection prompts, embedding caches, and trace batches will grow. Holding them inside core's interpreter inflates the orchestrator's resident memory and couples its restart-cost to the reflector's working set.
- **Cognitive specialisation.** ADR-0004 ratified that reflector, monitor, and (eventually) researcher are distinct cognitive functions with different cadences and prompt windows. Running them inside core re-creates the "single orchestrator stuffed with everything" shape that ADR-0004 explicitly rejects — one interpreter, one memory window, one process tree to debug. The structural rule says they are separate; the operational form should match.
- **Deployability.** Pepper deploys via docker-compose. A separate service per archetype is the idiomatic shape there; the alternative is one service that mixes orchestration and cognition in the same container.
- **Trigger flexibility.** APScheduler is the right place to decide *when* a reflection happens (it already owns the cadence vocabulary for Pepper). It is the wrong place to *do* the reflection — that conflates scheduling with execution.

The structural argument therefore lines up with the operational argument: the directory says these are separate cognitive functions, and the runtime should not re-collapse them.

## Decision

Each agent under `agents/` runs as its own OS process. The substrate phase ships one such process — `agents-reflector` — and the same shape applies to `agents-monitor` and `agents-researcher` when they land.

Concrete operating model:

- A generic `agents/runner.py` entrypoint takes `--archetype <name>` and runs that archetype's main loop. One process per archetype: `agents-reflector`, later `agents-monitor`, later `agents-researcher`.
- `docker-compose.yml` adds one service per archetype with `restart: unless-stopped`. Pepper Core stays a separate service.
- APScheduler in core remains the trigger mechanism. It does not invoke reflection logic directly. It fires a thin shim that signals the target archetype's process to run a pass.
- The shim uses Postgres `LISTEN/NOTIFY` on a per-archetype channel (`reflector_trigger`, `monitor_trigger`, …). Postgres is already the substrate's source of truth and the channel is cheap; no additional broker is added. A UNIX socket was considered and rejected — it adds a second IPC path that exists only for this purpose, and breaks once any agent runs on a different host.
- Each agent reads traces read-only (under the `pepper_traces_reader` role from ADR-0005) and writes its own outputs (reflections, alerts, summaries) into its own table. The first such table — `reflections` — is defined in #39's schema work; ADR-0005 does not pre-ratify it. No shared in-process state with core.
- Signal handling and graceful shutdown live in `agents/runner.py` so every archetype gets the same shutdown contract.

This decision applies to every cognitive archetype under `agents/`. It does not apply to `agent/` (the orchestrator) or to `subsystems/` (capability boundaries) — those keep their existing operating shape.

The runner entrypoint, the per-archetype docker service definitions, and the lint check that enforces the boundary land in #38 (scaffolding). The first inhabitant — `agents-reflector` — lands in #39.

## Consequences

**Positive.**

- A reflector OOM or crash leaves Pepper Core running. Crash blast radius is bounded to the archetype that failed.
- Pepper Core's resident memory is independent of reflection working sets; restart cost stays small.
- The directory's structural separation is mirrored at runtime. ADR-0004's commitment that each archetype runs on its own cadence with its own prompt and memory window stops being aspirational.
- Adding the next archetype is mechanical: new `agents/<name>/` module, new docker-compose service, new NOTIFY channel. No core-process surgery.
- Each archetype's logs, metrics, and resource usage are observable in isolation (one container per archetype).

**Negative.**

- One more service per archetype in `docker-compose.yml`. The substrate phase grows by one container today and by two more as monitor and researcher land.
- IPC adds an indirection: scheduler fires NOTIFY, agent process LISTENs and runs. A bug in the shim is harder to trace than a function call in-process. Mitigation: the shim is intentionally thin (a few lines per archetype) and the LISTEN/NOTIFY contract is single-channel, single-payload.
- Dev loop gets slightly heavier — `docker-compose up` now spins up Pepper Core plus the agents.
- Process supervision is on docker now, not on APScheduler. We rely on `restart: unless-stopped` to bring an agent back after a crash; this is fine for nightly cadences. Tripwire: if any archetype's cadence rises above one trigger per minute, revisit supervision (a fast crash-restart loop can outpace docker's default restart backoff).
- The shared utility plumbing in `agents/_shared/` (logging, db, config) carries more weight: it is the only common runtime surface across processes, so any shared utility regression hits every archetype simultaneously. ADR-0004's three `_shared/` safeguards apply.

**Neutral.**

- Pepper Core's APScheduler keeps owning *when* things happen. Nothing about cadence vocabulary changes.
- The trace store remains the canonical communication channel between core and agents (read-only from the agents' side). No shared in-memory queues, no in-process callbacks.
- This decision does not pre-commit to any particular language for future agents — the contract is "process that LISTENs on a Postgres channel and writes to Postgres", which is portable.

## Alternatives considered

- **(A) APScheduler job inside Pepper Core.** The reflector is registered as a job and invoked in-process. Rejected. It collapses the structural separation ADR-0004 just established back into the orchestrator. A reflector exception would surface inside core's interpreter, and reflection's working set would inflate core's resident memory. The single-orchestrator shape is exactly what the OJ-calibration thread documents as failing once cognitive specialists land.
- **(B) Daemon thread inside Pepper Core.** A long-lived thread alongside the orchestrator, woken by an in-process queue. Rejected. Same crash-isolation and memory-coupling failure modes as (A), with the added downside that Python threads share the GIL with core's request loop — any CPU-bound reflection work directly slows orchestrator latency.
- **Status quo: defer the decision until the first cognitive process is built.** Rejected. The trigger mechanism, the docker-compose layout, and the runner entrypoint all need to exist before #38's scaffolding can land. Building the reflector first and then deciding how it runs forces churn (rename services, rewire scheduling, re-do lint).
- **One process running multiple archetypes via threads or asyncio tasks.** Rejected. Brings back the single-interpreter coupling that this ADR is rejecting in (A) and (B), with the added complexity of intra-process scheduling between archetypes that have explicitly different cadences. The cost saving (one container instead of N) does not pay for the loss of isolation.
- **UNIX socket between core and each agent for the trigger.** Rejected as the trigger transport. It adds a second IPC mechanism that only exists for this purpose, requires a socket-path convention, and breaks the moment any agent runs on a different host. Postgres `LISTEN/NOTIFY` is already available, already authenticated, already supports multiple subscribers, and travels across hosts unchanged.
- **External job queue (Celery, RQ, Sidekiq-equivalent).** Rejected for the substrate phase. It would solve the trigger problem but at the cost of adding a broker (Redis or RabbitMQ) that is otherwise not needed in the stack. The Postgres-based trigger is enough until cadence or fan-out demand changes.
- **Host-level supervision (systemd or launchd unit per archetype) instead of docker-compose services.** Rejected for now. Pepper's substrate phase already runs core, Postgres, and the WhatsApp bridge under `docker-compose.yml`; co-locating the agents there keeps one supervision contract for all long-running components. A non-Docker distribution would reopen this question — if Pepper ships a launchd-native variant for single-user installs, revisit whether agents should run as launchd `LaunchAgents` instead. The runner entrypoint is process-supervisor agnostic, so the choice is reversible.

## References

- [ADR-0001](0001-resequence-around-oj-calibration.md) — substrate phase deliverables.
- [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) — compounding-capability principle.
- [ADR-0004](0004-introduce-agents-directory.md) — `agents/` directory and isolation rule that this ADR's processes inhabit.
- [ADR-0005](0005-trace-schema.md) — trace store and Postgres roles; the read-only contract that agent processes operate under.
- Source issue: [#37](https://github.com/jacksteroo/Pepper/issues/37).
- Parent epic: [#36](https://github.com/jacksteroo/Pepper/issues/36).
- Implementing issues: #38 (scaffolding), #39 (first inhabitant: `agents-reflector`).
- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) — §"Open questions" Q3.
- Generative Agents (Park et al, arXiv:2304.03442) — reflection as a separate cognitive process pattern.

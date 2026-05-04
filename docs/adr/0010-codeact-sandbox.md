# ADR-0010: CodeAct sandbox — containerized subprocess on the server

- **Status:** Proposed
- **Date:** 2026-05-03

## Context

CodeAct is the planned escape hatch for the hardest queries — the model emits Python, the system executes it, the result re-enters the conversation. The default tool-calling path stays JSON; CodeAct opens only for deep-reasoning paths where stitched JSON tools fall short. Issue #61 captures the integration plan; Q6 in the [OpenJarvis calibration thread](https://www.notion.so/354fb736739081ae8834eb6be2d361c0) captured the open question of *how* the model's code should run.

Three forces have changed since CodeAct was first sketched:

- **Deployment posture flipped from "Mac desktop app" to "thin clients on a Tailscale-accessible server"** ([ADR-0011](0011-layer-3-thin-clients-tailscale.md), 2026-05-02). The execution host is now a Linux server, not the operator's MacBook. The macOS dev-vs-prod divergence that made the original subprocess sketch painful disappears: namespaces, seccomp, cgroups, and read-only root filesystems are available natively, in production, by default.
- **Container topology already exists.** The server already ships as `docker-compose.yml`; image build, network isolation, and resource limits are tooling we already own and run. A CodeAct sandbox slots into that topology rather than introducing a parallel one.
- **Wasm Component Model isn't ready.** componentize-py (Python via Wasm Component Model) is the prettier capability-based endgame, but cold-start and host-bridge tooling are still maturing. Holding CodeAct on it would defer the escape hatch indefinitely.

CodeAct is the highest-stakes security item in the roadmap: model-generated code is, by construction, untrusted. The sandbox decision must be conservative by default and migrate cleanly when a stronger primitive lands. This ADR captures the *shape* of execution; the integration mechanics, threat model, and rollout gate live with #61.

## Decision

CodeAct runs in a **containerized subprocess on the Pepper server**. Each invocation executes inside a Docker container that is hardened by configuration, not by the model's good behaviour.

The default invocation pattern is **ephemeral**: `docker run --rm` per invocation, ~500ms cold start, fresh state every time. A long-lived sandbox container with `docker exec` (~50ms per call) is permitted as an optimization once latency is shown to hurt, but only if state-cleanup discipline between invocations is verified by tests. v0 ships ephemeral.

Container properties — declared in compose / run flags, not in code paths:

- `--network none` by default. An optional dedicated Docker network is attached only when the invocation needs the tool bridge (see below).
- `--read-only` root filesystem; `--tmpfs /scratch:size=64M` for writable scratch.
- No host filesystem mounts.
- Non-root user (`USER 1001` in the Dockerfile).
- `--memory=512m --cpus=1.0` resource limits.
- Wall-clock timeout enforced by the host, not the sandbox: the host process kills the container after N seconds.
- A Linux seccomp profile blocking dangerous syscalls (kernel module load, `ptrace`, `mount`).

The sandbox image is minimal Python: no shell utilities, no compilers, no `curl` / `wget` / `nc`. The smaller the surface, the less the model can splice together.

Bridge for callbacks: a small HTTP server on the dedicated Docker network exposing **only an allowlisted subset of Pepper tools**. Bridge auth uses a per-session shared secret rotated per invocation. Bridge endpoints are unreachable from outside the dedicated Docker network.

Mobile and desktop clients never execute CodeAct. Clients render the result text in the conversation thread, no different from any other tool result. CodeAct is server-side only.

The kill-switch (`PEPPER_CODEACT_ENABLED`) and the rollout sequencing belong with #61, not this ADR. This ADR ratifies the execution shape; the integration ADRs and PR land separately.

## Anchoring principles — preserved

- **Privacy-first.** Privacy here rests on the **composition of the bridge allowlist** — the prevention boundary — not on the audit log, which is detection. The allowlist starts minimal and is reviewed per addition; it must not expose tools that return raw personal data without redaction at the bridge boundary. Bridge invocations are additionally audited per call (trace_id, code, result, bridge tool calls) so any crossing is recoverable from traces, but the audit is a backstop, not the primary control.
- **Local-first / sovereign.** Execution stays on operator-owned infrastructure (home server primary, operator-owned VPS fallback per [ADR-0011](0011-layer-3-thin-clients-tailscale.md)). No managed code-execution service is in the path. The sandbox image is built locally from a Dockerfile in this repo; any external base-image dependency is a registry the operator pins explicitly. The image build and pin policy are part of the deployment artifact, not assumed away.
- **Additive memory.** Sandbox state is ephemeral by construction; the trace store records the invocation, not the in-process state. CodeAct does not introduce a new persistence surface.
- **Pluggable subsystems.** The sandbox does not pierce subsystem boundaries. The bridge allowlist is the only path from sandbox into Pepper, and it consumes the same tool interfaces as `agent/core.py` does.
- **Compounding capability** ([ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md)). The artifacts that govern sandbox behaviour — Dockerfile, seccomp profile, bridge allowlist — are plaintext under version control and edited by diff. The *built image* contains binary layers that are not auditable by hand; auditability is preserved by keeping the image source minimal (no compiled-from-elsewhere dependencies) and by treating the registry pin and base-image hash as part of the diffable artifact set.

## Consequences

**Positive.**

- Shippable now. Docker, seccomp, and cgroups are stable, deployed, and well-understood. CodeAct stops being a multi-quarter Wasm dependency.
- Defense in depth by configuration. Network, filesystem, user, memory, CPU, wall-clock, and syscall isolation are all expressed as flags or profiles. *Within* the sandbox, the model has no path to subvert them — they are not code branches it can trigger. They do not, however, defend against a kernel-level escape; an escape invalidates every flag at once. See the negative-consequences entry below.
- Aligned with the existing deployment shape. The sandbox is one more compose service alongside `web`, `api`, and the Postgres + pgvector stack.
- Migrates cleanly. The abstraction CodeAct exposes upward is "give the model code, get back a result." Re-pointing that at wasmtime once componentize-py matures is a swap of the runner, not a rewrite of the call site.

**Negative.**

- Docker-on-Docker in CI. Sandbox isolation tests need to run against a real Docker daemon (or docker-in-docker). CI configuration grows; build minutes increase.
- ~500ms cold start per invocation. The escape hatch is slower than a JSON tool call. Acceptable for the "deep reasoning" path it serves, but means CodeAct cannot be used as a hot path.
- Container escapes are the load-bearing residual risk. Containers provide isolation, not a security boundary as strong as a VM or a Wasm sandbox; a kernel CVE that grants escape collapses the entire isolation model in one step (network, filesystem, syscall, user — all of it). The threat model in #61 has to enumerate this honestly, and the kill-switch + soak window in #61 exist precisely because of it. If #61's threat model concludes this shape cannot be hardened to acceptable safety, this ADR is superseded rather than amended.
- A second runtime to maintain. Image, compose service, bridge HTTP server, seccomp profile, and the tool-allowlist registry all become live artifacts requiring upgrades, vulnerability tracking, and tests.

**Neutral.**

- Subsystem boundaries are unaffected. The sandbox is an `agents/codeact/` archetype per [ADR-0004](0004-introduce-agents-directory.md); subsystems neither know about it nor import from it.
- The trace store and reflection runtime are unchanged. CodeAct invocations emit the same trace shape (per [ADR-0005](0005-trace-schema.md)) as any other tool call, with the model-generated code captured in the trace payload.
- Existing JSON tool path is untouched. CodeAct is a sibling, not a replacement.

**Follow-up work this ADR creates.**

- Issue #61 (CodeAct integration as escape hatch) consumes this ADR. The threat-model document, the `agents/codeact/` archetype, the sandbox Dockerfile, the bridge HTTP server, and the kill-switch all land there.
- The bridge allowlist is a versioned artifact in its own right. It needs a single source of truth (likely a constant module under `agents/codeact/`) so that additions are reviewable as a diff.
- `docs/ROADMAP.md` carries the Layer 2 / inner-life sequencing today; once #61 is past the soak window referenced in its acceptance criteria, the roadmap entry that mentions CodeAct as deferred should reference this ADR for the *how*.
- Re-evaluate wasmtime when componentize-py declares a stable host-bridge story. The migration path is a swap of runner under the same `code_act(plan: str)` interface.

## Alternatives considered

- **Status quo: no CodeAct.** Rejected — the escape hatch isn't optional in the long run. Hard queries that require multi-step calculation, ad-hoc data manipulation, or composition the JSON tool grammar cannot express are the cases where Pepper fails most visibly. Indefinite deferral keeps the gap open without a closing condition.
- **In-process Python execution (e.g., RestrictedPython, AST-walked sandbox).** Rejected — every published in-process Python sandbox has been broken. Sharing an interpreter with the host is a structurally weak boundary for untrusted code; a single missed bypass (gadget, descriptor abuse, GC trick) compromises the whole agent. Not a serious option for the highest-stakes security item in the roadmap.
- **Plain subprocess (no container) on the host.** Rejected — pre-flip from desktop to server, this had the additional pain of macOS dev vs Linux prod divergence; even on Linux, getting filesystem isolation, network isolation, and resource limits right by composing `unshare` / `setrlimit` / `seccomp` by hand is strictly worse than letting the container runtime do it. We would be reinventing the parts of `docker run` we already use.
- **wasmtime + componentize-py for a capability-based sandbox.** Deferred, not rejected — capability-based isolation is the prettier endgame and is on the roadmap to reconsider once componentize-py declares a stable host-bridge story. Holding CodeAct on it today would defer the escape hatch indefinitely for ergonomic gains that don't outweigh the security gains we already get from a hardened container.
- **Managed code-execution service (e.g., a third-party "run untrusted Python" API).** Rejected — incompatible with the privacy-first and sovereignty principles. A platform operator with root on the execution host would have access to whatever the bridge exposes from Pepper into the sandbox; that defeats the boundary the bridge exists to enforce.
- **Long-lived `docker exec` sandbox container as v0 default.** Rejected for v0 — the latency win (~50ms vs ~500ms) is real, but v0 needs the simplest reasoning about state. Ephemeral containers are clean by construction; long-lived sandboxes need a verified cleanup story between invocations before they are safe to default to. Permitted as an optimization once that story is tested.

## References

- Issue #60 — CodeAct sandbox decision (this ADR's source).
- Issue #61 — CodeAct integration as escape hatch for hardest queries (consumer of this ADR).
- Issue #58 — Epic 07: Deferred — CodeAct & Subsystem Expansion (parent epic).
- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/354fb736739081ae8834eb6be2d361c0) — Q6 open question; mobile / server architecture confirmation 2026-05-02.
- [Agent Pepper hub](https://www.notion.so/353fb7367390806a88addf0430118d34).
- Related: [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) (compounding capability — diffable artifacts), [ADR-0004](0004-introduce-agents-directory.md) (`agents/` directory), [ADR-0005](0005-trace-schema.md) (trace schema for invocation logging), [ADR-0011](0011-layer-3-thin-clients-tailscale.md) (Layer 3 server-heavy posture).

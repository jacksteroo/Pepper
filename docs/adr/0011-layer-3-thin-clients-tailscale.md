# ADR-0011: Layer 3 — thin clients on a Tailscale-accessible server

- **Status:** Proposed
- **Date:** 2026-05-02

## Context

Layer 3 (Presentation) was previously framed as a notarized macOS desktop app that bundled the Pepper backend, an embedded PostgreSQL + pgvector, and the React UI into a single self-contained `.app` running on the operator's MacBook. That framing made two assumptions that no longer hold:

- **Single-device deployment.** Pepper is the operator's life assistant, not a desktop app. The operator needs Pepper from anywhere — phone on the move, laptop at a desk, future ambient surfaces — without a different agent on each device. A self-contained desktop app forces a synthesis problem (which device holds the canonical state? which one runs the agent?) that no version of "embedded Postgres on the desktop" answers cleanly.
- **No mobile story.** The original plan deferred mobile indefinitely. Mobile is the surface where push-notification-driven, proactive behaviour is most valuable, and treating it as a v2 concern guaranteed Pepper would never be where it is needed most.

The Q6 deep-walk in the [OpenJarvis calibration thread](https://www.notion.so/354fb736739081ae8834eb6be2d361c0) surfaced the same observation from a different angle: the right deployment shape for a single-operator, privacy-first agent is **server-heavy + thin clients**, with the network boundary held by a private mesh rather than a public endpoint. The detailed framing lives in the [Layer 3 redesign Notion thread](https://www.notion.so/354fb736739081279492cbaf25aa69b8).

The four anchoring privacy / sovereignty / additive-memory / pluggable-subsystem principles, plus the fifth (compounding capability, [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md)), constrain what shape that posture can take. This ADR ratifies the constraints and the headline shape; the sub-decisions land as sibling ADRs (0012–0016).

## Decision

Layer 3 is re-scoped as **thin clients on a Tailscale-accessible Pepper server**. The headline decisions are:

1. **Server-client protocol: FastAPI + long polling.** The same FastAPI orchestrator that already serves the local web UI becomes the canonical client surface. Long polling is preferred over WebSocket for resilience over flaky mobile connections and simpler reconnection semantics. Wire-level details land in ADR-0012.
2. **Network boundary: Tailscale.** Mobile, desktop, and web clients reach the server over a private tailnet. The server has **no internet-exposed endpoint by default**. Tailscale device identity is the basis of authentication; auth specifics land in ADR-0014.
3. **Clients: macOS desktop and mobile, both first-class thin clients.** The macOS shell remains a Swift / WKWebView wrapper around the existing React UI, with the embedded-PostgreSQL plan from `docs/MACOS_DESKTOP_APP_PLAN.md` retired. The mobile platform decision (Capacitor v0 wrapping the existing React UI vs. native vs. React Native) lands in ADR-0015. Voice and ambient surfaces remain on the wishlist.
4. **Hosting posture: home server primary, operator-owned VPS as fallback.** The home server is the default deployment target — on-premises, Tailscale-accessible, no third party in the data path. A VPS path exists as an explicit escape hatch for travel scenarios, **only when the VPS is operator-owned** (e.g., a Hetzner box Jack rents). Managed / serverless hosts (Vercel, Fly.io) are ruled out because the platform operator would have root on a host that holds raw personal data. Sovereignty interpretation lands in ADR-0016; server hardening lands in ADR-0013.

This ADR is the master decision; the six sibling ADRs (0011 through 0016) and the Epic 08 implementation sub-issues hang off it. The scope of this ADR is the **shape**, not the mechanism — concrete protocol fields, hardening checklists, auth tokens, and platform code all live in the sibling ADRs and downstream PRs.

## Anchoring principles — preserved

- **Privacy-first.** Raw personal data (email, messages, health, finance) stays on the server. Clients render and capture input; they do not store raw archives. The Tailscale boundary keeps the data path off the public internet.
- **Local-first / sovereign.** Home server is the default; no managed cloud is in the data path. The VPS fallback is permitted only when operator-owned, so "no mandatory cloud services" is preserved (the VPS is still the operator's machine, just rented).
- **Additive memory.** Memory and traces remain server-side, untouched by this decision. Clients are stateless render surfaces.
- **Pluggable subsystems.** Subsystems do not move; they remain server-side and remain isolated from each other and from `agent/core.py`. The protocol is between clients and the server; it does not pierce subsystem boundaries.
- **Compounding capability** ([ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md)). The trace store, reflection runtime, and optimizer all run server-side. This decision puts more usage signal through a single canonical surface, which makes the trace stream richer, not poorer.

## Consequences

**Positive.**

- One canonical agent runtime, multiple thin client surfaces. The synthesis problem ("which device is canonical?") is resolved by construction: the server is canonical.
- Mobile becomes tractable. Push-notification-driven proactive behaviour — which is where Pepper's morning-brief / commitment-tracking value compounds — gets a real surface.
- The Tailscale boundary is a stronger privacy invariant than localhost-bind. The current "[localhost](http://localhost)-bind enforces privacy" assumption (e.g., issue #34) is replaced by "tailnet membership + ACLs enforce privacy," which is verifiable independently of the operator's network configuration.
- The `docker-compose.yml` deployment becomes the production shape, not a dev convenience. Fewer moving parts; one canonical topology.
- All client surfaces consume the same FastAPI endpoints. New clients (voice, ambient) are additive; they don't fork the agent.

**Negative.**

- A server is now load-bearing. "Pepper works when the MacBook is closed" is no longer trivially true. Home-server uptime, Tailscale availability, and the VPS fallback path all become real concerns.
- Mobile + desktop client work that was previously "v2" is now in the critical path for the next platform direction. ADR-0015 needs to land before any mobile code ships.
- The earlier embedded-PostgreSQL macOS plan in [`docs/MACOS_DESKTOP_APP_PLAN.md`](../MACOS_DESKTOP_APP_PLAN.md) is retired. The Swift / WKWebView shell survives in the new posture, but the bundled-Postgres design and the auth flows that assumed local-only state need to be reframed against ADR-0014.
- Server hardening expectations rise. A long-running, network-reachable Pepper is a different threat surface than a desktop app that only listens on `127.0.0.1`. ADR-0013 captures the concrete hardening checklist.

**Neutral.**

- Subsystem boundaries are unaffected. This decision reshapes Layer 3, not Layers 1 or 2.
- The active-surface ranking from [ADR-0003](0003-layer-2-is-the-active-surface.md) (Layer 2 first) is unchanged. Layer 3 work tracked under Epic 08 runs in parallel with substrate work, but does not pre-empt it.
- The `agents/` directory introduced by [ADR-0004](0004-introduce-agents-directory.md) is unaffected. Agents remain server-side processes, isolated from each other.

**Follow-up work this ADR creates.**

- ADR-0012 (long polling protocol design), ADR-0013 (server hardening checklist), ADR-0014 (auth: Tailscale device identity + per-device PIN), ADR-0015 (mobile platform decision), ADR-0016 (sovereignty interpretation: VPS-as-fallback only with operator-owned VPS) — all required before the corresponding Epic 08 sub-issues can ship.
- The "[localhost](http://localhost)-bind enforces privacy" assumption in any in-flight UI / panel work needs to be migrated to a "Tailscale-bound + ACLs enforce privacy" assumption (tracked as a sub-issue under Epic 08).
- The retirement of the embedded-PostgreSQL desktop direction propagates to `README.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, and `docs/MACOS_DESKTOP_APP_PLAN.md` — those updates land in this PR alongside the ADR.

## Alternatives considered

- **Status quo: keep the notarized macOS desktop app with embedded PostgreSQL.** Rejected — it answers "where does Pepper live on the operator's primary laptop?" but not "where does Pepper live for the operator?" Mobile is a year away under this plan, and synthesizing state across a phone and a desktop with embedded Postgres is a problem the plan does not solve.
- **Self-hosted server with a public HTTPS endpoint instead of Tailscale.** Rejected — adds the operational cost of running a public service (TLS rotation, abuse handling, fail2ban, public-IP exposure) for no privacy gain. Tailscale gives mutual device authentication and a private network boundary with negligible operational overhead.
- **WebSocket instead of long polling.** Rejected for v0 — long polling tolerates flaky mobile networks, NAT timeouts, and aggressive radio-power-down behaviour without complex reconnection logic. WebSocket can be revisited as an optimization once the long-polling path is in production and we have telemetry on real reconnection patterns.
- **Managed-cloud fallback (Vercel / Fly.io / similar) for the VPS path.** Rejected — these put a third-party platform operator in root position on a host that holds raw personal data. Incompatible with the privacy / sovereignty principles. ADR-0016 ratifies this restriction explicitly.
- **Mobile via React Native (or Swift + Kotlin native) for v0.** Deferred to ADR-0015. The leading recommendation under that ADR is **Capacitor v0** (a thin native wrapper around the existing React UI) to ship on the existing surface, with native re-platforming revisited only after 2–3 months of usage data justifies the doubled implementation cost.
- **Defer Layer 3 work entirely until Q4 2026.** Rejected — the substrate-first sequencing in [ADR-0001](0001-resequence-around-oj-calibration.md) already pushes Knowledge / Health / Finance to Q4. Pushing Layer 3 with them would compound the "no mobile story" gap into a third quarter, and Layer 3 work does not contend with the substrate effort because the decisions sit in different files and surfaces.

## References

- [Layer 3 redesign — mobile thin clients on a Tailscale-accessible server](https://www.notion.so/354fb736739081279492cbaf25aa69b8) — the Notion thread that consolidated the headline decisions and open sub-questions this ADR ratifies.
- [OpenJarvis calibration — lessons, challenges, shortest path](https://www.notion.so/354fb736739081ae8834eb6be2d361c0) — Q6 deep-walk that surfaced the thin-client + server-heavy posture.
- [Agent Pepper hub](https://www.notion.so/353fb7367390806a88addf0430118d34) — Progress > "Next platform" row.
- Epic 08: Layer 3 — Mobile + Desktop Thin Clients on Tailscale-Accessible Server (issue #70) — implementation epic.
- Sub-decisions: ADR-0012 (long polling), ADR-0013 (server hardening), ADR-0014 (auth), ADR-0015 (mobile platform), ADR-0016 (sovereignty / VPS fallback).
- Related: [ADR-0001](0001-resequence-around-oj-calibration.md) (substrate-first sequencing), [ADR-0002](0002-fifth-anchoring-principle-compounding-capability.md) (compounding capability), [ADR-0003](0003-layer-2-is-the-active-surface.md) (Layer 2 active surface), [ADR-0004](0004-introduce-agents-directory.md) (`agents/` directory).
- [`docs/MACOS_DESKTOP_APP_PLAN.md`](../MACOS_DESKTOP_APP_PLAN.md) — historical context for the retired embedded-PostgreSQL desktop direction.
- Source issue: #71.

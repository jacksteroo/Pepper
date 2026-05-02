# Pepper — macOS Desktop App Plan

> **Status: Superseded** (2026-05-02). The embedded-PostgreSQL-on-macOS-desktop direction described below is no longer the plan. Layer 3 has been re-scoped to a thin-client + server-heavy posture: a Swift/WKWebView macOS shell and a Capacitor mobile wrapper reach a FastAPI server over a Tailscale tailnet (home server primary, operator-owned VPS as fallback). The macOS shell is retained, but it does **not** bundle PostgreSQL.
>
> This document is retained as historical context. See Epic 08 (issue #70) and the master ADR-0011 (in flight) for the live plan.

## Goal

Build a first-class macOS desktop app for Pepper that:

- wraps the existing React UI in a native Swift shell
- keeps PostgreSQL + pgvector, but removes the Docker dependency
- supports local/native onboarding for:
  - Google Mail
  - Google Calendar
  - Yahoo Mail
  - WhatsApp Desktop
  - iMessage
- preserves Pepper's privacy-first, local-first architecture

Companion security/runtime plan:

- [AUTH_LIFECYCLE_PLAN.md](AUTH_LIFECYCLE_PLAN.md) — restart behavior, reauthorization policy, Keychain migration, Touch ID policy, and Telegram remote-access constraints

## Product Outcome

The user installs one notarized `.app`, launches Pepper, completes a native setup flow, grants the required permissions, and ends up with:

- a native macOS app window
- a bundled local Pepper backend
- a bundled local PostgreSQL instance with `pgvector`
- native auth + permissions management
- no Docker, no manual port choreography, no separate browser tab required

## Guiding Constraints

### 1. Keep the current stack where it already works

We should not rewrite the agent or the React app up front. The fastest path is:

- keep the Python backend
- keep the React frontend
- add a Swift desktop layer that hosts and supervises both

### 2. PostgreSQL stays; SQLite does not replace it

Pepper already depends on PostgreSQL + `pgvector` for memory and retrieval. We should keep that architecture and embed/manage a local PostgreSQL distribution rather than fallback to SQLite.

### 3. This should be a direct-distribution macOS app, not a Mac App Store app

Reading:

- `~/Library/Messages/chat.db` for iMessage
- WhatsApp Desktop local databases
- other local app data with Full Disk Access

makes App Store distribution unrealistic. The plan should assume:

- Developer ID signing
- Apple notarization
- direct download / auto-update

### 4. Privacy boundaries remain unchanged

Raw personal data stays local. The desktop shell should improve the privacy posture, not weaken it.

## Current Repo Baseline

Today Pepper already has:

- React + Vite web UI
- FastAPI/Python backend
- PostgreSQL + `pgvector`
- Google OAuth for Gmail and Calendar
- Yahoo IMAP support
- local iMessage reads from `~/Library/Messages/chat.db`
- local WhatsApp reads from the Desktop SQLite store
- Docker-based Postgres + service wiring

Relevant current implementation:

- [README.md](/Users/jack/Developer/Pepper/README.md)
- [agent/db.py](/Users/jack/Developer/Pepper/agent/db.py)
- [subsystems/calendar/auth.py](/Users/jack/Developer/Pepper/subsystems/calendar/auth.py)
- [subsystems/communications/gmail_client.py](/Users/jack/Developer/Pepper/subsystems/communications/gmail_client.py)
- [subsystems/communications/imap_client.py](/Users/jack/Developer/Pepper/subsystems/communications/imap_client.py)
- [subsystems/communications/imessage_client.py](/Users/jack/Developer/Pepper/subsystems/communications/imessage_client.py)
- [subsystems/communications/whatsapp_client.py](/Users/jack/Developer/Pepper/subsystems/communications/whatsapp_client.py)

## Target Architecture

```text
SwiftUI App
  ├── Native onboarding + permissions
  ├── Native settings + status surfaces
  ├── Process supervisor
  ├── Keychain access
  └── WKWebView hosting bundled React app
          ↓ localhost bridge
Embedded Pepper API
  ├── FastAPI / Python runtime
  ├── Existing tools + subsystems
  ├── Local auth token usage
  └── Local-only data processing
          ↓ unix socket or localhost
Embedded PostgreSQL
  ├── app-managed data dir
  ├── pgvector extension enabled
  └── lifecycle owned by Swift app
```

## Proposed macOS App Layers

### Swift Layer

The Swift app should own everything that is "desktop-native":

- app lifecycle
- first-run setup
- auth initiation UX
- permissions UX
- secure secret storage in Keychain
- process supervision for backend + database
- health checks and restart behavior
- menu bar / launch at login / notifications later

Preferred UI approach:

- `SwiftUI` for native shell screens
- `WKWebView` to host the React app for the main interface

### React Layer

Keep the React UI as the main product surface for:

- chat
- history
- dashboard
- settings views that don't need native controls

In production, React should be built into static assets and loaded from the app bundle. In development, the Swift shell can optionally point to the Vite dev server.

### Python Layer

Keep the existing FastAPI/Pepper backend as the application brain in v1 of the desktop app.

The Swift app launches and supervises:

- embedded Postgres
- Pepper API process

This avoids a backend rewrite and gives us a migration path that ships sooner.

## Auth and Connection Strategy

### Google Mail

Keep direct Google OAuth for Gmail.

Plan:

- continue using a Google OAuth desktop client
- initiate auth from Swift, not from terminal scripts
- keep scope read-only unless we intentionally expand later
- store refresh tokens in macOS Keychain
- materialize short-lived runtime credentials for the Python backend as needed

Implementation direction:

- move initiation from `setup_auth.py` into a native "Connect Google Mail" flow
- retire terminal-driven auth UX
- keep the backend Gmail client logic, but switch token sourcing away from ad hoc files over time

### Google Calendar

Short term:

- keep direct Google OAuth, matching the current backend

Medium term option:

- optionally support EventKit-backed calendar reads for calendars already synced into Apple Calendar

Why this sequence:

- it preserves current Pepper behavior
- it keeps calendar behavior consistent with existing multi-account Google support
- it avoids coupling the first desktop release to Apple Calendar sync state

### Yahoo Mail

Keep IMAP with app passwords.

Plan:

- native "Connect Yahoo" screen explains app-password setup
- credentials stored in Keychain, not plain JSON long term
- Python IMAP client reads credentials from a desktop-managed secret provider

### iMessage

No remote auth flow is needed. This is a local permissions and access problem.

Plan:

- detect whether `~/Library/Messages/chat.db` is readable
- guide the user to grant Full Disk Access to Pepper
- add a native health indicator: `Not granted`, `Granted`, `DB unavailable`, `Messages disabled`

Important packaging note:

- Pepper needs direct local DB reads
- this is another reason to avoid the Mac App Store path

### WhatsApp Desktop

No OAuth flow is needed if we keep the current local Desktop DB approach.

Plan:

- detect WhatsApp Desktop database presence
- guide the user to grant Full Disk Access if required
- handle the locked-database case cleanly in UI
- preserve exported-chat fallback for users who do not want filesystem access

## Database Plan: PostgreSQL Without Docker

### Decision

Bundle and manage a local PostgreSQL runtime plus `pgvector` inside the macOS app.

### Why not SQLite

- Pepper already uses PostgreSQL semantics
- Pepper already expects `pgvector`
- keeping Postgres avoids a risky storage rewrite
- future concurrency and memory workloads fit Postgres better

### Proposed approach

Package:

- PostgreSQL binaries
- `pgvector` extension binary compatible with the packaged Postgres version

At first launch, Swift:

1. creates an app-owned data directory under `~/Library/Application Support/Pepper/`
2. initializes a local Postgres cluster if missing
3. starts Postgres on an app-owned port or unix socket
4. waits for readiness
5. launches Pepper API with the correct `POSTGRES_URL`
6. lets existing `agent/db.py` create tables + enable `vector`

### Recommended storage layout

```text
~/Library/Application Support/Pepper/
  postgres/
    data/
    run/
    logs/
  backend/
    logs/
  web/
  exports/
```

### Operational requirements

- Postgres version pinned by the app
- in-app migrations on version upgrade
- database backup/export flow before destructive schema changes
- automatic crash recovery on next launch

## Migration Away From Docker

### Current state

Docker currently handles:

- Postgres lifecycle
- pgvector availability
- host volume mounts for auth/config data
- local source mounting in dev

### Desktop migration target

Replace Docker responsibilities with the Swift shell:

- process lifecycle: Swift supervisor
- volumes: Application Support directories
- env injection: app-managed config
- health checks: native status service

### Transition plan

Phase the migration so Docker remains a developer convenience until the desktop runtime is stable, but is no longer required for end users.

End-state:

- end users never install Docker
- developers can still use Docker during backend iteration if useful
- CI can continue to use containers where convenient

## Secret Storage Plan

Move secrets toward macOS-native storage.

### Store in Keychain

- Google refresh tokens
- Yahoo app passwords
- API keys the user enters in-app

### Store on disk

- non-secret app config
- account labels
- exported diagnostics
- local logs

### Backend integration options

#### v1

Swift reads secrets from Keychain and injects them into the backend process at startup or over a local IPC bridge.

#### v2

Backend calls a local secret-provider endpoint owned by the Swift shell.

Recommendation:

- ship v1 first
- evolve to a cleaner local secret provider after the desktop shell is stable

## Frontend and Native Integration

### Main UI Strategy

Use:

- `SwiftUI` for native shell pages
- `WKWebView` for Pepper's primary React interface

### Native-only views

These should be Swift-native:

- first-run setup
- permissions center
- connect account flows
- database/backend health
- update/recovery screens

### Web-hosted views

These can remain React:

- chat
- conversation history
- relationship dashboard
- life context
- general app settings

### Bridge requirements

Add a thin JS-native bridge for:

- opening auth windows
- requesting permission checks
- showing filesystem health
- sending diagnostics
- triggering reconnect flows

## Backend Packaging Plan

### Short-term

Bundle a Python runtime plus the Pepper backend and launch it as a managed local service.

### Medium-term

Consider packaging the backend into a more self-contained executable if startup, signing, or distribution becomes painful.

Important point:

- backend rewrite to Swift is not required for the desktop milestone
- do not mix the "native app" goal with a "rewrite the agent stack" goal

## macOS Permissions Plan

The app should own a first-class permissions center.

### Required/likely permissions

- Full Disk Access for iMessage and WhatsApp local DB reads
- Notifications for proactive briefs and reminders
- Login Item / Launch at Login if we want Pepper to stay available in background

### UX requirements

- detect missing permissions
- explain exactly why each one is needed
- deep-link users to System Settings where possible
- keep degraded mode usable when a permission is denied

## Distribution Plan

Use direct distribution:

- Developer ID signing
- Apple notarization
- DMG or zipped `.app`
- app-managed auto-update later

Do not target Mac App Store for the initial desktop product.

## Phased Delivery Plan

### Phase 1 — Desktop Shell Foundation

Deliver:

- SwiftUI app shell
- WKWebView hosting bundled React build
- app-managed config directory
- process supervisor for Pepper backend
- health screen for backend reachability

Success criteria:

- user launches one `.app`
- React UI loads inside the app
- backend starts automatically
- no browser tab required

### Phase 2 — Embedded PostgreSQL + pgvector

Deliver:

- packaged PostgreSQL runtime
- packaged `pgvector`
- first-run DB init
- app-managed DB lifecycle
- removal of Docker requirement for local app usage

Success criteria:

- Pepper boots with no Docker installed
- vector extension is enabled automatically
- existing memory flows continue working

### Phase 3 — Native Auth + Secrets

Deliver:

- native Google Mail connect flow
- native Google Calendar connect flow
- native Yahoo IMAP setup flow
- Keychain-backed secret storage
- desktop account status page

Success criteria:

- no terminal auth scripts required
- reconnect flow is visible in-app
- token expiry/health is surfaced in settings

### Phase 4 — Native Permissions + Local App Integrations

Deliver:

- iMessage permission diagnostics
- WhatsApp DB detection + lock-state diagnostics
- native setup checklist
- clear degraded-mode UX

Success criteria:

- user can understand exactly why iMessage/WhatsApp are unavailable
- support burden drops because setup state is visible and actionable

### Phase 5 — Production Hardening

Deliver:

- structured crash recovery
- backup/export tooling
- logging and diagnostics bundle
- signed/notarized release pipeline
- in-place upgrades for app and DB schema

Success criteria:

- app survives restarts cleanly
- upgrades do not corrupt local data
- support/debugging is possible from exported diagnostics

## Engineering Workstreams

### Workstream A — macOS Shell

- create SwiftUI app project
- embed WKWebView
- implement process supervisor
- add native settings/onboarding shell

### Workstream B — Backend Runtime

- define startup contract between Swift and Python
- support app-owned paths and env injection
- remove assumptions that auth is initiated from terminal

### Workstream C — Database Runtime

- package Postgres + `pgvector`
- implement init/start/stop/health checks
- validate schema migration flow

### Workstream D — Auth and Secrets

- replace `setup_auth.py` UX with native flows
- add Keychain storage
- add token health reporting

### Workstream E — Permissions and Data Access

- iMessage access checks
- WhatsApp access + lock checks
- user messaging for Full Disk Access

## Risks

### 1. Embedded PostgreSQL packaging complexity

Bundling Postgres + `pgvector` is the hardest infra piece in this plan.

Mitigation:

- isolate it as an early spike
- prove cold start, upgrade, and recovery before building too much UI around it

### 2. Filesystem permission friction

iMessage and WhatsApp access can fail for reasons the app does not control.

Mitigation:

- build excellent permission diagnostics early
- keep fallback and degraded-mode UX explicit

### 3. Token migration complexity

Moving from file-based tokens to Keychain can create backward-compatibility pain.

Mitigation:

- support importing existing `~/.config/pepper` credentials on first launch
- migrate once, then standardize

### 4. Over-scoping the desktop milestone

A native shell can turn into a rewrite if we are not disciplined.

Mitigation:

- keep React
- keep Python
- keep current tool contracts
- make the desktop app a host/supervisor first

## Recommended Sequence

If we want the highest-confidence path, build in this order:

1. Swift shell + WKWebView + backend supervision
2. embedded Postgres + `pgvector`
3. native auth + Keychain
4. native permissions center for iMessage/WhatsApp
5. notarized distribution + updater

## Concrete First Milestone

The first milestone should be:

"Launch Pepper as a single macOS app with bundled React UI, bundled Python backend, and bundled local Postgres, even before all connect flows are native."

That gets us:

- off the browser-tab model
- off Docker for end users
- onto a real desktop foundation

Then we can replace setup scripts with native onboarding one integration at a time.

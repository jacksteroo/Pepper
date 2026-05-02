# Pepper — System Architecture

## Overview

Pepper is a three-layer system. **Layer 1 — Data** brings the operator's world into the machine. **Layer 2 — Intelligence** is the agent runtime that reasons over that world. **Layer 3 — Presentation** is how the operator and Pepper meet. The orchestrator sits in Layer 2 with full life context; specialised subsystems sit in Layer 1; Telegram, the web app, and the future macOS shell sit in Layer 3. A maintenance layer keeps the whole system healthy and a security layer watches everything; both cut across the three.

Nothing is monolithic. Every layer is independently replaceable.

> Architectural decisions that shape this system are recorded in [`adr/`](adr/). When a section here cites an ADR, that ADR is the binding source for the decision — this document is the descriptive surface. `docs/GUARDRAILS.md` still takes precedence over any ADR.

This document leads with the layered framing because it is the framing the active roadmap is sequenced against ([ADR-0003](adr/0003-layer-2-is-the-active-surface.md)). The earlier subsystem-horizontal framing — useful for capability decomposition — is preserved as [Appendix B: Capability Decomposition (View B)](#appendix-b-capability-decomposition-view-b) at the end of this document. Both views describe the same system; the layered view is canonical, the capability view is a complementary cross-section. The choice is captured in [issue #15](https://github.com/jacksteroo/Pepper/issues/15).

---

## Three Layers

Pepper has three load-bearing layers. They are not equal in maturity at any given moment — see [ADR-0003](adr/0003-layer-2-is-the-active-surface.md) for the current ranking.

### Layer 1 — Data

The operator's world made available to the agent. Today this includes Gmail, Yahoo Mail, iMessage, WhatsApp, Slack, Telegram, Google Calendar, the persistent memory store (PostgreSQL + pgvector), and the life-context document. Future Layer 1 expansions (Knowledge, Health, Finance) are paused per [ADR-0001](adr/0001-resequence-around-oj-calibration.md) until substrate work in Layer 2 lands.

The privacy invariant lives here: raw data stays local, processed locally, never leaves the machine. Layer 1 is what makes that invariant enforceable. As of 2026-05, Layer 1 is **mature** for the next two sprints — bug fixes only, no new sources.

### Layer 2 — Intelligence

The agent runtime. Pepper Core (orchestrator), the semantic intent router, prompt assembly, retrieval, the skill system, the MCP client, the routing-event store, and — once the substrate phase lands — the trace store, hybrid retrieval, reflection runtime, and learned routing/prompt optimization. The cognitive agents introduced by [ADR-0004](adr/0004-introduce-agents-directory.md) (`agents/reflector/`, `agents/monitor/`, …) are Layer 2 inhabitants.

Per [ADR-0003](adr/0003-layer-2-is-the-active-surface.md), Layer 2 is the **active surface** for the next two sprints: comprehension regressions are diagnosed here first, new investments land here, and roadmap items that propose new Layer 1 sources during this window are deferred to `WISHLIST.md`.

### Layer 3 — Presentation

How Pepper meets the operator. Telegram is the primary interface today; the local React + Vite web app is secondary. The next platform direction is a **thin-client + server-heavy** posture: a Capacitor mobile wrapper and a Swift/WKWebView macOS shell, both reaching a FastAPI server over a Tailscale tailnet, home server primary with an operator-owned VPS as fallback. See Epic 08 (issue #70) and the master ADR-0011 (in flight). The earlier embedded-PostgreSQL desktop plan in [`MACOS_DESKTOP_APP_PLAN.md`](MACOS_DESKTOP_APP_PLAN.md) is superseded by this direction. Voice is on the wishlist.

Layer 3 is in steady state. A better presentation layer over a confused agent would be a demo, not a tool, so Layer 3 work is not the active surface during the Layer 2 window.

### Cross-cutting layers

Two layers cut across the three:

- **Maintenance** — Claude Code schedulers, model evaluation and swap, health monitoring, self-healing agents, zero-downtime upgrades.
- **Security** — red-team adversarial agents, prompt-injection testing, anomaly detection, full audit logging, the "agents on agents" KGB model.

Both touch every layer and are therefore not numbered alongside them.

---

## Layer-to-component mapping

```text
┌─────────────────────────────────────────────────────────────┐
│                      INTERFACE LAYER                        │
│   Telegram Bot · Local Web App · macOS Desktop (Swift, future) · Voice  │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│                      Pepper CORE                            │
│                  Orchestrator Agent                         │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐   │
│  │              LIFE CONTEXT DOCUMENT                  │   │
│  │  Who you are · What matters · What's happening now  │   │
│  │  Family · Goals · Values · Patterns · Open loops    │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │  Persistent  │  │  Proactive   │  │  Tool Router     │  │
│  │  Memory      │  │  Scheduler   │  │  (MCP client)    │  │
│  │  (pgvector)  │  │  Morning     │  │                  │  │
│  │              │  │  brief, etc  │  │                  │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP / REST
          ┌────────────────┼────────────────────┐
          │                │                    │
┌─────────▼──────┐ ┌───────▼──────┐ ┌──────────▼──────┐
│   PEOPLE       │ │    TIME      │ │  COMMUNICATIONS  │
│                │ │              │ │                  │
│ Future layer   │ │ Google Cal ✅ │ │ Gmail ✅         │
│ Relationship   │ │ Deadlines ⏳  │ │ Yahoo Mail ✅    │
│ context +      │ │ (needs Slack)│ │ Telegram ✅      │
│ outreach       │ │ Event prep ⏳ │ │ iMessage ✅      │
│                │ │              │ │ WhatsApp ✅      │
│                │ │              │ │ Slack ✅         │
└────────────────┘ └──────────────┘ └─────────────────-┘
          │                │                    │
┌─────────▼──────┐ ┌───────▼──────┐ ┌──────────▼──────┐
│   KNOWLEDGE    │ │    HEALTH    │ │    FINANCE       │
│                │ │              │ │                  │
│ Notes/Obsidian │ │ Apple Health │ │ Bank CSV exports │
│ Documents      │ │ Oura Ring    │ │ Investment feeds │
│ Decisions log  │ │ Garmin/Whoop │ │ Planning models  │
│ Claude history │ │ + others     │ │                  │
└────────────────┘ └──────────────┘ └──────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    PERSISTENCE LAYER                        │
│   PostgreSQL + pgvector · Local embeddings · File storage   │
│   Git (code) · WAL (data) · Snapshots (vectors)            │
└─────────────────────────────────────────────────────────────┘

┌──────────────────────────┐  ┌──────────────────────────────┐
│    MAINTENANCE LAYER     │  │       SECURITY LAYER         │
│                          │  │                              │
│ Claude Code schedulers   │  │ Red-team adversarial agents  │
│ Model evaluation + swap  │  │ Prompt injection testing     │
│ Health monitoring        │  │ Anomaly detection            │
│ Self-healing agents      │  │ Full audit logging           │
│ Zero-downtime upgrades   │  │ KGB model: agents on agents  │
└──────────────────────────┘  └──────────────────────────────┘
```

---

## Pepper Core

The orchestrator is the only component that has a full view of the owner's life. Every other component is a specialist.

### Life Context Document

The most important data structure in the system. A living document containing:

- Current life situation (factual: who's in your life, what roles you hold, what's happening)
- Values and what actually matters
- Active concerns, open loops, things being avoided
- Patterns — how the owner thinks, where they get stuck, what helps
- Goals for the next chapter
- Family member profiles (relationships, dynamics, needs)
- Professional context (current work, key relationships, priorities)

This is Pepper's ground truth. All proactive recommendations are filtered through it. The live document lives at `data/life_context.md` (gitignored, mutable — Pepper writes back to it via the `update_life_context` tool, and every change appends a `LifeContextVersion` row in Postgres). The annotated template lives at [LIFE_CONTEXT.md.example](LIFE_CONTEXT.md.example).

### Persistent Memory

Custom implementation built on PostgreSQL + pgvector:

- **Working memory**: current conversation context (in-memory during session)
- **Recall memory**: recent events and decisions (last 30 days, PostgreSQL full-text search)
- **Archival memory**: compressed long-term storage (pgvector semantic search)

Memory is additive. Old events are summarized and compressed, never deleted. All embeddings generated locally using Ollama (nomic-embed-text).

### Proactive Scheduler

Built on APScheduler (Python), runs on a timer and event triggers:

- **Morning brief** ✅ (configurable time, default 7am): what's today, who needs attention, what's coming
  - 🔄 Enhancement in progress: news integration (mainstream + WSJ via Brave Search/RSS)
- **Weekly review** ✅ (configurable day/time, default Sunday 6pm): relationship health, open loops, next week preview
- **Pre-event intelligence** ⏳ (30min before calendar event): work meetings get conversation context, family events get logistics prep
- **Event-triggered** ⏳: job change detected, message from dormant contact, upcoming deadline (requires Slack integration)
- **Commitment tracking** ⏳: surfaces promises made that haven't been fulfilled

### Tool Router

Routes tool calls to the appropriate subsystem. Currently a monolithic implementation (all tools imported directly into core), but designed to be split into MCP-compatible subsystems over time. Pepper Core uses Anthropic's tool calling format.

---

## Subsystem Interface Contract

**Current implementation** (Phase 1-3): Subsystems are Python modules (`subsystems/*/`) that expose tools directly to Pepper Core via imports. Tool definitions live in `agent/*_tools.py` files.

**Future architecture** (Phase 4+): Each subsystem will be a standalone service exposing:

```python
# Standard subsystem interface
GET  /health              # Subsystem health check
GET  /tools               # List of available MCP tools
POST /tools/{tool_name}   # Execute a tool
GET  /status              # Current subsystem status and last sync time
```

Tool definitions follow Anthropic's function calling format:

```json
{
  "type": "function",
  "function": {
    "name": "tool_name",
    "description": "What this tool does",
    "parameters": {
      "type": "object",
      "properties": { ... },
      "required": [...]
    }
  }
}
```

This contract means any subsystem can be replaced without modifying Pepper Core. Current subsystems (calendar, email) are designed with this future split in mind.

---

## LLM Architecture

Two model tiers, separated by data sensitivity:

### Local Tier (Ollama) — Default

- Handles all raw personal data processing
- iMessage reading, email parsing, health data, financial data
- Routine retrieval, summarization, all tool calling
- Runs continuously, no cost, no data exfiltration
- Current default model: **hermes-4.3-36b-tools:latest** (configurable via `DEFAULT_LOCAL_MODEL` in `.env`)
- Other supported: hermes-4.3-36b-tools:latest, Llama 3.3 70B

### Frontier Tier (Claude API) — Optional

- Receives summaries and structured outputs only — **never raw personal data**
- Complex reasoning: family conversations, difficult decisions, strategic planning, high-quality drafting
- **Disabled by default** — `DEFAULT_FRONTIER_MODEL` defaults to local model so the system runs fully offline
- Enable by setting `ANTHROPIC_API_KEY` and `DEFAULT_FRONTIER_MODEL=claude-sonnet-4-6` in `.env`
- Estimated cost when enabled: ~$20-50/month for personal EA usage
- Graceful fallback: if API is unreachable, local tier handles everything at lower quality

### hermes3 Quirks — Handled in `agent/llm.py`

hermes3 sometimes emits tool calls as raw JSON text in the `content` field rather than using the structured `tool_calls` field. `_extract_text_tool_calls()` detects these patterns and normalises them before they reach Pepper Core — the rest of the system never sees the difference.

hermes3 can also return empty `content` after a tool call (e.g. for image requests it considers "done"). When this happens, Pepper Core re-prompts with "Please summarize what you found." to force a text response.

Ollama can occasionally return transient HTTP 500s even when the local model is otherwise healthy. Pepper classifies those as temporary local-model unavailability and retries the same local model before surfacing an actionable Ollama error.

### Abstraction Layer

All model calls go through a single interface that mirrors Ollama's OpenAI-compatible API. Swapping models is a config change. The agent never talks to a model directly.

```python
# All model calls look like this regardless of which model runs them
response = llm.chat(
    model=config.model,      # "hermes-4.3-36b-tools:latest" or "claude-sonnet-4-6" 
    messages=messages,
    tools=tools
)
```

---

## Data Flow

### Inbound (data entering Pepper)

```text
External sources → Subsystem importers → Local PostgreSQL/pgvector
→ Embedding generation (local sentence-transformers)
→ Pepper Core context (via tool calls)
→ Life context document (updated by Pepper when patterns detected)
```

### Outbound (Pepper acting on your behalf)

```text
Pepper Core reasoning → Draft action (message, calendar event, reminder)
→ Human approval (for significant actions)
→ Execution via interface layer
```

Pepper proposes; the owner decides. No automated action without approval for anything consequential.

---

## Interface Layer

### Telegram Bot ✅ (Primary)

- Runs on local network; accessible from phone anywhere with internet
- Natural language input: "What should I focus on today?"
- Proactive push notifications: morning brief, weekly review
- Command shortcuts: `/brief`, `/review`, `/status`
- Built with python-telegram-bot
- **Concurrent ack + think UX**: local LLM generates a context-aware acknowledgment while the main chat task runs in parallel; animated thinking indicator cycles through phrases + star frames while waiting
- **Typewriter response**: responses revealed sentence-by-sentence with a blinking cursor pause between each sentence; blank lines become separate messages
- **Inline image rendering**: LLM embeds `[IMAGE:url]` markers in responses; bot strips them and sends each as a Telegram photo before the text

### Local Web App ✅ (Secondary)

- React + Vite frontend (<http://localhost:5173>)
- Conversation history viewer
- Life context viewer
- System status dashboard
- Animated loading indicator (phrase + star cycling, matches Telegram UX)
- Docker service included in `docker-compose.yml` (`pepper-web`); `VITE_API_URL` env var configures API target
- Settings (future)

### macOS Desktop App ⏳ (Future)

- Swift shell with WKWebView wrapping the existing React UI — no frontend rewrite needed
- Embedded PostgreSQL + pgvector — no Docker required for end users
- Notarized and sandboxed for distribution
- Native notifications; Touch ID as step-up auth for sensitive local actions
- See [MACOS_DESKTOP_APP_PLAN.md](MACOS_DESKTOP_APP_PLAN.md) for full plan

### Voice ⏳ (Future)

- Whisper STT (local, Apple Silicon optimized)
- Local TTS for responses
- Always-on ambient mode: "Hey Pepper..."

---

## Persistence Layer

| Data type | Storage | Retention |
| --- | --- | --- |
| Life context | Markdown file (git versioned) | Forever, full history |
| Conversations | PostgreSQL (working/recall/archival tables) | Forever, tiered compression |
| Calendar events | Live API calls (no local storage) | N/A (fetched on demand) |
| Email | Live API calls (no local storage) | N/A (fetched on demand) |
| iMessage / WhatsApp | Read-only from local SQLite DBs (never copied to PostgreSQL) | N/A (fetched on demand) |
| Embeddings | pgvector (HNSW index) | Forever |
| Memory entries | PostgreSQL + pgvector | Forever, tiered (working/recall/archival) |
| Audit logs | Append-only log files | Forever |
| Code | Git | Full version history |

**Backup strategy**: PostgreSQL WAL streaming to local backup volume. Daily snapshots. Point-in-time recovery to any moment. Vector index snapshots weekly.

---

## Communication Between Components

**Current implementation** (Phase 1-3):

```text
Pepper Core  ←→  Subsystems      via direct Python imports
Pepper Core  ←→  Ollama          via HTTP (localhost:11434 / host.docker.internal:11434)
Pepper Core  ←→  Claude API      via HTTPS (summaries only, optional — disabled by default)
Pepper Core  ←→  Telegram        via Telegram Bot API (HTTPS, long-polling)
Pepper Core  ←→  Google APIs     via HTTPS (Calendar, Gmail)
Pepper Core  ←→  PostgreSQL      via asyncpg (localhost:5432 / Docker service)
Pepper Core  ←→  iMessage DB     via read-only SQLite ($HOME/Library/Messages/chat.db or /data/messages)
Pepper Core  ←→  WhatsApp DB     via read-only SQLite (Group Containers or /data/whatsapp)
Web Frontend ←→  Pepper Core     via HTTP (localhost:8000)
```

**macOS DB access:** iMessage and WhatsApp SQLite files are read directly — no data is copied.

- Native (no Docker): Terminal requires Full Disk Access in System Settings
- Docker: mount `$HOME/Library/Messages` and `$HOME/Library/Group Containers/group.net.whatsapp.WhatsApp.shared` as read-only volumes; grant Full Disk Access to Docker Desktop instead

**Future architecture** (Phase 4+):

```text
Pepper Core  ←→  Subsystems      via MCP (local HTTP)
Pepper Core  ←→  LLM models      via Ollama API (local)
Pepper Core  ←→  Claude API      via HTTPS (summaries only)
Pepper Core  ←→  Telegram        via Telegram Bot API (HTTPS)
Maintenance  ←→  Pepper Core     via health endpoints + git
Security     ←→  All components  via audit log tailing + probe endpoints
```

All inter-component communication is localhost or LAN. External connections: Telegram bot API (interface only), Claude API (summaries only, optional), Google APIs (Calendar/Gmail, optional), Brave Search (optional), Google Maps (optional), and model download endpoints (one-time, for model pulls).

---

## Appendix B: Capability Decomposition (View B)

The body of this document presents Pepper as three layers (Data, Intelligence, Presentation), which is the canonical framing per [ADR-0003](adr/0003-layer-2-is-the-active-surface.md) and [issue #15](https://github.com/jacksteroo/Pepper/issues/15). The same system can also be read as a horizontal decomposition into capability subsystems and runtime phases — useful when you want to know *what is built today* and *which subsystem owns which tools*. That decomposition is preserved below, unchanged.

If the two views ever drift, the layered framing in the body wins and this appendix is updated to match.

### Current subsystem status (capability view)

#### COMMUNICATIONS Layer (Phase 2) — ✅ Complete

**Built:**

- Gmail integration via OAuth2 (`subsystems/communications/gmail_client.py`)
- Yahoo Mail via IMAP (`subsystems/communications/imap_client.py`)
- Tools: `get_recent_emails`, `search_emails`, `get_email_unread_counts`
- **iMessage reader** (`subsystems/communications/imessage_client.py`) — reads local `~/Library/Messages/chat.db`
  - Tools: `get_recent_imessages`, `get_imessage_conversation`, `search_imessages`
  - Parameterized SQL, graceful degradation if Full Disk Access not granted
- **WhatsApp integration** (`subsystems/communications/whatsapp_client.py`) — reads local `ChatStorage.sqlite`
  - Tools: `get_recent_whatsapp_chats`, `get_whatsapp_chat`, `search_whatsapp`, `get_whatsapp_groups`
  - Fallback: parses WhatsApp `.txt` chat exports
- **Slack integration** (`subsystems/communications/slack_client.py`) — Slack Bot API (read-only)
  - Tools: `search_slack`, `get_slack_channel_messages`, `get_slack_deadlines`, `list_slack_channels`
  - Deadline detection via regex patterns ("due Friday", "by EOD", "ship by", etc.)
  - Requires `SLACK_BOT_TOKEN` in `.env`
- **Contact enrichment** (`subsystems/communications/contact_enricher.py`)
  - Cross-references contacts across iMessage, WhatsApp, email
  - Tools: `get_contact_profile`, `find_quiet_contacts`, `search_contacts`
- **Communication health dashboard** (`agent/comms_health_tools.py`)
  - Tools: `get_comms_health_summary`, `get_overdue_responses`, `get_relationship_balance_report`
  - Integrated into morning brief (surfaces 1-2 signals)
  - API endpoint: `GET /comms-health`
  - Web UI tab: "Relationships" (`web/src/components/Relationships.tsx`)
- Proactive context injection for all channels when relevant keywords mentioned

#### TIME Layer (Phase 2) — ✅ Complete

**Built:**

- Google Calendar integration via OAuth2 (`subsystems/calendar/`)
- Multi-account support with per-account calendar selection and display name overrides (`subsystems/calendar/preferences.py`)
- Tools: `get_upcoming_events` (next 90 days), `get_calendar_events_range` (arbitrary past/future date range), `list_calendars`
- Proactive context injection when schedule mentioned
- Unified Google auth shared with Gmail (`subsystems/google_auth.py`)

**Deferred (wishlist):**

- Pre-event intelligence (30 min before events) — planned as a Phase 4 skill
- Deadline tracking from Slack — planned as a Phase 4 skill
- Commitment tracking from conversation promise language — planned as a Phase 4 skill

#### Additional Capabilities

- **Web search** (`agent/web_search.py`) — Brave Search API; includes `brave_image_search` for image queries
- **Image search** — `search_images` tool calls `brave_image_search`; LLM embeds results as `[IMAGE:url]` markers that the Telegram bot renders as inline photos
- **Routing** (`agent/routing.py`) — Google Maps Distance Matrix API with live traffic
- **Account management** (`agent/accounts.py`) — Centralized gitignored config for personal data

#### RUNTIME Layer (Phase 3) — ✅ Complete

- **Parallel tool execution** (`agent/tool_router.py`) — read-only tools dispatched concurrently via `asyncio.gather`; side-effect tools execute sequentially in model-produced order
- **Context compression** (`agent/context_compressor.py`) — auto-compresses long conversations before hitting the model's context window; always runs on local Ollama (privacy invariant enforced)
- **Error classifier + smart fallback** (`agent/error_classifier.py`) — typed `ErrorCategory` / `DataSensitivity` error handling; classified retry loop; privacy invariant preserved under all failure modes
- **Semantic intent router** (`agent/semantic_router.py`) — Phase 3 cutover (2026-04-29): k-NN intent classifier over per-intent exemplar embeddings (`qwen3-embedding:0.6b`, pgvector HNSW). Primary router; legacy regex `agent/query_router.py` runs in shadow alongside it (writes to `routing_events.shadow_decision_*`) until Phase 5 cleanup removes the codepath. Capability-registry filtering applied as a deterministic post-route step. Pre-commit eval gate (`scripts/git-hooks/pre-commit-router-eval`, ≥85% on `tests/router_eval_set.jsonl`) gates router-relevant edits. See `docs/SEMANTIC_ROUTER.md` for operating details.

#### SKILL SYSTEM (Phase 4) — ✅ Complete

- **Skill files** (`skills/*.md`) — YAML frontmatter + workflow markdown
- **Skill injection** (`agent/skills.py`) — trigger matching + prompt injection
- **Self-improving** (`agent/skill_reviewer.py`) — background review + human-approved diffs
- 5 working skills: morning_brief, weekly_review, commitment_check, draft_reply_to_contact, prep_for_meeting

#### MCP INTEGRATION (Phase 5) — ✅ Complete

- **MCP Client** (`agent/mcp_client.py`) — connects to external MCP servers via stdio, discovers tools, routes calls
- **MCP Audit** (`agent/mcp_audit.py`) — privacy-preserving trust levels (local/trusted/external), data classification, audit logging
- **Subsystem MCP servers** (`subsystems/calendar/mcp_server.py`, `subsystems/communications/mcp_server.py`) — subsystems exposed as standalone MCP services
- **Pepper as MCP Server** (`agent/mcp_server.py`) — exposes safe subset of tools to Claude Desktop/Code/Cursor
- **Privacy enforcement**: `RAW_PERSONAL_TOOLS` (iMessage, WhatsApp, email, Slack, memory) NEVER reach external/trusted servers; 59 regression tests
- Configuration: `config/mcp_servers.yaml` (external servers), `config/mcp_server_access.yaml` (access control)

#### Not Yet Started

- **PEOPLE** — future People subsystem integration (post Phase 5)
- **KNOWLEDGE** (post Phase 5) — Notes, documents, decision log
- **HEALTH** (post Phase 5) — health data ingestion (Apple Health export, Oura Ring, Garmin, Whoop, and others)
- **FINANCE** (post Phase 5) — Bank CSV parsing
- **macOS DESKTOP APP** — Swift shell, embedded PostgreSQL, no Docker

# Pepper — Build Roadmap

## Philosophy

Build the orchestrator first. Subsystems plug in over time. Each phase must be genuinely useful before the next begins — no building ahead of real need.

Pepper tells us what to build next. As the system is used, gaps become apparent. Phase priorities are suggestions, not mandates — actual usage overrides the plan.

**Current focus**: Phases 1 and 2 shipped a working assistant with deep personal integrations. The active roadmap below pivots from *adding capabilities* to *upgrading the agent runtime* — faster execution, structured workflows via a skill system, opening the architecture to MCP, and then hardening the agent's intent/capability reliability. This is foundation work that makes every future capability cheaper to build.

Deferred capability work (knowledge layer, health & finance, maintenance & security, advanced time-layer features) moved to [WISHLIST.md](WISHLIST.md). It will come back as actual usage surfaces needs.

**Next platform direction**: a notarized macOS desktop app that wraps the existing React UI in a Swift shell, embeds PostgreSQL + pgvector locally, and removes Docker from the end-user experience. See [MACOS_DESKTOP_APP_PLAN.md](MACOS_DESKTOP_APP_PLAN.md).

**Near-term auth/platform work**: formalize credential lifecycle behavior across Docker today and the macOS app later:

- restart-safe auth (no unnecessary reauth on backend/app/container restart)
- explicit account auth states (`connected`, `needs_reauth`, `needs_local_login`, `upgrade_required`, etc.)
- Keychain migration for Google/Yahoo secrets
- Touch ID as step-up auth for sensitive local actions, not routine Telegram-triggered reads
- clear Telegram behavior for remote access when local device presence is unavailable

Detailed plan: [AUTH_LIFECYCLE_PLAN.md](AUTH_LIFECYCLE_PLAN.md).

---

## Phase 0 — Life Context (Before Any Code)

**Duration**: 1–2 sessions of honest reflection
**Output**: A rich, honest `LIFE_CONTEXT.md` that Pepper can use as its ground truth

This is the most important work. Before any software is useful, Pepper needs to know:

- Who is in your life and what those relationships mean
- What you're responsible for (work, family, household, finances)
- What you're trying to build in the next chapter
- What keeps you up at night
- What "a good week" looks like
- Your patterns — how you make decisions, where you get stuck

See [LIFE_CONTEXT.md.example](LIFE_CONTEXT.md.example) for the annotated template. Copy it to `LIFE_CONTEXT.md` and fill it in.

**Success criterion**: You can hand this document to a thoughtful stranger and they could give you genuinely useful advice about your life.

---

## Phase 1 — Pepper Core

**Goal**: A working orchestrator you can talk to, that knows your life context, and can be proactive
**Estimated build**: 2–3 weeks
**Status**: ✅ Complete

### What was built

**1.1 — Agent Runtime** ✅

- FastAPI-based orchestrator (custom implementation, not Letta/MemGPT)
- Ollama integration (hermes-4.3-36b-tools:latest as primary local model)
- Claude API integration for reasoning-heavy tasks
- Life context document loaded into agent at startup

**1.2 — Persistent Memory** ✅

- Working memory: current conversation
- Recall memory: last 30 days of interactions, searchable
- Archival memory: older events, compressed and vector-indexed
- PostgreSQL + pgvector for all persistent storage
- Memory update tools: agent can write to its own memory when it learns something important

**1.3 — Proactive Scheduler** ✅

- Morning brief (configurable time via APScheduler)
- Manual triggers: `/brief`, `/review`
- Commitment tracking: remembers what you said you'd do
- Weekly review scheduling

**1.4 — Telegram Interface** ✅

- Local Telegram bot (python-telegram-bot)
- Natural language conversation
- Proactive push messages (morning brief, alerts)
- Command shortcuts: `/brief`, `/review`, `/status`

**1.5 — Local Web Interface** ✅

- React + Vite frontend
- Conversation history viewer
- Life context viewer
- System status dashboard

**1.6 — Bonus: Additional Capabilities** ✅

- Web search integration (Brave Search API)
- Image search: `search_images` tool + `brave_image_search`; Telegram bot renders images inline via `[IMAGE:url]` markers
- Routing/navigation (Google Maps Distance Matrix API for driving time with live traffic)
- Account configuration system (`agent/accounts.py` + `config/local/accounts.json`)

### Success criteria (all met)

✅ Can have a conversation with Pepper about anything in your life
✅ Pepper sends a morning brief without being asked
✅ Pepper remembers things from yesterday's conversation
✅ Pepper can be asked "what am I missing?" and give a useful answer based on life context

### Enhancements in progress

**1.7 — News Integration for Morning Brief** 🔄

- Mainstream news headlines from major outlets (via Brave Search API — already integrated)
- Subscription content: WSJ, other paywalled sources
- Intelligent summarization: what's relevant to your life context and work
- Delivered as part of morning brief: "Here's what's happening in the world today..."
- Configurable: news categories/sources, how much detail, which topics to prioritize

**Technical approach:**

- **General news**: Brave Search API (already integrated) — search for "breaking news", "top headlines", etc.
- **WSJ/premium sources**: Multiple options to explore:
  - Option A: Dow Jones Factiva API (enterprise, requires paid subscription + API key)
  - Option B: WSJ RSS feeds (`feeds.content.dowjones.io`) — limited, may only get headlines
  - Option C: Third-party aggregators (Barchart News API, RapidAPI)
  - Start with RSS feeds, upgrade to Factiva API if needed
- **Content filtering**: Local LLM does initial relevance filtering based on life context
- **Summarization**: Claude API for final digest (summaries only, respects copyright)
- **Configuration**: `.env` settings for news sources, topics, and detail level

---

## Phase 2 — Communications Layer

**Goal**: Pepper sees your actual personal and professional relationships, not just what's been imported
**Estimated build**: 2 weeks
**Status**: ✅ Complete
**Depends on**: Phase 1 complete and useful

### What was built

**2.1 — Email Reader** ✅

- Gmail integration via OAuth2 (`subsystems/communications/gmail_client.py`)
- Yahoo Mail via IMAP (`subsystems/communications/imap_client.py`)
- Multi-account support (personal, work, etc.)
- Tools: `get_recent_emails`, `search_emails`, `get_email_unread_counts`
- Supports Gmail search syntax (`from:`, `subject:`, `has:attachment`, etc.)
- Proactive context injection: auto-surfaces unread counts when email-related keywords mentioned
- Real-time API queries (no local storage — privacy-first)

**2.2 — iMessage Reader** ✅

- Reads `~/Library/Messages/chat.db` (local SQLite — no API required)
- Tools: `get_recent_imessages`, `get_imessage_conversation`, `search_imessages`
- Parameterized queries (SQL injection safe), Full Disk Access graceful degradation
- Proactive context injection: auto-surfaces unread count when text/iMessage mentioned
- Privacy: processed entirely locally, never transmitted

**2.3 — WhatsApp Integration** ✅

- Reads local WhatsApp SQLite DB (`~/Library/Application Support/WhatsApp/ChatStorage.sqlite`)
- Identifies personal vs group chats; exposes member counts for group dynamics
- Fallback: parses `.txt` chat export files
- Tools: `get_recent_whatsapp_chats`, `get_whatsapp_chat`, `search_whatsapp`, `get_whatsapp_groups`
- Proactive context injection when WhatsApp mentioned
- Privacy: processed entirely locally, never transmitted

**2.4 — Slack Integration** ✅

- Slack Bot API (read-only): channels, DMs, message history, search
- Tools: `search_slack`, `get_slack_channel_messages`, `get_slack_deadlines`, `list_slack_channels`
- Deadline detection: regex patterns for "due Friday", "by EOD", "ship by", "urgent", etc.
- Requires `SLACK_BOT_TOKEN` in `.env` (instructions in `.env.example`)
- Privacy: processed locally, summaries only to frontier LLM

**2.5 — Contact Enrichment** ✅

- Cross-references contacts across iMessage, WhatsApp, and email
- Tools: `get_contact_profile`, `find_quiet_contacts`, `search_contacts`
- `get_contact_profile`: last contact time per channel, dominant channel, multi-channel presence
- `find_quiet_contacts`: surfaces people who've gone quiet above a threshold (default 14 days)
- Groundwork for future People subsystem integration

**2.6 — Communication Health Dashboard** ✅

- Tools: `get_comms_health_summary`, `get_overdue_responses`, `get_relationship_balance_report`
- Morning brief integration: 1-2 communication signals surfaced daily
- REST API: `GET /comms-health` endpoint
- Web UI: "Relationships" tab in local web app (overdue responses, quiet contacts, balance bar)

### Success criteria

✅ Pepper can check email across multiple accounts
✅ Pepper knows about conversations from iMessage and WhatsApp, not just Telegram
✅ Pepper can read Slack conversations and detect work deadlines/commitments
✅ Pepper understands group chat dynamics (family groups, friend groups on WhatsApp)
✅ "How are things with [family member]?" gives a real answer based on actual conversation history
✅ "What's been happening in the [group name] chat?" gives a useful summary
✅ Pepper can flag when someone important has been quiet

### Calendar reader (carried over from original Phase 3)

**2.7 — Calendar Reader** ✅

- Google Calendar integration via OAuth2 (`subsystems/calendar/`)
- Multi-account support (personal, work, etc.)
- Tools: `get_upcoming_events` (next 90 days), `get_calendar_events_range` (arbitrary past/future date range — ISO date strings), `list_calendars`
- Proactive context injection: auto-surfaces relevant events when schedule-related keywords mentioned
- Real-time API queries (no local storage/sync needed with Google Calendar API)

*Pre-event intelligence, deadline awareness, and enhanced commitment tracking — the rest of the original Phase 3 — are deferred to [WISHLIST.md](WISHLIST.md). They become cleaner to build once the skill system (Phase 4) lands.*

---

## Phase 3 — Runtime Upgrades

**Goal**: Make the existing agent runtime faster, smarter, and more resilient — without adding new capabilities
**Estimated build**: ~1 week
**Status**: ✅ Complete
**Depends on**: Phase 2 complete ✅

Context: Phases 1–2 proved the architecture works. Before building more capability surface area, the runtime itself deserves investment. These three upgrades are small, independent, and drawn from patterns proven in the Hermes Agent codebase (Nous Research's self-improving agent framework). None of them introduce new user-facing concepts — they make what already exists dramatically better.

### 3.1 — Parallel Tool Execution ✅

**Problem**: `agent/tool_router.py` executes tool calls sequentially. A morning brief that queries calendar + email + Slack + iMessage + WhatsApp takes 5× longer than necessary, and every subsystem round-trip blocks the next.

**Approach**:

- Add a `side_effects: bool` flag to every tool definition (default `True` for safety; explicit `False` on pure reads)
- When the model returns multiple tool calls in a single turn, partition them:
  - **Read-only tools** → dispatch concurrently via `asyncio.gather` (natural fit for existing async subsystem clients)
  - **Side-effect tools** (memory writes, commitment saves, future create/update ops) → stay sequential, in the order the model produced them
- Preserve message ordering when merging tool results back into the conversation
- Respect per-subsystem rate limits: add a semaphore per subsystem base URL (e.g., max 3 concurrent Gmail calls)

**Reference**: Hermes Agent's `run_agent.py` — classifies tools by safety and batches read-only calls in a `ThreadPoolExecutor`. Pepper's async-native architecture makes `asyncio.gather` the natural equivalent.

**Files touched**:

- `agent/tool_router.py` (dispatch logic)
- `agent/*_tools.py` (add `side_effects` flag to tool definitions)
- `agent/core.py` (merge results back in correct order)

**Success criteria**:

- Morning brief completes in <50% of current wall time
- No ordering regressions in conversation history
- Side-effect tools still execute in model-produced order

### 3.2 — Context Compression ✅

**Problem**: Long conversations grow until they hit the model's context window. There's no compression strategy, so multi-hour sessions either degrade or fail. This is especially painful on local Ollama models with smaller context windows (hermes-4.3-36b-tools:latest is 8K–32K depending on variant).

**Approach**:

- Track token count per message in conversation history
- When approaching 80% of the model's context window, trigger compression:
  - Keep the last N turns (default 6) uncompressed — the recent-context anchor
  - Summarize everything older into a compact summary block
  - Preserve verbatim: decisions, commitments made, facts stated about people
  - Replace the summarized turns with a single `system` message: `[Summary of earlier conversation: ...]`
- Save the pre-compression conversation to recall memory (pgvector) so nothing is lost — just moved out of the active context window
- **Privacy-critical**: compression runs on the local Ollama model only. Raw conversation history must never go to Claude for summarization, even under pressure. Enforce this in `agent/llm.py` by tagging the compression call as local-only.

**Reference**: Hermes Agent's `agent/context_compressor.py`.

**Files touched**:

- New: `agent/context_compressor.py` ✅
- `agent/llm.py` — `local_only=True` parameter added; overrides any non-local model to enforce privacy ✅
- `agent/core.py` — compression check runs before each LLM call ✅
- `agent/config.py` — `MODEL_CONTEXT_TOKENS` setting added (default 8192) ✅
- New: `agent/tests/test_context_compressor.py` — 21 tests including privacy invariant regression tests ✅

**Success criteria**:

- ✅ Multi-hour Telegram conversations complete without hitting context limits
- ✅ After compression, Pepper can still reference facts from earlier in the conversation (via the summary + recall memory fallback)
- ✅ Privacy audit: no compression call ever routes to Claude API (enforced in two layers: model string + `local_only=True` flag)

### 3.3 — Error Classifier + Smart Fallback ✅

**Problem**: `agent/llm.py` had basic graceful fallback but treated all failures the same way (generic retry, then error). Users saw "something went wrong" for what should be specific, actionable failures. Worse, the fallback logic could, under the wrong error mode, attempt to recover an Ollama call by routing to Claude — which would violate the privacy invariant if the original call carried raw personal data.

**What was built**:

- New `agent/error_classifier.py` module with:
  - `ErrorCategory` enum: `rate_limit`, `context_overflow`, `network`, `auth`, `model_unavailable`, `unknown`
  - `DataSensitivity` enum: `local_only`, `sanitized`, `public`
  - `classify_error(exc)` — maps httpx + Anthropic SDK exceptions to categories via typed checks then string heuristics
  - `decide_fallback(category, data_sensitivity, model, config)` — returns a `FallbackDecision` with model, retry flag, backoff, and user-facing message
  - `ClassifiedLLMError` — raised to callers when retries are exhausted or error type doesn't permit retry
- `agent/llm.py` rewritten to:
  - Remove blind tenacity retry decorators from `_ollama_chat` / `_anthropic_chat`
  - Add `data_sensitivity` parameter to `ModelClient.chat()`
  - Implement classified retry loop (up to 3 attempts) using `classify_error` + `decide_fallback`
  - Enforce privacy invariant at two layers: `local_only` flag + `data_sensitivity` both block frontier routing
  - Fallback matrix: Claude rate-limit → local (sanitized/public only); Ollama unavailable → surface error; context overflow → raise for `core.py` to compress
- `agent/core.py` updated to:
  - Catch `ClassifiedLLMError` around the main LLM call and tool-call follow-up
  - On `CONTEXT_OVERFLOW`: force-compress and retry once (handles edge case where compressor threshold wasn't hit)
  - All other errors: surface the `user_message` from the classifier directly to the user
- Comprehensive test suite: `agent/tests/test_error_classifier.py` with privacy invariant regression tests

**Files touched**:

- New: `agent/error_classifier.py` ✅
- `agent/llm.py` (classified retry loop, `data_sensitivity` param, removed tenacity) ✅
- `agent/core.py` (import + wrap LLM calls with `ClassifiedLLMError` handler) ✅
- New: `agent/tests/test_error_classifier.py` ✅

**Success criteria**:

- ✅ API failures produce specific, actionable messages, not "something went wrong"
- ✅ Privacy audit: no `local_only` call ever routes to Claude under any failure mode (enforced at two layers + regression tests)
- ✅ Rate limits and context overflows recover automatically without user intervention

### Phase 3 success criteria (all met ✅)

- ✅ Morning brief completes in <50% of current wall time (3.1)
- ✅ Multi-hour conversations don't hit context limits (3.2)
- ✅ API failures produce useful, specific error messages (3.3)
- ✅ Privacy invariant preserved: no raw personal data ever routed to Claude under failure (3.3)
- ✅ Every change has tests (particularly for the privacy invariant — regression tests in test_error_classifier.py) (3.3)

---

## Phase 4 — Skill System

**Goal**: Encode repeatable workflows as structured, self-improving skills instead of hard-coded Python
**Estimated build**: ~1 week
**Status**: ✅ Complete
**Depends on**: Phase 3 complete (context compression matters — skills inflate the system prompt)

Context: Pepper currently reasons from scratch on every turn. The morning brief is hard-coded in `briefs.py`, but a "weekly financial check-in" or "draft an email to X based on our recent conversations" has to be rebuilt from first principles each time. A skill system lets you teach Pepper a workflow once and reuse it, with the workflow improving over time.

This is also the prerequisite for resurrecting deferred wishlist items cleanly: pre-event intelligence, deadline awareness, and enhanced commitment tracking all become skills rather than new hard-coded pipelines.

### 4.1 — SKILL.md Format ✅

Define a structured markdown format with YAML frontmatter:

```markdown
---
name: weekly_financial_review
description: Summarize spending trends, flag unusual charges, compare to budget
triggers: ["weekly review", "financial check", "how are we doing financially"]
tools: [parse_csv, search_memory, save_to_recall]
model: local  # local | frontier (respects privacy rules)
version: 1
---

## Workflow

1. Fetch last 7 days of transactions from the finance CSV cache
2. Categorize using prior-week categories as anchor
3. Flag any charge >2× the 30-day median in that category
4. Compare weekly total to rolling budget
5. Save summary to recall memory with tag `weekly_finance`
```

- Skills live in `skills/` directory (git-tracked, part of the repo)
- Loaded at startup, validated against a schema
- Skills can declare required tools — if any are missing, skill is disabled with a clear warning

**Reference**: Hermes Agent's SKILL.md pattern (see `skills/` and `optional-skills/` in the hermes-agent repo).

### 4.2 — Skill Injection ✅

- On each user turn, match against skills by:
  - Literal trigger phrases (fast path)
  - Semantic similarity using existing pgvector embeddings (slow path, runs in parallel with trigger match)
- Top N matches (default 3) are injected into the system prompt for that turn
- Injection is fenced: `<skill name="weekly_financial_review">…</skill>` so the model treats skill content as guidance, not user input
- If no skill matches, Pepper reasons from scratch as today — skills are opt-in guidance, not mandatory routing
- Phase 3.2 compression handles the prompt bloat

**Files touched**:

- New: `agent/skills.py` (loader, matcher, injector)
- `agent/core.py` (invoke matcher before prompt build)
- `agent/life_context.py` (extend `build_system_prompt` with skill block)

### 4.3 — Self-Improving Skills ✅

- After executing a turn that used a skill, a background task reviews the interaction:
  - Did the skill's workflow actually get followed? (check tool calls made)
  - Were there steps the model added or skipped?
  - Did the user give any correction or feedback?
- Review runs on local Ollama only (privacy-critical — review sees the raw conversation)
- Review proposes a structured diff to the skill's `SKILL.md`
- Diff surfaced in web UI under a "Skill improvements" queue; user approves / rejects
- Approved diffs committed to git, version incremented in frontmatter
- Never auto-applies — always requires human approval

**Files touched**:

- New: `agent/skill_reviewer.py` (background review task)
- `web/src/components/` (new "Skill improvements" view)
- `agent/scheduler.py` (schedule reviews post-turn, debounced)

### 4.4 — Initial Skill Library ✅

Seed the system by porting existing hard-coded workflows to skills:

- `morning_brief` — ports `agent/briefs.py` logic to a skill. Proves the pattern can replace hard-coded Python.
- `weekly_review` — currently a scheduled job; becomes a skill with the scheduler as the trigger
- `commitment_check` — currently `agent/briefs.py::CommitmentExtractor`; becomes a skill
- `draft_reply_to_contact` — uses contact enricher + recent messages across channels
- `prep_for_meeting` — first wishlist item (pre-event intelligence) ported as a skill rather than rebuilt

These five validate the skill system against real workflows, not toy examples.

### Phase 4 success criteria (all met ✅)

- ✅ Existing morning brief runs as a skill, not hard-coded Python (BriefFormatter deleted; scheduler calls pepper.chat())
- ✅ Adding a new skill requires zero code changes — just a SKILL.md file in skills/
- ✅ Skills can be improved post-execution via user-approved diffs (SkillReviewer + /skill-improvements API)
- ✅ 5 working skills: morning_brief, weekly_review, commitment_check, draft_reply_to_contact, prep_for_meeting

**Files added/changed**:
- New: `agent/skills.py` — loader, SkillMatcher (inject_into_prompt) ✅
- New: `agent/skill_reviewer.py` — post-turn reviewer, improvement queue (approve/reject) ✅
- New: `skills/morning_brief.md`, `weekly_review.md`, `commitment_check.md`, `draft_reply_to_contact.md`, `prep_for_meeting.md` ✅
- `agent/core.py` — skill init, injection before LLM call, background review task ✅
- `agent/scheduler.py` — all content-generation jobs now call pepper.chat() ✅
- `agent/briefs.py` — BriefFormatter deleted; CommitmentExtractor kept ✅
- `agent/main.py` — GET /skills, GET /skill-improvements, POST /skill-improvements/{id} ✅
- New: `agent/tests/test_skills.py` — 25 tests including scheduler trigger coverage ✅

---

## Phase 5 — MCP Integration

**Goal**: Pepper subsystems become true MCP services; external MCP servers become first-class tools
**Estimated build**: ~2 weeks
**Status**: ✅ Complete
**Depends on**: Phase 4 stable

Context: The existing subsystem architecture (`subsystems/*/client.py` + `agent/tool_router.py`) is already structured *for* MCP — tools are defined declaratively, subsystems are isolated, routing is explicit. This phase completes that vision and opens the door to the wider MCP ecosystem (Obsidian MCP, GitHub MCP, Linear MCP, etc.), which in turn makes several wishlist items almost free to build.

This is the largest architectural phase on the active roadmap. Sequencing matters — don't start until the skill system is proven.

### 5.1 — MCP Client ✅

Add a real MCP client to `agent/tool_router.py`:

- Configuration: `config/mcp_servers.yaml` lists MCP servers to connect to
- On startup: connect to each server, discover its tools, register them in the existing tool registry alongside native subsystem tools
- Authentication: MCP servers can have credentials (stored in `config/local/mcp_credentials.json`, gitignored)
- Tool calls from the model route identically whether the tool is native or MCP-backed — the router abstracts the difference
- Health checks: MCP servers are pinged alongside native subsystems; failures degrade gracefully

**Reference**: Hermes Agent's `tools/mcp_tool.py` (~1,050 lines — solid reference implementation for MCP client concerns including tool registration, auth, health).

**Files touched**:

- New: `agent/mcp_client.py`
- `agent/tool_router.py` (merge MCP tool registration with native)
- `config/local/mcp_credentials.json` (gitignored)
- New: `config/mcp_servers.yaml`

### 5.2 — Subsystems Expose MCP ✅

Convert Pepper's own subsystems from in-process Python modules to standalone MCP servers:

- Each subsystem (`subsystems/calendar/`, `subsystems/communications/*`) gets an MCP entry point
- Subsystems run as standalone processes on dedicated ports (8100–8104) — architecture already reserves these
- Original docker-compose stack adds the subsystem servers as services
- `tool_router.py` consumes them via the same MCP client from 5.1 — no special case
- Benefit: subsystems become independently deployable, testable, and replaceable without touching core. The dependency-inversion goal that's been in the architecture docs since day one finally becomes real.

This is mechanical but meaningful — it's what turns "structured like MCP" into "is MCP".

### 5.3 — Privacy-Preserving MCP ✅

**Core principle**: MCP servers that Pepper connects to may receive data. This must be controlled, not trusted.

Implementation:

- Every MCP server in `config/mcp_servers.yaml` must declare a trust level: `local | trusted | external`
  - `local` — runs on the same machine, inside Pepper's trust boundary (the subsystem servers from 5.2 qualify)
  - `trusted` — self-hosted, user-operated, non-public (e.g., a self-hosted Notion MCP server)
  - `external` — any third-party MCP service
- Routing rules, enforced in the tool router:
  - Tools that surface raw personal data (iMessage content, WhatsApp messages, raw email bodies) can **only** call `local` MCP servers
  - `trusted` servers can receive sanitized / structured data (e.g., contact names but not message bodies)
  - `external` servers follow the same rules as Claude API today — summaries only, never raw data
- Every MCP tool call logged to the audit trail with: server, trust level, data classification of inputs
- Violations (attempted cross-trust routing) raise and are surfaced prominently — this is a privacy bug, not a runtime warning

This reuses the `data_sensitivity` tags added in Phase 3.3 — the error classifier and MCP router share the same privacy invariant.

**Files touched**:

- `agent/mcp_client.py` (trust-level enforcement)
- `agent/tool_router.py` (classification lookup)
- `logs/` (audit log format extended)
- New tests in `agent/tests/` specifically for cross-trust routing attempts

### 5.4 — Pepper as MCP Server (optional) ✅

Expose Pepper's own tools as an MCP server that other agents can connect to:

- Authentication via API keys (already have the auth middleware from `agent/auth.py`)
- Access control: per-key tool allowlists (e.g., Claude Desktop gets memory search + calendar read, but not memory write or contact profiles)
- Enables: Claude Desktop, Claude Code, Cursor, and other MCP clients can use Pepper's memory, calendar, and contact enrichment as tools in their own conversations
- This is where the "context is the moat" philosophy pays off — other agents become smarter by borrowing Pepper's context, without seeing the raw data

**Why optional**: 5.1–5.3 deliver value even without this. 5.4 is a multiplier — only build it if there's a concrete agent you want to give limited access to Pepper's context.

### Phase 5 success criteria (all met ✅)

- ✅ External MCP servers configurable and usable from Pepper via `config/mcp_servers.yaml`
- ✅ Subsystems expose MCP entry points (`subsystems/calendar/mcp_server.py`, `subsystems/communications/mcp_server.py`)
- ✅ Privacy classification enforced: 59 regression tests confirm raw iMessage/WhatsApp/email/Slack data cannot reach external or trusted MCP servers
- ✅ Audit log shows full trace of every MCP call with trust level and data classification (`agent/mcp_audit.py`)
- ✅ Pepper exposes tools as MCP server with NEVER_EXPOSE guardrail + per-key allowlists (`agent/mcp_server.py`)

**Files added/changed**:
- New: `agent/mcp_client.py` — MCP client (server lifecycle, tool discovery, call routing) ✅
- New: `agent/mcp_audit.py` — privacy enforcement, trust boundaries, audit logging ✅
- New: `agent/mcp_server.py` — Pepper-as-MCP-server with NEVER_EXPOSE + allowlists ✅
- New: `subsystems/mcp_base.py` — base MCP server wrapper for subsystems ✅
- New: `subsystems/calendar/mcp_server.py` — calendar as standalone MCP server ✅
- New: `subsystems/communications/mcp_server.py` — communications as standalone MCP server ✅
- New: `config/mcp_servers.yaml` — external MCP server configuration ✅
- New: `config/mcp_server_access.yaml` — Pepper MCP server access control ✅
- `agent/tool_router.py` — unified routing: native + MCP tools, trust enforcement ✅
- `agent/core.py` — MCP client init, MCP tool injection, shutdown ✅
- `agent/main.py` — `/mcp/servers`, `/mcp/tools` endpoints, graceful shutdown ✅
- `pyproject.toml` — `mcp>=1.9` dependency ✅
- New: `agent/tests/test_mcp_client.py` — 12 tests ✅
- New: `agent/tests/test_mcp_privacy.py` — 33 tests (privacy regression suite) ✅
- New: `agent/tests/test_mcp_router.py` — 14 tests ✅

## Phase 6 — Intent And Capability Reliability

**Goal**: Make Pepper reliably understand what the user is asking, know which sources and tools are actually available, and choose the right action path before answering — and then exercise the judgment a human executive assistant would exercise on top of that foundation
**Estimated build**: ~5 weeks (2 weeks for 6.1–6.4; ~3 weeks for 6.5–6.7)
**Status**: ✅ All of Phase 6 complete (6.1–6.7 shipped 2026-04-17)
**Depends on**: Phase 5 foundations in place; can begin earlier in a limited in-process form if MCP slips

Context: Pepper now has the core ingredients of an executive assistant — life context, memory, communications, calendar, skills, and MCP-ready routing — but it still too often misses the point of the user's first question. The current runtime relies on a mix of broad "heavy" classification, source-specific keyword triggers, prompt instructions, and a flat tool list. That is good enough for obvious requests and brittle for natural language. Phase 6 is the reliability phase: it turns Pepper from "has the tools" into "consistently uses the right tools for the right ask" — and then, in 6.5–6.7, from "consistently routed" into "reliably exercises executive judgment."

The first four subphases (6.1–6.4) were intentionally narrow: a real router, prompt/tool contract hygiene, an explicit capability registry, and an EA-focused eval harness. They fixed the blockers that kept Pepper from parsing ordinary executive-assistant language.

6.5–6.7 extend Phase 6 rather than opening a new phase, because the gaps they close are part of the same question: "does Pepper understand the ask, and does it act like someone who is paid to get it right?" They sit on top of 6.1–6.4 and are what turn reliable routing into reliable judgment — the difference between a switchboard and an assistant.

Subphases 6.1–6.4 address the three routing-level blockers:

- no real first-pass intent router
- drift between prompt claims and actual tool contracts
- no explicit capability registry that distinguishes configured, reachable, permission-blocked, and unavailable sources

### 6.1 — Real Intent Router ✅

**Problem**: Pepper does not cleanly separate "what is the user asking?" from "which tool should I call?" The current path combines a coarse heavy/light decision with substring heuristics (`email`, `texts`, `whatsapp`, `slack`, etc.). This misses ordinary EA phrasing like "Did Sarah send anything?", "Who do I owe replies to?", or "What came in this morning?" and can also route to the wrong source.

**Approach**:

- Add a first-pass `QueryRouter` that emits a structured routing decision before prompt assembly or tool execution
- Router output should include:
  - `intent_type` (`capability_check`, `inbox_summary`, `action_items`, `person_lookup`, `conversation_lookup`, `schedule_lookup`, `cross_source_triage`, `general_chat`, etc.)
  - `target_sources` (`email`, `imessage`, `whatsapp`, `slack`, `calendar`, `memory`, `mixed`, `unknown`)
  - `action_mode` (`answer_from_context`, `call_tools`, `ask_clarifying_question`)
  - `time_scope`, `entity_targets`, and `needs_clarification`
- Use deterministic rules for obvious queries first
- Use a small local LLM classifier only for ambiguous cases
- Persist the router decision to logs so evals can compare the inferred route with the eventual tool usage
- Make `agent/query_intents.py` a supporting library for the router, not the routing system itself

**Files touched**:

- New: `agent/query_router.py`
- `agent/core.py` (routing step runs before proactive fetch and prompt build)
- `agent/query_intents.py` (reduced to shared helper utilities)
- New tests in `agent/tests/test_query_router.py`

**Success criteria**:

- ✅ "Did Sarah send anything?" routes to a person/source lookup path, not generic chat
- ✅ "Who do I owe replies to?" routes to cross-source comms triage
- ✅ "What came in this morning?" routes to an inbox/messages summary path
- ✅ The router can explain in logs why it chose a path

**Files added**:
- New: `agent/query_router.py` — `QueryRouter`, `RoutingDecision`, `IntentType`, `ActionMode`; deterministic 9-rule priority chain ✅
- `agent/core.py` — routing step runs before depth selection; capability-check short-circuit; entity-target logging ✅

### 6.2 — Prompt/Tool Contract Cleanup ✅

**Problem**: Pepper's prompt and capability prose can drift from the real tool registry. When the model is told about tools that do not exist, stale tool names, or conflicting descriptions, smaller local models are more likely to apologize, hallucinate, or refuse instead of trying the correct call.

**Approach**:

- Make the real tool registry the single source of truth for capability text shown to the model
- Generate the capability block in the system prompt from actual registered tools rather than hand-written strings
- Add a validation step that fails tests if the prompt mentions nonexistent tool names
- Tighten tool descriptions so each tool says:
  - what source it covers
  - when to use it
  - when not to use it
  - one short example for ambiguous language
- Remove stale hard-coded names and references from prompt docs and inline capability text

**Files touched**:

- `agent/life_context.py` (generate capability text from registry)
- `agent/core.py` (pass the active registry into prompt assembly)
- `agent/*_tools.py` (normalize descriptions)
- New tests in `agent/tests/` for prompt/tool registry consistency

**Success criteria**:

- ✅ No prompt text references tools that are not actually registered (regression test in `test_query_router.py::test_validate_prompt_tool_references_no_stale_names`)
- ✅ Tool descriptions are source-specific and non-overlapping
- ✅ Capability questions no longer depend on prompt folklore; they are grounded in the live registry

**Files changed**:
- `agent/life_context.py` — `build_capability_block(registry=None)` generates capability text from actual tool names; `validate_prompt_tool_references()` for test validation; `build_system_prompt()` updated to call `build_capability_block()`; fixed stale names (`search_calendar_events` → `get_calendar_events_range`, `get_slack_messages` → `get_slack_channel_messages`) ✅

### 6.3 — Explicit Capability Registry ✅

**Problem**: Pepper has tools, but it does not have a single runtime view of whether a source is actually usable right now. There is a meaningful difference between "tool exists", "account not configured", "permission missing", "temporarily unavailable", and "disabled by policy". Today that state is spread across prompt instructions, tool errors, and incidental health checks.

**Approach**:

- Add a `CapabilityRegistry` that tracks per-source status:
  - `available`
  - `not_configured`
  - `permission_required`
  - `temporarily_unavailable`
  - `disabled`
- Populate it at startup and refresh it on a schedule or after relevant failures
- Feed capability state into the new `QueryRouter`
- Answer capability questions from the registry first, not by asking the model to remember what tools exist
- Standardize user-facing error messages so Pepper says precise things like:
  - "Yes, I can read email; Yahoo is configured and Gmail is not."
  - "I can access iMessage, but Full Disk Access has not been granted."
  instead of vague refusals

**Files touched**:

- New: `agent/capability_registry.py`
- `agent/core.py` (capability checks before tool execution; capability answers use registry)
- `agent/tool_router.py` or MCP bootstrap path (source health/config reporting)
- New tests in `agent/tests/test_capability_registry.py`

**Success criteria**:

- ✅ Capability questions are answered deterministically and correctly
- ✅ Permission/configuration problems surface as precise status, not generic failure
- ✅ Pepper stops saying it cannot access data when the tool exists but has not yet been tried

**Files added**:
- New: `agent/capability_registry.py` — `CapabilityRegistry`, `CapabilityStatus`, `SourceCapability`; `populate(config)` probes all 8 sources at startup ✅
- `agent/core.py` — registry populated before system prompt build; `_answer_capability_check()` short-circuit; `/capabilities` REST endpoint ✅
- `agent/main.py` — `GET /capabilities` endpoint ✅

### 6.4 — Evaluation Harness For Exec Assistant Reliability ✅

**Problem**: The current tests mostly verify helper behavior and happy-path tool execution. They do not measure whether Pepper interprets real executive-assistant asks correctly at the top of the funnel.

**Approach**:

- Add a benchmark set focused on paraphrase-heavy EA queries
- Include cases for:
  - capability checks
  - inbox/message summaries
  - action-item and follow-up detection
  - person-centric lookups
  - ambiguous source wording
  - mixed-source follow-ups
  - partial subsystem failure
- Track metrics:
  - intent classification accuracy
  - source-routing accuracy
  - wrong-source answer rate
  - false "cannot access" rate
  - unnecessary clarification rate
- Seed the eval set with concrete prompts such as:
  - "Did my mom send anything?"
  - "Anything important overnight?"
  - "Who do I owe replies to?"
  - "Can you check my texts?"
  - "Do you have access to my messages?"

**Files touched**:

- New: `agent/tests/test_exec_assistant_eval.py`
- New: `docs/` note describing the eval corpus and scoring rubric

**Success criteria**:

- ✅ Routing regressions are caught before they reach users
- ✅ Pepper's EA-specific understanding quality is measurable over time
- ✅ Phase 6 changes can be tuned against real natural-language failures, not anecdotes

**Files added**:
- New: `agent/tests/test_exec_assistant_eval.py` — 30-case eval corpus across capability checks, inbox summaries, action items, person lookups, schedule lookups, general chat; metric summary printed on every run ✅
- New: `agent/tests/test_query_router.py` — 70 tests including prompt/tool registry regression ✅
- New: `agent/tests/test_capability_registry.py` — 33 tests including async populate() probes ✅

### Phase 6.1–6.4 success criteria (all met ✅)

- ✅ Pepper correctly identifies the user's intent and likely source in ordinary language
- ✅ Pepper stops falsely claiming it cannot read email/messages/calendar when tools exist
- ✅ Capability answers are grounded in live system state, not prompt memory
- ✅ Exec-assistant queries about inbox, messages, schedule, and follow-ups feel reliably routed rather than brittle
- ✅ 570 tests passing, zero regressions from prior phases

---

### The executive-judgment gap (motivating 6.5–6.7)

Using 6.1–6.4 in practice surfaced a second class of failure that routing alone cannot fix. Pepper now understands *what is being asked* and *which tools exist* — but it still behaves like a switchboard rather than an assistant in several recurring, specific ways:

- it routes to a source even when the capability registry knows that source is unavailable
- it fails to extract possessives, nicknames, or event-relative time scopes ("since Thursday", "before my 3pm")
- it treats every turn as stateless — "any urgent?" right after an email question does not inherit email context
- the capability registry is populated once at startup and never refreshed, so a permission revoked mid-session goes unnoticed
- registry status is not surfaced to the LLM or to the web UI, so graceful fallback reads as a generic apology rather than a precise explanation
- commitments are captured to memory but never followed through — "I'll reply tonight" disappears
- no draft-and-queue path exists for outbound messages, so Pepper either sends immediately or does nothing
- there is no priority grading that adapts to the user's actual attention patterns
- the router can set `needs_clarification` but nothing ever reads it, so Pepper guesses instead of asking

6.5–6.7 close these specific gaps. They are the smallest set of changes that turn Pepper from a well-routed chatbot into something that feels like an EA.

### 6.5 — Router Hardening ✅

**Problem**: The router introduced in 6.1 is deterministic and reliable for common phrasings, but a handful of known holes cause real misroutes in natural EA language. These are all narrow, contained problems — none of them require an LLM classifier, and fixing them inside the deterministic layer keeps the router fast and auditable.

**Approach**:

- **Registry-aware routing**: `QueryRouter.route()` already accepts a `CapabilityRegistry` but ignores it. When the registry marks a requested source `not_configured` / `permission_required` / `temporarily_unavailable`, the router should narrow `target_sources` to the reachable set, or — if nothing reachable — set `needs_clarification=True` with a reasoning string the UI can render.
- **Possessive and indirect entity extraction**: add `\b([A-Z][a-z]+)'s\b` and `"[name]'s [noun]"` patterns alongside the existing kinship extraction so "Mike's latest email" and "reply to Sarah's thread" produce the right `entity_targets`.
- **Relative and event-relative time scope**: extend `_TIME_SCOPE_TABLE` to cover "since [day-of-week]", "over the last few days", "in the last N hours", and event-anchored phrases like "before my 3pm" (resolved against the calendar client when available).
- **Short conversation carry-over**: when the current turn has no source terms but the previous 1–2 user turns did, inherit those sources with reduced confidence. This makes follow-ups like "anything urgent?" after an email question work correctly, and is easier to reason about than full conversational state.
- **Multi-intent split**: when a single message mentions two independent source/time/entity targets joined by " and "/"; ", emit a list of routing decisions rather than one. `core.py` dispatches each in parallel and merges the responses.

**Files touched**:

- `agent/query_router.py` (registry consultation, possessive regex, time-scope table extension, carry-over, multi-intent)
- `agent/core.py` (pass registry into router; handle multi-decision output)
- `agent/tests/test_query_router.py` (expanded cases for each new pattern)
- `agent/tests/test_exec_assistant_eval.py` (new eval cases for possessives, relative time, carry-over)

**Success criteria**:

- Queries targeting unavailable sources either re-route or surface a clarifying question rather than failing at tool time
- Possessive and event-relative phrasings route as cleanly as their direct equivalents
- "Anything urgent?" following an email question routes to email, not to cross-source triage
- Multi-intent queries produce multi-decision output, verified in evals

### 6.6 — Capability Registry As A Live Organ ✅

**Problem**: The registry from 6.3 is populated at startup and then frozen. A permission granted or revoked mid-session does not propagate. The LLM never sees registry state, so when routing falls through to general chat, Pepper's apology for a failed tool call reads as generic ("something went wrong") rather than precise ("WhatsApp needs Full Disk Access"). The `/capabilities` REST endpoint exists but is not rendered in the web UI.

**Approach**:

- **Runtime refresh on failure**: wrap tool execution in `agent/tool_router.py` so that tools returning `{"error": "..."}` with recognizable permission/auth patterns trigger `CapabilityRegistry.update_status()`. Classification reuses the `ErrorCategory` taxonomy from Phase 3.3 — `auth`, `permission_required`, `temporarily_unavailable` all map cleanly.
- **Prompt injection of registry state**: `build_system_prompt()` already receives the registry. Extend `build_capability_block()` to include a compact status line for each source (`available` / `permission_required` + reason / `not_configured`). The model can then answer "why" questions precisely without a separate capability-check short-circuit.
- **Web UI surface**: add a small "Capabilities" panel to the web app that calls `GET /capabilities` and renders status per source with a remediation hint. This is the user-facing expression of the same truth the router and prompt consume.
- **Periodic re-probe**: `CapabilityRegistry.populate()` gets a `refresh()` wrapper called on a slow scheduler (every ~15 minutes) and on-demand after failure. Covers the case where a user grants FDA without restarting Pepper.

**Files touched**:

- `agent/capability_registry.py` (refresh hook, failure-driven update mapping)
- `agent/tool_router.py` (classify tool errors → update registry)
- `agent/life_context.py` (registry-aware capability block in system prompt)
- `agent/scheduler.py` (periodic refresh job)
- `web/src/components/` (new Capabilities panel)
- `agent/tests/test_capability_registry.py` (runtime update paths, prompt injection assertions)

**Success criteria**:

- A permission revoked mid-session updates the registry within one tool call
- The system prompt reflects current registry state, and the LLM's explanations for unavailable sources are precise (no generic "something went wrong")
- The web UI shows current capability status with remediation text
- Existing privacy-boundary tests from 5.3 still pass unchanged

### 6.7 — Executive Judgment Behaviors ✅

**Problem**: Even with perfect routing and honest capability reporting, Pepper still behaves like a switchboard rather than an assistant. It does not draft and queue outbound messages, does not follow through on the commitments it captures, does not ask clarifying questions when the routing is ambiguous, and does not grade attention requests by the user's actual response patterns. These are the specific gaps between "uses the right tools" and "acts like someone paid to get things right."

This is the largest of the three extension subphases and the one with the most user-visible value. It should only begin after 6.5 and 6.6 are stable — each behavior here depends on the router and registry being reliable.

**Approach**:

- **Draft-and-queue outbound actions**: all outbound writes (email send, message send, event create) route through a pending-actions queue rather than executing directly. Queued drafts are surfaced in Telegram and the web UI with explicit approve/edit/reject controls. Uses the existing MCP write-approval gating from Phase 5 as the substrate. The `draft_reply_to_contact` skill becomes the first consumer.
- **Commitment follow-through**: `CommitmentExtractor` already persists commitments; add a scheduler job that re-surfaces unresolved commitments at the relevant time (morning brief for "today" commitments, end-of-day for "by EOD", next-morning for "tonight"). On user response, mark resolved. This turns memory into action.
- **Clarifying-question path**: `RoutingDecision.needs_clarification` currently a dead field — wire it end-to-end. When the router sets it True (either from ambiguity or from 6.5's registry routing), `core.py` bypasses the LLM and emits a deterministic clarifying question with the candidate sources/entities as options. The user's reply feeds a second routing pass with those options pre-resolved.
- **Priority grading v1 (no learning yet)**: a `PriorityGrader` that combines signals already collected — communication health (last contact per channel), life-context VIPs, keyword urgency, calendar proximity — into an `urgent | important | defer | ignore` tag on each item surfaced in inbox summaries and cross-source triage. No adaptive learning in v1; that becomes a follow-up once the tagging is trusted.
- **Tool disambiguation via routing intent**: now that routing is richer, replace the ad-hoc early-exit for action-items vs summary in `core.py` with an explicit `intent → tool` mapping, and remove overlapping tool triggers from skill files that 6.5's router already covers.

**Deferred from this subphase (but noted for visibility)**:

- Adaptive priority learning from actual response latency per contact — needs the static grader shipped and instrumented first
- Ambient awareness (suppress morning brief during a meeting, batch during focus mode) — depends on calendar-presence detection and focus-mode signals that are not yet wired

**Files touched**:

- New: `agent/pending_actions.py` (draft queue, approve/edit/reject)
- New: `agent/commitment_followup.py` (scheduler-driven re-surfacing)
- New: `agent/priority_grader.py` (non-learning v1 grader)
- `agent/query_router.py` (wire `needs_clarification` emissions)
- `agent/core.py` (clarifying-question dispatch; priority-grader integration in summary paths)
- `agent/scheduler.py` (commitment follow-through job)
- `skills/draft_reply_to_contact.md` (route through pending-actions)
- `web/src/components/` (pending-actions panel, clarifying-question prompt)
- `agent/tests/` (new suites for pending actions, commitment follow-through, clarifying-question path, priority grader, end-to-end EA scenarios)

**Success criteria**:

- No outbound write executes without user approval or a durable auto-approve flag for a specific action type
- Unresolved commitments from yesterday surface in today's brief and are marked resolved on user action
- Ambiguous queries ("check messages") produce a clarifying question, not a guess
- Items in inbox summaries are ranked by a priority tag that matches the user's intuition on a new eval slice (≥20 cases)
- The ad-hoc action-items early-exit in `core.py` is replaced by intent-driven tool selection and removed

### Phase 6.5–6.7 success criteria (rolls up to Phase 6 when all met) ✅

- ✅ Router respects live capability state and asks clarifying questions instead of guessing
- ✅ Capability registry is a live organ: failures refresh it (classify_tool_error), periodic scheduler re-probe, UI Capabilities panel renders it
- ✅ Pepper drafts-and-queues outbound writes via `PendingActionsQueue`, follows through on captured commitments via `CommitmentFollowup`, and grades attention by user-specific signals via `PriorityGrader`
- ✅ 630 tests passing (26 new tests for 6.7: pending actions, commitment follow-through, clarification, priority grader), zero regressions

**Files added in 6.5–6.7**:

- `agent/pending_actions.py` — in-memory draft queue with approve/edit/reject + executor callback
- `agent/commitment_followup.py` — slot-based re-surfacing (morning/afternoon/evening cues)
- `agent/priority_grader.py` — non-learning rule-based grader + life-context VIP extraction
- `agent/tests/test_pending_actions.py`, `test_commitment_followup.py`, `test_priority_grader.py`, `test_clarification.py`
- `web/src/components/Status.tsx` — Capabilities panel + Pending Actions panel
- `agent/scheduler.py` — `capability_refresh` job (every 15m) + `commitment_followup` job (8:05/17:05/22:05)
- `agent/main.py` — `/capabilities/refresh`, `/pending-actions`, `/pending-actions/{id}` endpoints
- `agent/core.py` — `needs_clarification` short-circuit, `PendingActionsQueue` instance wired
- `agent/query_router.py` — possessives, relative/event-relative time scopes, carry-over, multi-intent split, registry-aware narrowing
- `agent/capability_registry.py` — `classify_tool_error()`, `refresh()` wrapper

---

## After Phase 6

With runtime, skills, and MCP in place, the wishlist items become dramatically cheaper to build:

- **Knowledge layer** → most items become MCP server integrations (Obsidian MCP, filesystem MCP) + skills
- **Health & Finance** → CSV/export parsing becomes a skill; a single Health MCP server handles all sources (Apple Health, Oura Ring, Garmin, Whoop, etc.) via per-source adapters
- **Pre-event intelligence / deadline awareness** → already listed as planned skills in 4.4
- **Maintenance & Security** → scheduled skills running on the background scheduler; the Phase 3.3 error classifier is the foundation

At that point, revisit [WISHLIST.md](WISHLIST.md) and pull items back onto the active roadmap based on what you're actually missing day-to-day.

---

## The Long Game — What Pepper Drives

Once the active roadmap is stable, the system begins driving its own evolution:

**Pepper recommends improvements to the People subsystem** based on observed gaps in people/relationship data. When Pepper repeatedly fails to answer a relationship question, it logs that as a People subsystem improvement opportunity.

**Pepper recommends new skills** when it notices a workflow being reinvented from scratch repeatedly. The skill reviewer (4.3) is the first step toward this.

**Pepper recommends new MCP integrations** when patterns suggest a data source it can't reach. "You keep asking about your Notion workspace — should I connect to it?"

**Pepper evolves its own life context** as it learns more about your patterns, values, and what matters to you. The document is no longer just what you wrote — it incorporates what Pepper has observed.

**Pepper begins managing others** — over time, it can help draft family communications, prepare you for difficult conversations, track how family members are doing based on all available signals.

This is the Pepper arc: starts as a junior assistant, earns trust, becomes something that genuinely knows you.

---

## What Pepper Will Tell Us to Build

The honest answer is that phases 3–6 priorities will be reshaped by actual experience using phases 1–2. The roadmap is a hypothesis. Usage is the test.

If actual usage surfaces a capability gap that's on [WISHLIST.md](WISHLIST.md), pull it back onto the active roadmap. The wishlist isn't frozen — it's a backlog.

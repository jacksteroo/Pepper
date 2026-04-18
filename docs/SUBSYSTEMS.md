# Pepper — Subsystem Specifications

Each subsystem is a focused data and tool service. Pepper Core calls them; they don't call each other. Each is independently replaceable.

---

## Subsystem 1: People

**Status**: Deferred future phase  
**Directory**: `subsystems/people/` — currently a stub with interface spec only

### What it does

Maintains the relationship graph: who's in your life, how they're connected, conversation history across platforms, relationship health signals, scoring, and semantic search over people and communications.

### Why the People subsystem is not Phase 1

Relationship intelligence is useful, but the exact capabilities Pepper needs should be driven by real usage. Better to learn what matters from actual questions and workflows before expanding this layer.

### What Pepper will eventually drive in the People subsystem

- Populate contacts based on people mentioned in iMessage, email, calendar
- Recommend relationship health improvements based on Pepper's broader life context
- Surface relationship insights Pepper can't surface from message data alone
- Flag when a person becomes relevant to an active goal or challenge

### Integration interface (when ready)

Tool names below are illustrative — the actual interface will be driven by what Pepper needs from real usage.

```text
GET  /tools                          # Available people tools
POST /tools/get_contact_details      # Person profile
POST /tools/get_outreach_recs        # Who to reach out to
POST /tools/get_dormant_contacts     # Relationships going quiet
POST /tools/search_messages_semantically  # Semantic message search
```

---

## Subsystem 2: Time (Calendar)

**Status**: ✅ Complete — Phase 2  
**Directory**: `subsystems/calendar/`

### What it does

Reads Google Calendar events across all linked accounts and makes them available to Pepper. Knows what's coming up, who you're meeting, and surfaces schedule conflicts.

### Data sources

- **Google Calendar API** via OAuth2 — real-time queries, no local sync required
- Multi-account support: personal, work, and any named accounts configured in `config/local/accounts.json`
- Unified Google auth (`subsystems/google_auth.py`) shared with Gmail — one token per Google account covers both
- If a Pepper account id differs from the shared Google token slug, set `google_account` on that email account in `config/local/accounts.json`

### Implemented tools

```text
get_upcoming_events(days=7, calendar_filter=None)   # next N days across all accounts
get_calendar_events_range(start_date, end_date)     # arbitrary past/future range
list_calendars()                                     # all calendars across all accounts
```

### Configuration

- Per-account calendar selection and display name overrides: `subsystems/calendar/preferences.py`
- Excluded calendar IDs per account: `config/local/accounts.json`
- Proactive context injection: calendar context automatically surfaced when schedule-related keywords appear in conversation

### Deferred (planned as Phase 4 skills)

- Pre-event intelligence: prep brief 30 minutes before events
- Deadline tracking from Slack cross-reference
- Commitment tracking from promise language in conversations

---

## Subsystem 3: Communications (Messages + Email)

**Status**: ✅ Complete — Phase 2  
**Directory**: `subsystems/communications/`

### What it does

Reads messages and email across all channels — locally where possible, via API where needed — and makes the insights available to Pepper. All raw content is processed locally; only summaries reach the frontier LLM.

### Data sources and implemented tools

**Gmail** (`gmail_client.py`) — OAuth2, shared token with Calendar:
```text
get_recent_emails(account, days, max_results)
search_emails(account, query)           # supports full Gmail search syntax
get_email_unread_counts(account)
```

**Yahoo Mail / IMAP** (`imap_client.py`):
```text
get_recent_emails(account, days, max_results)
get_email_unread_counts(account)
```

**iMessage** (`imessage_client.py`) — reads `~/Library/Messages/chat.db` directly, no API:
```text
get_recent_imessages(days, limit)
get_imessage_conversation(contact, days)
search_imessages(query)
```
Requires Full Disk Access. Parameterized SQL queries throughout (injection-safe). Graceful degradation if permission not granted.

**WhatsApp** (`whatsapp_client.py`) — reads local `ChatStorage.sqlite` directly:
```text
get_recent_whatsapp_chats(days, limit)
get_whatsapp_chat(contact_or_group, days)
search_whatsapp(query)
get_whatsapp_groups()
```
Fallback: parses `.txt` chat export files. Identifies personal vs. group chats.

**Slack** (`slack_client.py`) — Slack Bot API, read-only. Requires `SLACK_BOT_TOKEN` in `.env`:
```text
search_slack(query)
get_slack_channel_messages(channel, days)
get_slack_deadlines(days)              # regex-based deadline detection
list_slack_channels()
```

**Contact enrichment** (`contact_enricher.py`) — cross-references contacts across all channels:
```text
get_contact_profile(name_or_handle)    # last contact per channel, dominant channel
find_quiet_contacts(days_threshold)    # surfaces people who've gone quiet
search_contacts(query)
```

**Communications health dashboard** (`agent/comms_health_tools.py`):
```text
get_comms_health_summary()
get_overdue_responses()
get_relationship_balance_report()
```
Integrated into morning brief (1–2 signals surfaced daily). REST endpoint: `GET /comms-health`. Web UI tab: "Relationships".

### Privacy architecture

```text
Raw iMessage / WhatsApp / email → local processing only
                                → summaries surfaced to frontier LLM
                                → raw content never transmitted, never stored in PostgreSQL
```

All SQLite reads are read-only. No data is copied out of the source databases.

---

## Subsystem 4: Knowledge

**Status**: Planned — post Phase 5  
**Directory**: `subsystems/knowledge/`

### What it does

Indexes your notes, documents, and decision history so Pepper can reference what you know and have decided.

### Data sources

- **Obsidian vault** (if used): local markdown files
- **Apple Notes**: export to markdown (or read from SQLite at `~/Library/Group Containers/.../NoteStore.sqlite`)
- **Plain markdown files**: any directory you designate
- **Key documents**: PDFs, contracts, plans
- **Decision log**: structured log of significant decisions (Pepper helps maintain this)

### Key capabilities

- Semantic search over all notes and documents
- "What did I decide about X?" — retrieve past decisions with context
- "What do I know about Y?" — retrieve relevant notes
- Connect ideas across notes (topics that appear in multiple contexts)
- Temporal awareness: "what was I thinking about in March?"

### Decision Log format

```json
{
  "date": "2026-04-11",
  "decision": "...",
  "context": "...",
  "options_considered": ["...", "..."],
  "choice": "...",
  "reasoning": "...",
  "revisit_date": "2026-10-11"
}
```

Pepper proposes decision log entries through conversation. You accept, edit, or reject.

---

## Subsystem 5: Health

**Status**: Planned — post Phase 5  
**Directory**: `subsystems/health/`

### What it does

Reads health data from multiple sources and gives Pepper awareness of your physical patterns and energy levels. Not medical — context for better life recommendations.

### Data sources

- **Apple Health export**: Health app → Export All Health Data → XML; process incrementally using `startDate` filtering
- **Oura Ring**: sleep stages, HRV, readiness scores via local export or API
- **Garmin**: activity, GPS, heart rate via Garmin Connect export
- **Whoop**: strain, recovery, sleep via export
- **Other wearables**: any device that exports CSV or JSON (Fitbit, Polar, etc.)
- Categories across all sources: steps, sleep, heart rate, HRV, exercise, body metrics

### Privacy

- All health data stays on the machine — raw metrics are never transmitted
- Raw metrics stored locally; Pepper receives summaries only
- API access (e.g. Oura Ring API) is acceptable where the API key lives locally and data flows machine → source → machine; no health data is sent to third-party cloud services

### What Pepper does with this

- Understands your typical energy levels by time of day and day of week
- Notices anomalies: "your sleep has been poor this week"
- Connects health patterns to performance: "you tend to make better decisions when..."
- Gentle prompts, not medical advice

---

## Subsystem 6: Finance

**Status**: Planned — post Phase 5  
**Directory**: `subsystems/finance/`

### What it does

Reads financial data from CSV exports and gives Pepper basic awareness of your financial situation. Goal is life context, not financial management.

### Data sources

- **Bank CSV exports**: most banks offer transaction history as CSV
- **Investment accounts**: similar export functionality
- **Manual entries**: significant transactions Pepper captures through conversation
- **No direct banking API connections** — CSV only, simpler and more private

### What Pepper does with this

- "How are we doing financially?" — basic household P&L
- "Are we on track for [goal]?" — trajectory analysis
- Spending pattern awareness to inform recommendations
- Flag anomalies: unusual charges, significant changes

### What Pepper doesn't do

- Tax advice
- Investment decisions
- Replace a financial advisor
- Any automated financial actions (read-only, always)

---

## Subsystem Interface Standard

All subsystems implement this HTTP interface:

```text
GET  /health
     Returns: { "status": "ok|degraded|down", "last_sync": ISO8601, "record_count": int }

GET  /tools
     Returns: MCP-compatible tool definition list

POST /tools/{tool_name}
     Body: { "arguments": { ... } }
     Returns: { "result": ..., "error": null }

GET  /status
     Returns: detailed subsystem status for dashboard display
```

Subsystems run as independent processes on localhost with different ports:

- Calendar: 8100
- Communications: 8101
- Knowledge: 8102
- Health: 8103
- Finance: 8104
- People: TBD

# Pepper — Wishlist

Deferred capabilities from the original roadmap. These are things we want Pepper to eventually do, but they're not on the active build path.

The active roadmap (see [ROADMAP.md](ROADMAP.md)) now focuses on agent runtime upgrades, a skill system, and MCP integration — foundations that make everything below easier to build when their time comes.

Revisit this list quarterly, or whenever actual usage surfaces a specific gap that matches one of these items. Pepper tells us what to build next.

---

## Time Layer — Pre-Event & Deadline Intelligence

> _Deferred from original Phase 3. Calendar reader `3.1` already shipped and lives in ROADMAP.md._

### Pre-Event Intelligence

- 30 minutes before any calendar event: Pepper pushes a briefing
- **Work events** (meetings with people): context on who you're meeting, last conversation, open items, suggested talking points
- **Personal events** (family activities): logistics check — "Do you have what you need? Kids' gear ready? Anything to grab before leaving?"
- Pulls context from memory and (later) the People subsystem
- Different prep modes: conversation prep vs practical readiness

**Why deferred**: Once the skill system (active Phase 4) is in place, this becomes a `prep_for_meeting` skill rather than hard-coded Python. Building it now would mean rewriting it later.

### Deadline Awareness

- Slack integration already done in Phase 2; extraction of deadline language is partial
- Pepper flags upcoming deadlines from Slack conversations proactively
- Asks about things due soon that haven't been mentioned
- Email search as secondary source

**Why deferred**: Better implemented as a skill + a scheduled job once skill system exists.

### Commitment Tracking (Enhanced)

- Semantic search over conversation history for promise language ("I'll send you", "let me intro you to", "I'll follow up")
- Surfaces unfulfilled commitments before they become embarrassments

**Why deferred**: Basic commitment tracking exists (Phase 1.3). Enhanced semantic search benefits from parallel tools (active Phase 3) and may become a skill.

---

## Knowledge Layer

> _Deferred from original Phase 4._

### Notes Reader

- Connect to local notes (Obsidian vault, Apple Notes export, plain markdown files)
- Index and make semantically searchable
- Pepper can pull relevant notes into context when discussing a topic

### Decision Log

- Every significant decision gets recorded with: context, options considered, choice made, reasoning
- Pepper can reference past decisions when relevant ("you decided X six months ago because Y, does that still apply?")
- Builds a personal wisdom library over time

### Document Awareness

- Key documents: contracts, plans, important PDFs
- Pepper can answer questions about documents without you finding them

### Thinking History

- Optional: Claude Code conversation history (local sessions)
- Topics you've been researching, problems you've been working through
- Pepper gains awareness of what's been occupying your mind

**Why deferred**: Most of this becomes trivial once MCP integration (active Phase 5) lands — an Obsidian MCP server, a PDF MCP server, and a local notes MCP server all exist in the ecosystem already. Build the MCP client first, then connect these rather than writing custom readers.

**Success criteria (for when it's built)**:

- Pepper can answer "what did I decide about X?" accurately
- Pepper can find a note you vaguely remember writing
- "What have I been thinking about lately?" gives a real answer

---

## Health & Finance Layer

> _Deferred from original Phase 5._

### Health Data Reader

- Support multiple health data sources: Apple Health XML export, Oura Ring, Garmin Connect, Whoop, Fitbit, and others
- Incrementally sync activity, sleep, heart rate, HRV, and key health metrics from whichever devices the user owns
- Pepper gains awareness of energy levels, sleep quality, activity patterns
- Privacy: all processing done locally — raw data never leaves the machine

### Financial Reader

- Parse CSV exports from banks and investment accounts
- Categorize, summarize, and track over time
- Pepper can answer "how are we doing financially?" and "am I on track?"
- No direct banking API connections — CSV exports only (simpler, more private)

### Life Context Integration

- Health and financial patterns inform life context updates
- Pepper can connect dots: "your sleep has been poor this week, might explain the low energy you mentioned"

**Why deferred**: Like the knowledge layer, these become much easier as MCP servers or skills. Health data parsing is well-understood — wait until the runtime can do parallel ingestion and skill-based summarization.

**Success criteria (for when it's built)**:

- Pepper knows roughly how you're doing physically and financially
- Health and financial context improves recommendations ("you mentioned being tired — what's driving that?")
- "Am I on track for retirement?" can be answered at a basic level

---

## Maintenance & Security

> _Deferred from original Phase 6._

### Maintenance Agents

- Nightly health check: all subsystems responding, database healthy, disk space OK
- Weekly model evaluation: benchmark new model releases against current model
- Monthly dependency audit: outdated packages, security advisories
- Self-healing: common failure modes auto-resolved without human intervention
- Upgrade PRs: when a better model or component is available, agent proposes the upgrade

### Security Agents (KGB Layer)

- Red-team agent: continuously attempts prompt injection against Pepper
- Anomaly detection: unusual tool calls, unexpected data access patterns, behavior changes
- Audit log review: agent reads its own audit trail and flags anomalies
- Input sanitization: all external data (emails, messages) processed through sanitization before reaching Pepper core

### Backup and Recovery

- Automated PostgreSQL WAL streaming to backup volume
- Daily pgvector snapshots
- Weekly full system backup
- Tested restore procedures (maintenance agent runs restore tests on a clone)

### Observability

- System health dashboard in local web app (partial today)
- Metrics: model latency, tool call success rates, memory usage, sync freshness
- Alert: if Pepper hasn't successfully sent a morning brief in 48h, something is wrong

**Why deferred**: Maintenance and security are best built once the system is load-bearing daily. Premature hardening against failures you haven't actually experienced leads to the wrong defenses. The active Phase 3 error classifier is a down payment on this work.

**Success criteria (for when it's built)**:

- Can go 30 days without manually touching any infrastructure
- Attempted prompt injection is detected and logged
- System can be fully restored from backup in under 30 minutes

---

## The Long Game

Once the active roadmap and this wishlist are stable, Pepper begins driving its own evolution:

- **Pepper recommends improvements to the People subsystem** based on observed gaps in people/relationship data
- **Pepper recommends new subsystems** when patterns suggest a domain isn't covered
- **Pepper evolves its own life context** as it learns more about your patterns, values, and what matters to you
- **Pepper begins managing others** — drafting family communications, preparing you for difficult conversations, tracking how family members are doing based on all available signals

This is the Pepper arc: starts as a junior assistant, earns trust, becomes something that genuinely knows you.

---

## Prioritization Notes

This list is a hypothesis, not a commitment. Actual reprioritization triggers:

1. **You repeatedly ask Pepper for something it can't do** — that's a wishlist item moving to active
2. **An active phase unblocks multiple wishlist items** — bundle them opportunistically (e.g., MCP integration unblocks most of the Knowledge and Health & Finance layers)
3. **A wishlist item becomes load-bearing for trust** — e.g., if you're using Pepper daily and backup anxiety is real, move Maintenance & Security up

The active roadmap is for foundation work. This wishlist is for capability work. Don't mix them — building capabilities on shaky foundations just creates tech debt.

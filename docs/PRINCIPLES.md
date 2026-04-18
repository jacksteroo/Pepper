# Pepper — Core Principles

These are the load-bearing beliefs behind every architectural decision. When in doubt, return here.

---

## 1. Data Sovereignty

Your data is yours the way crypto is yours — not because a company promises to protect it, but because it physically lives on your hardware and nowhere else.

**What this means in practice:**

- No raw personal data (messages, emails, health, finance) ever transmitted to a third-party API
- Claude API and other frontier models may only receive summaries and structured outputs produced by local processing
- Every integration must have a local-only fallback
- If a service requires sending your data to their servers, it is not used

The threat model isn't paranoia — it's correct reasoning. Terms of service change. Companies get acquired. Breaches happen. The only defense is physical control.

---

## 2. Compounding Context Is the Moat

The intelligence of Pepper is not the model — models will commoditize to near-zero cost. The irreplaceable asset is **accumulated context**: every conversation, every decision, every family moment, every pattern observed over years.

After five years, Pepper knows things about you that no commercial product can replicate — because it was present for your actual life.

**What this means in practice:**

- Never delete historical data — compress, summarize, and archive instead
- Every significant event, decision, and conversation gets recorded
- The life context document is updated continuously and treated as sacred
- Retrieval quality improves with more data; storage is cheap and getting cheaper

---

## 3. Start Junior, Earn Trust

Pepper begins as a capable junior assistant and grows into something more over time. It doesn't need to be brilliant on day one — it needs to be present and learning.

**What this means in practice:**

- Phase 1 is useful and limited; that's fine
- Don't over-engineer for capabilities that aren't needed yet
- Trust is extended gradually as the system demonstrates reliability
- "Becomes part of the family" is the long arc, not the launch requirement

---

## 4. Self-Maintaining by Design

The maintenance loop is closed by agents, not humans. Claude Code running on a schedule can read the codebase, monitor health, evaluate new model releases, write upgrade PRs, and test them. Zero-downtime model swaps are a config change in Ollama.

**What this means in practice:**

- Maintenance agents are first-class components, not afterthoughts
- Systems are designed to be observable (logs, metrics, health endpoints)
- Model upgrades are non-breaking by design (abstraction layer between agent and model)
- Version control and database WAL ensure full rollback capability at any point

---

## 5. Models Are Commodities

LLM capabilities are converging toward commodity pricing. GPT-4 class reasoning is already near-free via open source. In 18–24 months, running a frontier-class model locally will be unremarkable.

**What this means in practice:**

- Never architect in a way that tightly couples to a specific model
- All model calls go through an abstraction layer (Ollama-compatible API)
- The investment is in the data layer and tool layer, not the model
- Pay for frontier API reasoning today where quality matters; migrate local as models improve

---

## 6. Adversarial Security Built In

Security is not a layer added later — it's an adversarial agent whose only job is to find holes. The KGB model: agents watching agents, continuously.

**What this means in practice:**

- A red-team agent runs continuously, attempting to find prompt injection vectors
- All agent actions are logged with full audit trails
- Anomaly detection runs independently of the main agent loop
- Security agents are upgraded on their own schedule, independent of Pepper core
- The system is designed to fail safe — if compromised, it stops acting, not escalates

---

## 7. Pluggable Everything

Every subsystem (People, Calendar, Communications, Knowledge, Health, Finance) is independently replaceable. The orchestrator doesn't care what's behind each interface — only that the interface contract is honored.

**What this means in practice:**

- Subsystems expose standard MCP-compatible tool interfaces
- No direct imports between subsystems — all communication via local REST or MCP
- The People subsystem can be upgraded, replaced, or forked without touching Pepper core
- The interface layer (Telegram today, something else tomorrow) is similarly replaceable

---

## 8. Offline First

Internet connectivity is not assumed. Pepper must be fully functional on the local network with no internet access.

**What this means in practice:**

- Local models (Ollama) are the baseline, not the fallback
- Telegram bot runs on local network; doesn't require Telegram cloud when offline
- All integrations (iMessage, calendar, email) read from local storage, not cloud APIs
- Claude API is an optional enhancement, not a dependency

---

## 9. Versioning and Rollback

Everything is versioned. Everything is recoverable. What can go wrong, can be undone.

**What this means in practice:**

- Git for all code, with signed commits
- PostgreSQL WAL + point-in-time recovery for all data
- Vector snapshots on a schedule
- The life context document is version-controlled separately with full history
- "Catastrophic failure" means restoring from a backup from last Tuesday — not losing years of context

# Pepper — Claude Code Instructions

## What This Project Is

Pepper is a sovereign, local-first AI life assistant. Think Iron Man's Pepper — a system that knows its owner deeply, anticipates needs, and operates proactively across every domain of life. All data stays on the owner's machine. No cloud dependencies for personal data.

## Core Principles (Never Violate These)

1. **Privacy-first**: Raw personal data (emails, messages, health, finance) never leaves the machine. Claude API may receive summaries and structured outputs only.
2. **Sovereignty**: No mandatory cloud services. Every component must have a local-only fallback.
3. **Additive**: The system accumulates context over time. Never delete or overwrite historical data — compress and archive instead.
4. **Pluggable**: Every subsystem (People, Calendar, Communications, etc.) is independently replaceable. No tight coupling between subsystems.
5. **Self-maintaining**: Prefer designs that a maintenance agent can upgrade without human intervention.

## Project Structure

```text
agent/          # Pepper core orchestrator
docs/           # Architecture, roadmap, principles
subsystems/
  people/       # Future: relationship intelligence subsystem
  calendar/     # iCal/CalDAV reader
  communications/ # iMessage, email
  knowledge/    # Notes, documents, decisions
  health/       # Health data (Apple Health, Oura Ring, Garmin, Whoop, etc.)
  finance/      # Financial data
security/       # Adversarial and monitoring agents
maintenance/    # Self-upgrade and health check agents
```

## Key References

- **Guardrails**: [docs/GUARDRAILS.md](docs/GUARDRAILS.md) — **READ THIS FIRST** — development guardrails based on OpenAI's harness engineering
- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md)
- Life Context: [docs/LIFE_CONTEXT.md](docs/LIFE_CONTEXT.md) — this is Pepper's ground truth about its owner
- LLM Strategy: [docs/LLM_STRATEGY.md](docs/LLM_STRATEGY.md)
- Agent Infrastructure: [docs/INFRA_GUIDELINES.md](docs/INFRA_GUIDELINES.md) — tool registry, memory pipeline, autonomy ladder, eval flywheel

## Development Guardrails (Harness Engineering)

Pepper follows **harness engineering** principles from OpenAI: the system around the agent (constraints, validation, feedback loops) is what makes agent-driven development reliable. Full details in [docs/GUARDRAILS.md](docs/GUARDRAILS.md).

### Non-Negotiable Rules

**Privacy Boundaries:**

- Raw personal data (emails, messages, health, finance) NEVER leaves the machine
- Claude API receives **summaries only**, never raw content
- All embeddings generated locally (Ollama + nomic-embed-text)
- PostgreSQL runs locally, never cloud-hosted
- **Before any external API call with personal data: ask yourself "is this a summary or raw content?"**

**Repository as Single Source of Truth:**

- All decisions, architecture, configuration live in this repository
- Knowledge in Google Docs, Slack, or people's heads doesn't exist to the agent
- When information is needed, add it to `docs/` first

**Architectural Boundaries:**

- Subsystems never import from each other
- Subsystems never import from `agent/core.py`
- Dependencies flow: config → subsystems → core → interfaces
- Each subsystem is independently replaceable

**Validation Before Execution:**

- All tool outputs are logged before return
- No blind execution of external API calls
- Rate limiting on all external APIs
- Manual user approval for consequential actions (sending messages, creating events)

**Graceful Degradation:**

- Claude API down → fallback to Ollama
- External API down → skip that section, deliver what's available
- Tools return `{"error": "..."}` instead of raising exceptions
- Never pretend to work when a dependency is unavailable

### Development Workflow

**When adding code:**

1. Check if it violates privacy boundaries (see GUARDRAILS.md §2)
2. Check if it breaks subsystem boundaries (see GUARDRAILS.md §3)
3. Update relevant docs (`ROADMAP.md`, `ARCHITECTURE.md`, `README.md`)
4. Add tests if modifying critical paths
5. Run linting before commit (`ruff`)

**When adding a tool:**

1. Define tool schema in `agent/{subsystem}_tools.py`
2. Implement `async def execute_{tool_name}(args: dict) -> dict`
3. Register in `agent/core.py`
4. Handle errors gracefully (return `{"error": "..."}`)
5. Log execution with `logger.info()` or `logger.debug()`

**When agent struggles:**

- Treat it as a signal: what's missing? (tool, docs, constraint)
- Add what's missing to the repository
- Update GUARDRAILS.md if needed

## Development Conventions

- Python 3.10+ for all backend services
- Each subsystem exposes a standard MCP-compatible tool interface (future: standalone services)
- Subsystems communicate via tool definitions only — never direct imports
- PostgreSQL + pgvector as the persistence layer
- Environment config via `.env` files, never hardcoded
- All agents must handle graceful degradation when a subsystem is unavailable
- Structured logging with `structlog` for observability

## Relationship to the People Subsystem

The People subsystem is intentionally deferred. It is NOT a dependency of Pepper Phase 1. Pepper should surface what relationship capabilities are actually needed through real usage before the implementation is expanded.

## No Co-Author Lines in Commits

# Pepper Development Guardrails

## Introduction

This document defines the development guardrails for Pepper based on **harness engineering** principles introduced by OpenAI. Harness engineering is the practice of designing environments, constraints, feedback loops, and infrastructure that make AI agents reliable at scale.

When OpenAI's team built a 1M+ line codebase with just three engineers driving Codex, they learned that the **system around the agent** — the constraints, feedback loops, documentation, linters, and lifecycle management — is what makes agent-driven development work. This harness is what prevents chaos and ensures quality.

Pepper must operate under similar principles: **the harness makes the agent reliable, not just smart.**

---

## Core Principles

### 1. Repository as Single Source of Truth

**Rule**: Anything the agent can't access in-context doesn't exist.

- All architectural decisions live in [`adr/`](adr/) as ADRs. Descriptive docs (architecture, roadmap, principles) live elsewhere in `docs/`; ADRs are the normative source for the decision they record. **This file (GUARDRAILS.md) overrides any ADR if they conflict** — see "ADR precedence" below.
- **Any decision that survives a Notion thread becomes an ADR before the implementation PR opens.** If the discussion converged, the rationale exists somewhere — capture it in an ADR or it will rot.
- All configuration schemas live in code or example files (`.env.example`, `config/`)
- All personal data mappings live in gitignored config files (`config/local/accounts.json`)
- Knowledge in Google Docs, Slack threads, or people's heads is invisible to the agent
- **When information is needed, add it to the repository first**

**Applied to Pepper:**

- Life context in `docs/LIFE_CONTEXT.md` (version controlled)
- Roadmap in `docs/ROADMAP.md` (version controlled)
- Architecture in `docs/ARCHITECTURE.md` (version controlled)
- Architectural decisions in [`docs/adr/`](adr/) (version controlled)
- Development instructions in `CLAUDE.md` (version controlled)
- Personal config in `config/local/accounts.json` (gitignored, template in repo)

**ADR precedence.** ADRs are normative for the architectural decisions they record, but they sit below this file in the precedence stack. If an ADR — proposed, accepted, or otherwise — conflicts with a guardrail in `GUARDRAILS.md` (Privacy-First, Subsystem Boundaries, Permission Boundaries, etc.), the guardrail wins and the ADR cannot be merged in that form. Reviewers should reject any ADR that weakens a guardrail without a corresponding update to this file landed first.

### 2. Privacy-First Architecture (Non-Negotiable)

**Rule**: Raw personal data never leaves the machine.

**Enforcement mechanisms:**

- Local LLM (Ollama) processes all raw personal data
- Claude API receives **summaries only**, never raw emails, messages, health data, or financial data
- All embeddings generated locally (nomic-embed-text via Ollama)
- PostgreSQL runs locally (never cloud-hosted)
- Code review: any PR that sends personal data externally must be rejected

**Applied to Pepper:**

- iMessage content → processed locally → summaries only to Claude
- Email content → processed locally → summaries only to Claude
- WhatsApp chats → processed locally → summaries only to Claude
- Slack messages → processed locally → summaries only to Claude
- Calendar event details → OK to send (not sensitive personal data)
- Memory entries → summaries only to Claude

### 3. Architectural Constraints (Subsystem Boundaries)

**Rule**: Dependencies flow in one direction, subsystems are independently replaceable.

**Dependency Flow:**

```text
agent/config.py
    ↓
subsystems/*  (calendar, communications, knowledge, etc.)
    ↓
agent/core.py (orchestrator)
    ↓
agent/telegram_bot.py, web/, etc. (interfaces)
```

**Constraints:**

- Subsystems never import from `agent/core.py`
- Subsystems never import from each other
- Subsystems communicate via tool definitions only
- Each subsystem must be independently testable
- Future: subsystems become standalone services (MCP servers)

**Applied to Pepper:**

- `subsystems/calendar/` has no knowledge of `subsystems/communications/`
- `agent/calendar_tools.py` defines tool interface, `agent/core.py` routes calls
- Tools are stateless: input → processing → output, no side effects on other subsystems

### 4. Validation and Testing Guardrails

**Rule**: Validate before apply. No blind execution.

**Mechanisms:**

- **Type checking**: `mypy` or similar on all code (future enhancement)
- **Linting**: `ruff` on all Python code
- **Testing**: Unit tests for all subsystems (`agent/tests/`)
- **Structural tests**: Validate subsystem boundaries (future enhancement)
- **Manual approval**: No automated actions without user approval for consequential operations

**Applied to Pepper:**

- All tool outputs are logged before being returned to user
- Memory writes are validated before PostgreSQL commit
- Calendar/email API calls have rate limiting
- No automated sending of messages without user approval
- Morning briefs and reviews are informational, not actional

### 5. Permission Boundaries

**Rule**: Restrict what the agent can access and modify.

**File Access:**

- **Read-only**: `~/Library/Messages/chat.db`, `~/Library/Application Support/WhatsApp/`, calendar/email via APIs
- **Read-write**: PostgreSQL (local), memory tables, conversation logs
- **Append-only**: Audit logs (`logs/`)
- **No access**: System files, other users' data, anything outside project scope

**API Access:**

- Google Calendar: read-only (no event creation without explicit user request)
- Gmail/Yahoo: read-only (no email sending without explicit user request)
- Slack: read-only (no posting without explicit user request)
- Telegram: send messages (this is the approved output channel)

**Applied to Pepper:**

- Subsystems operate with least privilege
- OAuth tokens stored securely in `~/.config/pepper/`
- API rate limiting prevents runaway calls
- No file writes outside of PostgreSQL, logs, and approved config locations

### 6. Rate Limiting and Resource Controls

**Rule**: Prevent runaway execution.

**Limits:**

- API calls: max N per minute per service (configured in each client)
- LLM calls: monthly spend limit (`MONTHLY_SPEND_LIMIT_USD` in `.env`)
- Database queries: connection pooling, query timeouts
- Memory growth: archival compression triggers at defined thresholds
- Background tasks: APScheduler with concurrency limits

**Applied to Pepper:**

- Ollama: no cost limit (local)
- Claude API: monthly spend limit enforced
- Google Calendar API: rate limit per client
- Email API: rate limit per account
- Morning brief: runs once per day maximum
- Weekly review: runs once per week maximum

### 7. Observability and Feedback Loops

**Rule**: When the agent struggles, treat it as a signal to improve the harness.

**Mechanisms:**

- Structured logging (`structlog`) with context
- Tool execution logs (what was called, with what args, what was returned)
- Error logs (what failed, why, what context was missing)
- Audit trail (all memory writes, all API calls)
- Agent self-reports: "I couldn't do X because Y was missing"

**Applied to Pepper:**

- All tool calls logged with `logger.info()` or `logger.debug()`
- Failed operations logged with `logger.error()` and context
- Missing context triggers a user prompt or memory retrieval
- When agent fails repeatedly, it's a sign that a tool or subsystem is missing

### 8. Graceful Degradation

**Rule**: Every external dependency has a fallback.

**Fallbacks:**

- Claude API unavailable → use local LLM (Ollama) for all reasoning
- Google Calendar API down → skip calendar context in briefs
- Email API down → skip email context
- Slack integration missing → skip deadline detection (gracefully inform user)
- PostgreSQL down → fail fast, don't pretend to work

**Applied to Pepper:**

- `agent/llm.py` + `agent/error_classifier.py` (Phase 3.3) handle all LLM failures with typed `ErrorCategory` / `DataSensitivity` classification — rate limits, context overflow, auth failures, and model unavailability each produce specific, actionable messages rather than generic errors
- Fallback routing is privacy-safe: `local_only` calls never route to Claude API under any failure mode (enforced at two layers, regression-tested in `agent/tests/test_error_classifier.py`)
- Morning brief skips sections that fail, delivers what's available
- Tools return `{"error": "..."}` instead of raising exceptions
- User sees: "Calendar unavailable this morning" instead of crash

---

## Development Workflow Guardrails

### Code Changes

**When adding a new feature:**

1. Update `docs/ROADMAP.md` if it's a new phase item
2. Update `docs/ARCHITECTURE.md` if it changes system structure
3. Write the code in the appropriate subsystem
4. Add tests to `agent/tests/`
5. Update `.env.example` if new config is needed
6. Update `README.md` if user-facing setup changes
7. Run tests before committing
8. No commits to `main` that break existing functionality

**When adding a new subsystem:**

1. Create `subsystems/{name}/` directory
2. Create `agent/{name}_tools.py` for tool definitions
3. Update `agent/core.py` to import and register tools
4. Add setup instructions to `README.md`
5. Add subsystem status to `docs/ARCHITECTURE.md`
6. Add to roadmap if it's a new phase item

**When modifying the agent orchestrator:**

- Changes to `agent/core.py` require extra scrutiny
- Must not break existing tool calling patterns
- Must maintain privacy boundaries (summaries only to Claude)
- Must handle errors gracefully

### Data Handling

**Personal data (iMessage, WhatsApp, Slack, email bodies, health, finance):**

- NEVER log raw content (not even in debug logs)
- Process with local LLM only
- Send summaries only to Claude API (if needed)
- Store in PostgreSQL (local only)
- No external transmission except summaries

**Non-personal data (calendar event titles, email senders/subjects, public info):**

- OK to log for debugging
- OK to send to Claude API
- OK to cache

**Memory and embeddings:**

- All embeddings generated locally (Ollama + nomic-embed-text)
- Memory summaries (not raw content) can go to Claude API
- Vector indices stay local in pgvector

### Git Commits

**Commit message format:**

```text
<type>: <description>

<optional body explaining why, not what>
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

**Rules:**

- Never commit secrets (`.env` is gitignored)
- Never commit personal data (`config/local/` is gitignored)
- Never commit tokens or credentials
- Run tests before committing (when tests exist)

### Tool Development

**When creating a new tool:**

1. **Define in tool schema** (`agent/{subsystem}_tools.py`):

   ```python
   {
       "type": "function",
       "function": {
           "name": "tool_name",
           "description": "Clear description of what it does and when to use it",
           "parameters": { ... }
       }
   }
   ```

2. **Implement execute function**:

   ```python
   async def execute_tool_name(args: dict) -> dict:
       try:
           # Process with local LLM if personal data
           # Return structured output
           return {"key": "value"}
       except Exception as e:
           logger.error("tool_failed", tool="tool_name", error=str(e))
           return {"error": f"Failed: {e}"}
   ```

3. **Register in core.py**:

   ```python
   if name == "tool_name":
       return await execute_tool_name(args)
   ```

4. **Test manually** via Telegram or web interface

5. **Add unit test** to `agent/tests/`

---

## Security Guardrails

### Adversarial Testing (Phase 6)

**When implemented:**

- Red-team agent attempts prompt injection
- Anomaly detection flags unusual tool calls
- Audit log review catches unexpected patterns

**Current defense (Phase 1-3):**

- Principle of least privilege (read-only APIs where possible)
- User approval for consequential actions
- No automated message sending
- Structured logging for audit trail

### Input Sanitization

**External data sources:**

- iMessage: SQLite queries use parameterized queries
- Email: API responses validated before processing
- Calendar: API responses validated
- Slack: API responses validated
- WhatsApp: SQLite queries use parameterized queries

**User input:**

- Telegram messages: passed to LLM (Claude handles prompt injection defenses)
- Web UI: input validated on frontend and backend

---

## Enforcement

### Automated (Future)

- Pre-commit hooks: lint check, type check (if enabled)
- CI/CD: run tests on every PR
- Structural tests: validate subsystem boundaries

### Manual (Current)

- Code review: ensure privacy boundaries are maintained
- Pull requests: review for architectural compliance
- Testing: manual testing of new features before merge

---

## When to Break the Rules

**Never:**

- Privacy-first architecture (personal data never leaves machine)
- Repository as single source of truth

**Rarely (with explicit user approval):**

- Automated actions (sending messages, creating calendar events)
- Breaking subsystem boundaries (must have architectural reason)

**Sometimes (with justification in commit message):**

- Skipping tests (if no tests exist yet for that subsystem)
- Adding external dependency (if it's the right tool for the job)

---

## Evolution

This document evolves as Pepper evolves. When the agent struggles, we:

1. Identify what's missing (tool, documentation, constraint)
2. Add it to the repository
3. Update this guardrails document if needed
4. Have the agent (Claude Code) write the fix

**The harness is never done. It grows with the system.**

---

## References

- OpenAI's Harness Engineering: <https://openai.com/index/harness-engineering/>
- Pepper Architecture: `docs/ARCHITECTURE.md`
- Pepper Roadmap: `docs/ROADMAP.md`
- Development Instructions: `CLAUDE.md`

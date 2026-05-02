# Pepper

A sovereign, local-first AI life assistant. Knows your life context deeply. Runs entirely on your machine. Your personal data never leaves.

---

## What it does

- **Morning briefs** — what's today, open loops, pending commitments (delivered to Telegram or web)
- **Conversational** — ask anything about your life, get context-aware answers that draw on your full history
- **Memory** — remembers everything you tell it, auto-detects commitments you make, compresses old memories into long-term archival
- **Proactive** — morning brief, weekly review, commitment reminders run on schedule without you asking
- **Calendar awareness** — knows your schedule across all Google Calendars (personal, work, shared), answers "what do I have coming up?"
- **Email integration** — checks inbox across Gmail and Yahoo accounts, searches emails, surfaces unread counts
- **Web search** — can search the web (Brave Search API) when needed
- **Navigation** — calculates driving time with live traffic (Google Maps API)
- **Extensible** — three-layer architecture (Data / Intelligence / Presentation) with capability subsystems at Layer 1 means iMessage, health, finance, notes plug in over time

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| Python 3.10+ | `python3 --version` |
| Docker | For PostgreSQL + pgvector |
| [Ollama](https://ollama.ai) | Local LLM inference |
| Ollama models | `ollama pull hermes-4.3-36b-q4:latest && ./scripts/package_hermes4_ollama.sh && ollama pull nomic-embed-text` |
| Anthropic API key | Optional — for complex reasoning. Pepper degrades gracefully without it |
| Telegram bot token | Optional — get from [@BotFather](https://t.me/BotFather) |
| Google OAuth credentials | Optional — for Calendar and Gmail integration. See `subsystems/*/setup_auth.py` |
| Brave Search API key | Optional — for web search capability |
| Google Maps API key | Optional — for driving time/routing queries |

**Hardware**: 64GB unified memory (M3/M4 Max) runs hermes-4.3-36b-tools:latest well. 36GB runs 32B models comfortably.

If you use Hermes 4.3 with Pepper, package it through the repo Modelfile instead of
using the raw `hermes-4.3-36b-q4:latest` template directly. The packaged model enables
tool-aware prompting for Ollama and is created as `hermes-4.3-36b-tools:latest` by
default:

```bash
ollama pull hermes-4.3-36b-q4:latest
./scripts/package_hermes4_ollama.sh
```

Then set `DEFAULT_LOCAL_MODEL=hermes-4.3-36b-tools:latest` in `.env`.

---

## Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/jacksteroo/Pepper
cd Pepper
cp .env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN if desired

# 2. Install
make install

# 3. Start (starts PostgreSQL via Docker, then Pepper)
make start

# 4. Web UI (in a second terminal)
cd web && npm run dev
# Open http://localhost:5173
```

---

## The most important step: your Life Context

Before Pepper is useful, you need to fill in your Life Context document. This is Pepper's ground truth about you — your family, responsibilities, goals, patterns, and what matters. The quality of everything Pepper does is directly proportional to the honesty and specificity of this document.

```bash
cp docs/LIFE_CONTEXT.md.example data/life_context.md
```

Then open `data/life_context.md` and fill it in. [`docs/LIFE_CONTEXT.md.example`](docs/LIFE_CONTEXT.md.example) has a fully annotated template with guidance for every section:

> The file lives under `data/` (user-mutable state), not `docs/` (project documentation). Pepper writes back to it when you ask it to update something — keep it under `data/` so the docs/ tree stays clean and read-only inside the container.

| Section | What goes here |
| --- | --- |
| **Identity** | Name, location, life stage — the basics Pepper uses to ground every response |
| **Self-Description** | How you actually make decisions, what depletes you, your real blind spots |
| **Partner / Spouse** | What's going on with your partner and the current state of the relationship |
| **Family** | Parents, siblings, children — anyone whose situation affects your mental load |
| **Professional** | Current role, active company/project, 12-month goal, what could derail it |
| **What You Want** | More of, less of, excited about, worried about — grounding Pepper in what actually matters to you |
| **Travel** | How you plan trips, upcoming travel with current status |
| **Meals and Dietary Restrictions** | Cooking approach, style preferences, dietary restrictions, household constraints |
| **Finances** | Big-picture situation, real estate, open loops, 3–5 year direction (no account numbers needed) |
| **Active Challenges and Open Loops** | Concrete blockers, pending tasks, background worries — Pepper surfaces these in your morning brief |
| **Pepper's rules** | How Pepper should behave with you specifically, and hard constraints |

A few tips:
- Write what's actually true, not what sounds good. Pepper uses this to prioritize, not to judge.
- Be specific. "Work is busy" is useless. "Need to ship v1 by June or the funding round is at risk" is useful.
- Keep it current. Pepper is only as accurate as this document. Update it when things change.
- `data/life_context.md` is gitignored — it stays on your machine and is never committed.

---

## Interfaces

| Interface | How to use |
| --- | --- |
| **Web UI** | `cd web && npm run dev` → <http://localhost:5173> |
| **API** | <http://localhost:8000/docs> (FastAPI auto-docs) |
| **Telegram** | Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USER_IDS` in `.env`, then message your bot |

### Telegram commands

- `/brief` — morning brief now
- `/review` — weekly review now
- `/status` — system health
- Any message → conversation with Pepper

---

## Architecture

```text
Telegram / Web UI / API
         ↓
    Pepper Core (FastAPI + orchestrator)
    ├── Life Context Document (your ground truth)
    ├── Persistent Memory (pgvector — working / recall / archival)
    ├── Proactive Scheduler (APScheduler — brief, review, commitments)
    ├── Semantic Intent Router (k-NN over exemplar embeddings; pgvector HNSW)
    └── Tool Router → Subsystems (MCP/REST)
              ↓
    PostgreSQL + pgvector (local)
```

The intent router is semantic since the Phase 3 cutover (2026-04-29):
top-level intent is classified by k-NN over per-intent exemplar embeddings
(`qwen3-embedding:0.6b`), with capability filtering applied as a
deterministic post-route step. The legacy regex router runs in shadow for
the soak window. See [`docs/SEMANTIC_ROUTER.md`](docs/SEMANTIC_ROUTER.md).

Two LLM tiers:

- **Local** (Ollama / hermes-4.3-36b-tools:latest) — all raw personal data, background tasks, routine retrieval
- **Frontier** (Claude API) — complex reasoning, summaries only, never raw personal data

---

## Privacy

- All personal data stays on your machine
- The Anthropic API (if configured) receives only summaries and structured outputs — never raw messages, emails, health data, or financial data
- PostgreSQL runs locally via Docker
- Embeddings are always local (nomic-embed-text via Ollama)

---

## Roadmap

| Phase | Description | Status |
| --- | --- | --- |
| **Phase 1** | Core — conversation, tiered memory, proactive scheduler, Telegram bot, web UI | ✅ Complete |
| **Phase 2** | Communications + Calendar — Gmail, Yahoo, iMessage, WhatsApp, Slack, contact enrichment, Google Calendar | ✅ Complete |
| **Phase 3** | Runtime — parallel tool execution, context compression, classified error handling | ✅ Complete |
| **Phase 4** | Skill System — SKILL.md structured workflows, self-improving via user-approved diffs | Planned |
| **Phase 5** | MCP Integration — subsystems as MCP servers, external MCP tools (GitHub, Linear, Obsidian) | Planned |
| **Layer 3** | Mobile + macOS thin clients on Tailscale-accessible server (FastAPI + long polling); home server primary, operator-owned VPS as fallback. See Epic 08 (issue #70). | Planned |

---

## Development

```bash
make test    # Run tests
make lint    # Ruff + black check
make db-start  # Start PostgreSQL only
make db-stop   # Stop PostgreSQL
```

Pepper always logs to stdout. By default it also mirrors logs to `logs/pepper.log`.
In Docker, `docker-compose.yml` bind-mounts `./logs` into the container so the same
file is persisted on the host.

### Documentation

- **[GUARDRAILS.md](docs/GUARDRAILS.md)** — Development guardrails based on OpenAI's harness engineering (read this first!)
- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — System architecture: three-layer framing (Data / Intelligence / Presentation), with the subsystem decomposition preserved as an appendix
- **[ROADMAP.md](docs/ROADMAP.md)** — Phase-by-phase build plan
- **[CONTINUOUS_IMPROVEMENT_PLAN.md](docs/CONTINUOUS_IMPROVEMENT_PLAN.md)** — How Pepper gets iteratively better through evals, simulated chats, and judgment-focused upgrades
- **[CLAUDE.md](CLAUDE.md)** — Instructions for Claude Code when working on this project
- **[LLM_STRATEGY.md](docs/LLM_STRATEGY.md)** — How Pepper uses local vs frontier LLMs

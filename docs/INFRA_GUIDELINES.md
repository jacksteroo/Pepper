# Pepper — Agent Infrastructure Guidelines

## Why Infrastructure Over Model

Pepper will improve faster from better infrastructure around the model than from chasing a newer model. This document translates current best practices from Anthropic, OpenAI, and MCP guidance into Pepper-specific rules.

The system around the agent — the tool registry, memory pipeline, session log, approval policies, and eval flywheel — is what makes Pepper reliably useful. The model is the brain; these are the hands, the nervous system, and the immune system.

---

## 1. Separate Brain, Hands, and Memory

The orchestrator (brain), tools (hands), and session/event log (memory) must stay independent. No tight coupling between them.

- **Orchestrator**: replaceable; knows how to route, plan, and decide
- **Tools**: isolated subsystems; one job each; no shared state
- **Session log**: lives outside the live prompt; persists across runs; can be replayed

Applied to Pepper: `agent/core.py` is the brain. `subsystems/*/` are the hands. The PostgreSQL event log is the durable memory. None of these should depend on the internals of another.

---

## 2. Start Single-Agent; Add Subagents Only When Earned

One main orchestrator plus tools covers most of what Pepper needs today. Don't add subagents just because it sounds powerful.

Add a subagent only when you need one of:
- Parallel retrieval across multiple independent sources
- Context isolation (untrusted content processed in a separate window)
- A truly distinct domain with its own tool set and safety profile

Before adding a subagent, ask: could a tool + a well-scoped prompt replace it? Usually yes.

---

## 3. Tools Are First-Class Product Surfaces

Every tool Pepper has should be designed with the same care as a product feature:

| Property | Requirement |
|---|---|
| Name | Unambiguous, verb-noun (`search_calendar`, `draft_message`) |
| Description | Precise; includes when to use and when NOT to use |
| Schema | Strict types; no freeform string blobs as inputs |
| Examples | At least one in the description |
| Risk level | `read` / `write` / `external` / `irreversible` |
| Approval policy | See §6 |
| Error contract | Returns `{"error": "..."}` on failure; never raises |

Tool descriptions are the primary interface between the model and the system. Vague descriptions cause wrong tool calls. Wrong tool calls cause trust failures.

---

## 4. Memory Is a Pipeline, Not a Dump

Raw personal data must not sit in the model context. Memory flows through a distillation pipeline:

```
Raw data (local only)
  → Structured extraction (Ollama, local)
  → Facts / summaries with provenance + confidence + timestamp
  → pgvector store (local)
  → Selective retrieval into context
  → Periodic review / expiry
```

Every memory write must record:
- **Source**: where did this come from?
- **Confidence**: how certain is this?
- **Timestamp**: when was it observed?
- **Expiry or review date**: when should it be re-verified?

Never overwrite historical memory — append and archive. This is the Additive principle from CLAUDE.md.

---

## 5. Prefer Explicit Retrieval Over Semantic Search Where Possible

For a life assistant, exact retrieval often beats approximate recall:

- Calendar events → SQL filter by date range, not cosine similarity
- Contact info → exact lookup by name or identifier
- Financial amounts → precise query, not fuzzy match

Use vector search (pgvector) where the query is inherently fuzzy: "what have I said about this person?", "find a decision I made about X", "what's my general feeling about Y?"

Mixing explicit and semantic retrieval in the right places is the skill. Default to explicit; add semantic where fuzziness is the feature.

---

## 6. Risk Ladder for Autonomy

Not all actions carry the same stakes. Pepper should operate at the appropriate autonomy level for each action type:

| Action Type | Autonomy Level |
|---|---|
| Read-only retrieval (calendar, contacts, notes) | Automatic |
| Summarization, analysis, drafting | Automatic |
| Creating drafts (message, event, note) | Semi-automatic (show before commit) |
| Sending messages, posting externally | Requires confirmation |
| Creating / editing calendar events | Requires confirmation |
| Financial actions | Requires explicit confirmation |
| Deleting or overwriting data | Requires explicit confirmation |

When in doubt, err toward confirmation. A life assistant that asks once too often is annoying. A life assistant that takes an irreversible action without asking once is a trust failure.

---

## 7. Hard Boundaries Around Untrusted Text

Emails, messages, web pages, and documents are untrusted. They must not directly shape privileged prompts or tool inputs.

The rule: **convert untrusted content to validated structured fields before it touches tool schemas.**

```
Raw email body
  → local extraction (Ollama): sender, subject, intent, entities
  → validated struct: { from, subject, action_requested, entities[] }
  → that struct enters the orchestrator context, not the raw body
```

This prevents prompt injection from emails or messages that contain instructions designed to manipulate Pepper.

---

## 8. Credentials Out of Model-Reachable Environments

API tokens, OAuth credentials, and secrets must not be accessible in the same sandbox where generated code or arbitrary tool outputs execute.

- Credentials live in `.env` files, never in prompts or tool outputs
- External API calls are made by tool implementations, not by code the model generates
- No tool should return a credential, token, or secret in its output

---

## 9. Build the Eval Flywheel Early

Pepper's quality compounds only if failures become tests. Log structured traces; inspect failures; turn real failures into eval cases.

Key things to grade:
- **Tool choice accuracy**: did Pepper pick the right tool for the job?
- **Privacy leakage**: did any raw personal data reach an external API?
- **Retrieval quality**: did the right memories surface?
- **Proactivity calibration**: over-eager or missed?
- **Approval policy adherence**: did irreversible actions get confirmed?

Even a lightweight eval — 20 traced interactions reviewed weekly — compounds into a meaningfully better system over months.

---

## 10. Optimize for Trust, Not Maximum Agency

The goal is a life assistant the owner would give access to their inbox, calendar, and finances because it has earned that trust. That means:

- Predictable behavior over clever behavior
- Conservative on irreversible actions
- Transparent about what it's doing and why
- Good at knowing when to ask

Trust is the product. Agency is the mechanism. Don't confuse them.

---

## Pepper's Next Highest-Leverage Infrastructure Work

Based on the above, in priority order:

1. **Risk-rated tool registry** — every tool tagged with risk level and approval policy (§3, §6)
2. **Durable session/event log** — outside the live prompt, replayable, queryable (§1)
3. **Stricter memory write policy** — provenance, confidence, expiry on every write (§4)
4. **Trace-based evals** — tool choice, privacy, proactivity quality (§9)
5. **Stronger people/commitments layer** — commitments, follow-ups, relationship context (§4, §5)

---

## References

- [OpenAI: A practical guide to building agents](https://openai.com/research/a-practical-guide-to-building-agents)
- [OpenAI: Safety in building agents](https://openai.com/safety/safety-in-building-agents)
- [OpenAI: Evaluate agent workflows](https://openai.com/research/evaluate-agent-workflows)
- [Anthropic: Writing effective tools for AI agents](https://docs.anthropic.com/en/docs/build-with-claude/tool-use)
- [Anthropic: Building agents with the Claude Agent SDK](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/overview)
- [Model Context Protocol intro](https://modelcontextprotocol.io/introduction)
- [MCP security best practices](https://modelcontextprotocol.io/docs/concepts/security)
- [docs/GUARDRAILS.md](GUARDRAILS.md) — Pepper's harness engineering rules
- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — system layer model

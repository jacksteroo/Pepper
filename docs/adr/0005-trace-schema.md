# ADR-0005: Trace schema — canonical record of every agent turn

- **Status:** Proposed
- **Date:** 2026-05-02

## Context

Pepper has run for several months without a structured per-turn record. Diagnostics live in scattered logs (`agent/chat_turn_logger.py` writes JSONL, `routing_events` captures router decisions, `mcp_audit` captures tool invocations) and there is no single place that joins *what was asked → what context was assembled → which model was selected → which tools fired → what was returned → how the user reacted*.

That gap blocks two roadmap epics:

- **Epic 04 (`agents/reflector`)** — daily/weekly reflection only works if there is one queryable record per turn.
- **Epic 05 (DSPy/GEPA optimizer)** — prompt optimization requires labelled trajectories, which is exactly what a trace is.

Source: [OJ calibration import #1](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0) called out that without traces, reflection collapses into "vibes-based LLM self-prompting that hallucinates as confidently as `hermes3` does about health metrics" — the same failure mode the maintainer already filed in `feedback_hermes3_health_hallucination.md`.

The schema is the contract that downstream epics read against. Getting field names, types, and nullability wrong now forces a painful migration after E4/E5 land.

## Decision

Define the canonical `Trace` record. Every agent turn — user-triggered or scheduler-triggered, every archetype — produces exactly one row with the fields below.

### Fields

| Field | Type | Nullable | RAW_PERSONAL | Notes |
|---|---|---|---|---|
| `trace_id` | uuid | no | no | Primary key. |
| `created_at` | timestamptz | no | no | Indexed (B-tree). |
| `trigger_source` | enum (`user` \| `scheduler` \| `agent`) | no | no | Distinguishes proactive vs reactive turns. |
| `scheduler_job_name` | text | yes | no | Set when `trigger_source = scheduler`. |
| `archetype` | enum (`orchestrator` \| `reflector` \| `monitor` \| `researcher`) | no | no | Indexed jointly with `created_at`. |
| `input` | text | no | **yes** | The user message or scheduler trigger payload. |
| `assembled_context` | jsonb | no | **yes** | Output of E3 context assembly with provenance (`{items: [{source, ref, summary}], strategy}`). Stub-populated until #33 lands. |
| `model_selected` | text | no | no | E.g. `hermes3-local`, `claude-opus-4-7`. |
| `model_version` | text | no | no | Pinned model identifier (e.g. SHA, API version tag). |
| `prompt_version` | text | no | no | Versioned per E5 (#48). |
| `tools_called` | jsonb | no | **yes** | Array of `{name, args, result_summary, latency_ms, error}`. Indexed (GIN) for "which traces called X". |
| `output` | text | no | **yes** | Final assistant response text. |
| `latency_ms` | int | no | no | End-to-end turn latency. |
| `user_reaction` | jsonb | yes | yes | `{thumbs: +1\|-1\|null, followup_correction: bool, source: explicit\|inferred}` — populated when signal arrives, may stay null forever. |
| `data_sensitivity` | enum (mirrors `agent.error_classifier.DataSensitivity`) | no | no | First-class column so consumers filter without parsing the row. |
| `embedding` | vector(1024) | yes | derived | Embedding of `input` + `output`. Nullable by design — recall-tier compression drops it (recoverable). |
| `embedding_model_version` | text | yes | no | E.g. `qwen3-embedding:0.6b`. Required when `embedding` is non-null. |
| `tier` | enum (`working` \| `recall` \| `archival`) | no | no | Compression tier (#21). New rows are `working`. |

### Storage decision: pgvector on traces from day one

Per #19 (resolved 2026-05-02): the trace store exists for the agent to reason over its own behavior. That is fundamentally a similarity problem. Embedding cost is negligible because `qwen3-embedding:0.6b` (1024-dim) is already loaded for the router. Re-embedding on model swap is tractable as long as `embedding_model_version` is recorded per row (which it is). HNSW index on `embedding`. Recall-tier compression treats the embedding as expendable — text is preserved, embedding can be recomputed.

### Mutability

Append-only at the application layer for the **conversation payload** — `input`, `output`, `assembled_context`, `tools_called`, `latency_ms`, and the provenance fields are written exactly once at `INSERT` and never change.

Three columns are explicit, narrow UPDATE carve-outs because the row is created before the value is known:

| Column | Updated by | When |
|---|---|---|
| `embedding`, `embedding_model_version` | async embedding worker (#22 step 5) | After the orchestrator finishes the turn — embedding generation runs off the critical path. |
| `tier` | nightly compression job (#21) | Once per row, advancing `working → recall → archival`. Companion column trims (e.g. setting `embedding = NULL` at the recall boundary) ride this same UPDATE. |
| `user_reaction` | reaction-capture path (separate from this epic) | When an explicit thumb arrives or a follow-up correction is inferred. |

`data_sensitivity` is **not** an UPDATE carve-out: a row's sensitivity is set at `INSERT` and immutable for the row's lifetime. Reclassification (e.g. promoting a `local_only` row to `sanitized` after redaction) is performed by inserting a new derivative row, never by mutating the original.

The Python `Trace` dataclass (`agent/traces/schema.py`) is `frozen=True` so an in-process holder cannot circumvent these rules between `start()` and persistence. The mutable accumulator that turns a turn-in-progress into a finalized `Trace` is `TraceBuilder`, landing in #22.

### Postgres roles & grants (mandate for #20)

Three roles divide trace-table privilege:

- **`pepper_traces_writer`** (used by `agent/core.py`, `agent/scheduler.py`, and the four agent processes) — `INSERT` only. No `UPDATE`, no `DELETE`.
- **`pepper_traces_compactor`** (used by the embedding worker and the nightly compression job) — `SELECT` plus `UPDATE` on `(embedding, embedding_model_version, tier)`. No `DELETE`. No `UPDATE` on any other column.
- **`pepper_traces_reader`** (used by E4 reflector, E5 optimizer, and the `/traces` HTTP route #24) — `SELECT` only.

The compactor role is the only path by which the carve-out columns may change. Postgres permits per-column `GRANT UPDATE (col1, col2, col3) ON traces`; #20 uses that. Without a per-column `UPDATE` grant the compactor would have full-row update rights, defeating the invariant at the database layer.

`DELETE` is not granted to any role. Retention is enforced exclusively through the `tier` column — there is no path that removes a trace from the table once it has been written.

### Privacy

`input`, `assembled_context`, `output`, `tools_called.result_summary`, and (transitively) `embedding` may contain raw personal data. The schema is local-only by construction:

- The `traces` table lives in the same Postgres instance as `memory_events` and `routing_events` — no separate cloud-hosted store, ever.
- No tool, MCP server, or HTTP route may serialize a trace to a destination outside the host. #25 enforces this with a regression test in the existing 59-test MCP suite.
- The `data_sensitivity` column is a first-class filter so consumers (UI panel #24, optimizer #45) can drop rows above their handling capability without parsing field-by-field.

### Consequences

**Positive**

- E4 and E5 unblocked — both epics can read against a stable contract.
- One source of truth for "what happened on turn X" replaces three scattered log streams.
- Embedding-on-traces enables free clustering and "find similar past turns" (#24's similarity action).

**Negative**

- Storage cost: ~1024-dim vector + jsonb per turn. Mitigated by tiered compression (#21).
- Trace emission adds latency to every turn. Bounded by #22's <50ms p95 acceptance criterion; embedding generation is async to keep the path off the critical path.
- Schema changes after this ADR require a real migration with backfill, not a SQLAlchemy `create_all` no-op.

**Neutral**

- `agent/chat_turn_logger.py` (JSONL writer) keeps existing semantic-router responsibilities. Trace emission is additive, not a replacement. Reconciliation between the two log streams is intentionally out of scope for E1 and is filed for follow-up if the redundancy becomes a maintenance burden.

## Alternatives considered

- **Status quo (do nothing).** Reflection and optimization both stall on missing data. Rejected — explicitly the failure mode named in the OJ calibration import.
- **Relational-only traces (no embeddings).** Cheaper storage, no model-swap re-embed risk. Rejected per #19 — the dominant access pattern for E4/E5 is similarity search.
- **Reuse `routing_events` and add columns.** Avoids a new table. Rejected — `routing_events` is router-scoped, already at 8 indexes, and conflating per-turn trace data with router telemetry would couple two epics that should evolve independently.
- **External trace store (LangSmith, Helicone, Phoenix).** Off-the-shelf UI. Rejected — violates the privacy principle that raw personal data never leaves the host.

## References

- Parent epic: [Epic 01: Trace Substrate (#17)](https://github.com/jacksteroo/Pepper/issues/17)
- Sub-issues: [#18 schema](https://github.com/jacksteroo/Pepper/issues/18), [#19 pgvector decision](https://github.com/jacksteroo/Pepper/issues/19)
- Notion: [OJ calibration import #1](https://www.notion.so/jacksteroo/OpenJarvis-calibration-lessons-challenges-shortest-path-354fb736739081ae8834eb6be2d361c0)
- Related ADRs: [ADR-0002 compounding capability](0002-fifth-anchoring-principle-compounding-capability.md), [ADR-0003 Layer 2 is the active surface](0003-layer-2-is-the-active-surface.md), [ADR-0004 introduce `agents/` directory](0004-introduce-agents-directory.md)
- Canonical schema doc: [`docs/trace-schema.md`](../trace-schema.md)
- Python contract: [`agent/traces/schema.py`](../../agent/traces/schema.py)

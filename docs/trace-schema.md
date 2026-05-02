# Trace Schema (canonical)

This is the canonical reference for the `traces` table. Decision rationale lives in [ADR-0005](adr/0005-trace-schema.md). The Python contract that mirrors this doc lives in [`agent/traces/schema.py`](../agent/traces/schema.py). The Postgres migration that materializes it lands in #20.

## Field reference

Every field in the `Trace` record, with its writer (where the value is set) and at least one reader (where it is consumed).

### Identity & timing

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `trace_id` | uuid | no | `TraceBuilder.start()` | UI detail view (#24); reflector cross-reference (#39) |
| `created_at` | timestamptz | no | `TraceBuilder.start()` | reflector daily window (#39); UI list filter (#24) |

### Provenance

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `trigger_source` | enum | no | `agent/core.py` (`user`), `agent/scheduler.py` (`scheduler`), `agents/*` (`agent`) | reflector (proactive vs reactive analysis) |
| `scheduler_job_name` | text | yes | `agent/scheduler.py` per-job wiring (#23) | reflector failure-mode rollups (#41) |
| `archetype` | enum | no | the agent process emitting the trace | reflector per-archetype rollups (#37, #39) |

`trigger_source` enum members:

- `user` — turn originated from a user message (Telegram, web UI, Slack passthrough).
- `scheduler` — APScheduler invoked `pepper.chat()`.
- `agent` — another agent process (reflector, monitor, researcher) invoked the orchestrator.

`archetype` enum members map 1:1 to the four agent processes named in [ADR-0004](adr/0004-introduce-agents-directory.md):

- `orchestrator` — the user-facing turn-by-turn agent (current `PepperCore`).
- `reflector` — periodic introspection over recent traces (#39).
- `monitor` — recurring failure-mode detector (#41).
- `researcher` — long-horizon background research agent (placeholder; no producer yet).

### Conversation payload (RAW_PERSONAL)

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `input` | text | no | `TraceBuilder.start(input=...)` | UI detail view; reflector |
| `assembled_context` | jsonb | no | `TraceBuilder.set_context(...)` | UI detail view; optimizer (#46) |
| `output` | text | no | `TraceBuilder.finish(output=...)` | UI detail view; reflector eval rubric (#42) |

`assembled_context` shape (stub until #33 fully implements E3):

```jsonc
{
  "strategy": "recall+memory+life_context",   // assembly strategy name
  "items": [
    {
      "source": "memory_events",            // table or subsystem name
      "ref": "<row id or external locator>",
      "summary": "...",                     // human-readable summary; for the UI
      "score": 0.83                         // optional retrieval score
    }
  ],
  "version": 1                              // bumped when this jsonb shape changes
}
```

### Model & prompt

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `model_selected` | text | no | `TraceBuilder.set_model(...)` (called from the LLM dispatch site) | reflector (model performance comparison); optimizer |
| `model_version` | text | no | `TraceBuilder.set_model(version=...)` | reflector (regression detection across model swaps) |
| `prompt_version` | text | no | `TraceBuilder.set_model(prompt_version=...)` | optimizer (#48) — joins traces back to the prompt artifact that produced them |

Until #48 lands, every dispatch site that does not yet wrap a versioned prompt MUST pass `agent.traces.PROMPT_VERSION_UNVERSIONED` (`"unversioned"`). The optimizer treats this sentinel as "ineligible for prompt-level rollups" and skips those rows when computing per-prompt deltas. The same convention applies to `model_version` — pass an empty string until the dispatch site is wired to record an actual model identifier (SHA, API tag, etc.); a future hardening PR will gate non-empty `model_version` once every dispatch site is wired.

### Tool calls (RAW_PERSONAL)

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `tools_called` | jsonb | no | `TraceBuilder.add_tool_call(...)` per call | UI detail view; reflector (tool failure rates); optimizer |

Each element shape:

```jsonc
{
  "name": "send_telegram_message",
  "args": {"chat_id": "<redacted-or-raw>", "text": "..."},
  "result_summary": "ok",      // free-form short string; raw payload never persisted
  "latency_ms": 412,
  "success": true,             // mirrors agent/mcp_audit.py AuditEntry.success
  "error": null                 // free-form short error string when success=false; null otherwise
}
```

`name` is the only required key — `__post_init__` enforces it so the GIN index in #20 always has a value to index. `success` and `latency_ms` mirror the names used by `agent.mcp_audit.AuditEntry` to avoid a translation layer in #22 when wrapping an existing audit log entry into a trace element. `error` is **not** drawn from `agent.error_classifier.ErrorCategory` — that enum is LLM-call-scoped (RATE_LIMIT, AUTH, NETWORK, …) and does not describe tool-call failure modes. Tool-call errors carry whatever short string the failing tool returned.

### Outcome

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `latency_ms` | int | no | `TraceBuilder.finish()` | UI list view; reflector latency rollups |
| `user_reaction` | jsonb | yes | populated lazily — see below | reflector (graded turn outcomes); optimizer (label source) |
| `data_sensitivity` | enum | no | `TraceBuilder.start(data_sensitivity=...)` | UI filter; optimizer eligibility check; **#25 privacy regression** |

`user_reaction` shape:

```jsonc
{
  "thumbs": 1,                           // -1, 0, +1, or null
  "followup_correction": false,          // true if a follow-up turn was inferred to correct this one
  "source": "explicit"                   // "explicit" | "inferred"
}
```

`data_sensitivity` mirrors `agent.error_classifier.DataSensitivity` exactly:

- `local_only` — raw personal data; trace stays in Pepper Postgres.
- `sanitized` — summary-shaped data; safe to surface in cross-archetype joins.
- `public` — no personal data.

Note: even `data_sensitivity = public` traces are local-only at the *storage* layer. The column annotates *content* sensitivity, not destination — destination is hard-locked to local Postgres for every trace.

### Embedding

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `embedding` | vector(1024) | yes | async post-persist worker (#22 step 5) | UI "find similar" (#24); reflector retrieval (#39); optimizer clustering (#45) |
| `embedding_model_version` | text | yes | same writer; required when `embedding` is non-null | reflector (skip rows whose embedding model is incompatible with current query embedding) |

### Compression tier

| Field | Type | Nullable | Writer | Reader |
|---|---|---|---|---|
| `tier` | enum (`working` \| `recall` \| `archival`) | no | `TraceBuilder.start()` initializes to `working`; nightly job (#21) advances tier | UI filter; reflector access window decisions |

Tier semantics live in [#21](https://github.com/jacksteroo/Pepper/issues/21).

## Indexing

Defined in #20's migration. Required indexes:

- B-tree on `created_at` — every reflector and UI query is time-windowed.
- B-tree on `(archetype, created_at)` — per-archetype recent traces. Cardinality of `archetype` is small (4) but the composite index lets `WHERE archetype = X ORDER BY created_at DESC` skip a sort.
- B-tree on `(model_selected, created_at)` — UI list filter by model (#24); reflector model-comparison rollups.
- GIN on `tools_called` — "which traces called X". Use `jsonb_path_ops` to keep the index tight; Postgres still supports the `@>` containment operator that "called X" queries use.
- HNSW on `embedding`, **partial** with `WHERE embedding IS NOT NULL` — pgvector rejects nulls in HNSW; the partial predicate keeps recall-tier rows (where the embedding is dropped) out of the index.
- Full-text search on `(input || ' ' || output)` is **not** required for v0 — the UI's `contains-text` filter uses `LIKE '%term%'` until search volume justifies the index cost. Document the upgrade path in the migration's README.

Maintenance notes (informational, not migration-blocking):

- HNSW degrades on growing tables. Once the working tier exceeds ~100k rows, run `REINDEX INDEX CONCURRENTLY` on the embedding index nightly. Recall-tier rows are excluded from HNSW (partial predicate above), so the index stays bounded by the working window even as the table grows.

## Read patterns (mandate for #24 and downstream consumers)

The schema includes a 4 KB `embedding` and unbounded jsonb columns per row. Naive `SELECT *` queries OOM the reflector and slow the UI list. Consumers MUST project:

- **List view (#24):** `trace_id, created_at, trigger_source, archetype, model_selected, latency_ms, data_sensitivity, tier`. Never `SELECT *`. The list view also never returns `input` / `output` text — only the detail view does.
- **Detail view (#24):** full row except `embedding` (which is fetched only when the user clicks "Find similar").
- **Similarity search (#24):** project `(trace_id, embedding)` only when computing nearest-neighbor; then re-fetch full rows for the matches.
- **Reflector daily window (#39):** project `(trace_id, created_at, archetype, latency_ms, data_sensitivity, tools_called, output)`. Skip `embedding` and `assembled_context` unless the rollup needs them.
- **Optimizer rollups (#46):** project the columns relevant to the rollup; rely on the `(model_selected, created_at)` and `(archetype, created_at)` indexes.

#20's SQLAlchemy model SHOULD mark the `embedding`, `assembled_context`, and `tools_called` columns with `deferred()` so default loads exclude them; consumers opt back in via `undefer()` per query.

## Privacy invariants (enforced by #25)

- A trace row never appears in an outbound HTTP request body or MCP tool argument that targets a destination outside the host.
- The frontier-LLM dispatch site (`agent/llm.py`) does not accept a `Trace` instance as input. Traces feed reflectors which themselves call LLMs with summaries; the boundary is enforced by typing, not convention.
- The HTTP route that exposes traces (#24) is bound to localhost by default and audit-logged on every read.
- Compression's summarization step (#21) hard-pins to local Ollama, mirroring the existing context-compression invariant in `agent/llm.py`.
- `data_sensitivity` is set at `INSERT` and immutable for the row's lifetime. Reclassification means inserting a new derivative row, never updating the original. (See ADR-0005 §Mutability.)
- The `Trace` dataclass is `frozen=True` and its `__repr__` redacts `input`, `output`, `assembled_context`, and `tools_called`. A stray `logger.info(trace)` between now and #25's regression test does not leak RAW_PERSONAL into structured logs.

## Open questions resolved

- **#19 — pgvector vs relational-only?** pgvector. Per the storage decision in [ADR-0005](adr/0005-trace-schema.md). 1024-dim, `qwen3-embedding:0.6b`, HNSW index, `embedding_model_version` recorded per row.

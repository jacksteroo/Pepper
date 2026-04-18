# Pepper v0.2 — Release Manifest

**Tag:** `v0.2`
**Base:** `v0.1` (`fe08ee0`) → `c23f37b`
**Date:** 2026-04-18
**Scope:** 54 files changed, +10,484 / −410

v0.2 consolidates Phases 2, 4, 5, and 6 of the roadmap: Pepper now knows what time it is, encodes workflows as self-improving skills, speaks MCP in and out, routes queries by intent against a live capability registry, queues outbound writes for explicit approval, grades attention by user-specific priority signals, and treats untrusted web content as data rather than instructions.

## Highlights

- **Intent- and capability-aware routing.** Every turn classifies intent against a 9-rule deterministic router and checks source availability against a live capability registry before prompting the LLM. "Did Sarah send anything?" / "Who do I owe replies to?" / "Anything overnight?" now resolve without relying on substring heuristics.
- **Approval-gated outbound writes.** Every send/create action — email, iMessage, WhatsApp, calendar event, MCP write — queues as a reviewable draft. The UI shows the real recipient + body from the actual args; model-supplied descriptions are kept as advisory only. Per-action MCP approval is bound to the exact tool + args the user saw, so a drifting model cannot re-authorize a different write under an old approval.
- **MCP in both directions with privacy-preserving trust boundaries.** Pepper connects to external MCP servers, exposes subsystems as standalone MCP servers, and exposes a subset of its own tools as an MCP server for Claude Desktop / Claude Code / Cursor. Raw personal data cannot leave the local trust boundary; external servers receive summaries only. Enforced by 59 privacy-regression tests.
- **Self-improving skill system.** Repeatable workflows (morning brief, weekly review, commitment check, reply drafting, meeting prep) ship as `SKILL.md` files with a background reviewer that proposes improvements for human approval.
- **Web search is treated as untrusted input.** Brave snippets are sanitized and wrapped in `BEGIN/END UNTRUSTED SEARCH RESULTS` markers; a response post-processor rewrites hallucinated citation URLs to the canonical ones from the result set, appends a `Sources:` block when the model skips attribution.
- **Real timezone awareness.** `[Current time: …]` is injected into every system prompt from a configured IANA timezone; the scheduler uses the same zone for all local-time computations.

## What's in v0.2 by phase

### Phase 2 — timezone awareness (`35efeb6`)
- New `TIMEZONE` setting (IANA name, default `America/Los_Angeles`).
- `core.py` prepends `[Current time: …]` to the system prompt per request.
- `scheduler.py` uses `ZoneInfo(config.TIMEZONE)` for all `datetime.now()` calls.

### Phase 4 — self-improving skill system (`004f014`, `bff9553`)
- `agent/skills.py` — `SkillLoader` + `SkillMatcher`; injects matching skills into the system prompt as `<skill>` blocks per turn.
- `agent/skill_reviewer.py` — post-turn reviewer queues proposed diffs to `skills/.improvements_queue.json`; human-approved changes are written back and version-bumped.
- `agent/query_intents.py` — shared trigger-detection helpers; eliminates duplicated trigger lists across subsystems.
- `skills/` — initial library: `morning_brief`, `weekly_review`, `commitment_check`, `draft_reply_to_contact`, `prep_for_meeting`.
- Scheduler refactor: morning brief, weekly review, commitment check now call `pepper.chat()` guided by skill files; `BriefFormatter` deleted. Commitment check enforces the 48-hour cutoff in structured recall data before calling the LLM.

### Phase 5 — MCP integration (`5adea0d`)
- `agent/mcp_client.py` — MCP client: server lifecycle, tool discovery, call routing, structured-content + image + embedded-resource extraction.
- `agent/mcp_audit.py` — trust-level enforcement, data classification, audit log.
- `agent/mcp_server.py` — Pepper-as-MCP-server with `NEVER_EXPOSE` guardrail and per-key allowlists.
- `subsystems/mcp_base.py` + `subsystems/calendar/mcp_server.py` + `subsystems/communications/mcp_server.py` — subsystems exposed as standalone MCP servers.
- `config/mcp_servers.yaml` + `config/mcp_server_access.yaml` — external server config + access control.
- `agent/tool_router.py` — unified native+MCP routing, trust enforcement, `is_mcp_read_only_tool()`.
- Per-action MCP write approval gate: two-turn flow (propose → user affirm → execute), bound to exact tool + args the user saw, 5-minute TTL, read-only tools bypass, local servers bypass.
- Strict `allow_side_effects` bool validation — rejects quoted `"false"` that would otherwise evaluate truthy.
- 59 privacy regression tests in `test_mcp_privacy.py`, `test_mcp_router.py`, `test_mcp_client.py`.

### Phase 6 — intent/capability reliability + approval flows + web-search safety (`dac4dcd`, `c4c35c1`, `2bf0658`, `8d7980f`, merged as `c23f37b`)
**6.1 — QueryRouter** (`agent/query_router.py`)
Nine-rule deterministic router: generic capability checks, specific capability checks, cross-source triage, person-centric lookups, schedule lookups, action items, inbox summaries, conversation lookups, general chat. Runs before every prompt build; logs `query_route` for eval tracking.

**6.2 — Prompt/tool contract cleanup** (`agent/life_context.py`)
`build_capability_block()` now generates capability text from registered tool names. Fixed two stale names (`search_calendar_events → get_calendar_events_range`, `get_slack_messages → get_slack_channel_messages`). `validate_prompt_tool_references()` regression-tests the invariant.

**6.3 — CapabilityRegistry** (`agent/capability_registry.py`)
Per-source runtime status (`available | not_configured | permission_required | temporarily_unavailable | disabled`), populated at startup via async probes. Deterministic answers to capability questions. New `GET /capabilities` endpoint.

**6.4 — EA eval harness** (`agent/tests/test_exec_assistant_eval.py`)
30-case paraphrase-heavy corpus, parametrized so each failure is a named regression.

**6.5 — Router hardening**
Compound capability requests ("Can you read my email and tell me what's urgent?") now route to work intents, not `CAPABILITY_CHECK`. Cross-sentence compound detection via `_COMPOUND_MODAL_RE`. Kinship terms (mom/dad/wife/boss/…) extracted as entity targets. Routing-driven trigger augmentation activates proactive fetchers even when phrasing has no source keywords.

**6.6–6.7 — Executive-judgment behaviors + approval hardening**
- `agent/pending_actions.py` — draft-and-queue for every outbound write. LLM calls `queue_outbound_action`; UI surfaces approve / edit / reject.
- `web/src/components/Status.tsx` — approval UI renders recipient/subject/body from actual queued args, not from a model-supplied preview. Model description shown separately as advisory only.
- `_execute_tool` takes `skip_mcp_write_gate=True` when the caller is `PendingActionsQueue`, so approved drafts don't get re-gated and misclassified as executed. Defense-in-depth: the queue treats `approval_required` responses as failures.
- `agent/commitment_followup.py` — scheduler keeps a persistent instance; follows through on commitments captured in conversation.
- `agent/priority_grader.py` — non-learning v1 grader (`urgent | important | defer | ignore`) applied to email action-items, email summaries, and iMessage / WhatsApp attention flows via `_apply_priority_tags_to_attention`. VIPs extracted from life-context.
- Web-search URL grounding in `core.py` — response post-processor rewrites hallucinated markdown / bare links to canonical URLs from the actual result set, appends a `Sources:` block when the model skips attribution. Applies to both `search_web` tool calls and proactive `_maybe_search_web` context. URL normalization (lowercase scheme/host, strip trailing slash) so comparison is robust.

**Security — untrusted Brave snippets**
Brave titles/descriptions were copied verbatim into the system prompt's `web_context` block, giving a malicious page direct access to the highest-trust channel. Each snippet is now sanitized (collapse newlines/tabs/control chars, title capped at 240, description at 480), reframed as untrusted quoted data with explicit `--- BEGIN/END UNTRUSTED SEARCH RESULTS ---` markers and anti-instruction guidance. Regression tests cover injection payloads and oversized snippets.

## New modules

| Area | Module |
|------|--------|
| Routing | `agent/query_router.py`, `agent/query_intents.py` |
| Capabilities | `agent/capability_registry.py` |
| MCP (client) | `agent/mcp_client.py`, `agent/mcp_audit.py` |
| MCP (server) | `agent/mcp_server.py`, `subsystems/mcp_base.py`, `subsystems/calendar/mcp_server.py`, `subsystems/communications/mcp_server.py` |
| Skills | `agent/skills.py`, `agent/skill_reviewer.py`, `skills/*.md` |
| Approval / follow-through | `agent/pending_actions.py`, `agent/commitment_followup.py` |
| Priority | `agent/priority_grader.py` |
| Config | `config/mcp_servers.yaml`, `config/mcp_server_access.yaml` |

## Notable behavior changes

- Outbound writes no longer execute from chat turns directly; the model calls `queue_outbound_action` and the user approves from the web UI.
- MCP write tools require per-action approval in the chat channel (two-turn flow) unless the server is local.
- The capability block in the system prompt is generated from code, not hand-written. Stale tool names in the prompt are now a test failure.
- Morning brief / weekly review / commitment check are driven by skill files, not Python formatters.
- Web search context in the system prompt is explicitly marked untrusted and sanitized.
- Pepper no longer says "we" / "our" / "my team"; it addresses the current user turn only.

## Test plan

- `.venv/bin/pytest -q agent/tests/` — full suite, **642 passed**.
- Privacy regression: `agent/tests/test_mcp_privacy.py` (33), `test_mcp_router.py`, `test_mcp_client.py`.
- Approval gate: `TestMCPWriteApprovalGate`, `TestPendingActionsMCPExecution` in `test_core.py`.
- Routing / capability: `test_query_router.py`, `test_capability_registry.py`, `test_exec_assistant_eval.py`.
- Web-search safety: `test_core.py -k "ground or search_result_context or search_web_links or untrusted"`.
- Frontend: `npx tsc --noEmit` in `web/`.

## Upgrade notes

- Add `TIMEZONE` to `.env` (defaults to `America/Los_Angeles`).
- External MCP servers must declare `allow_side_effects: true` (strict bool) in `config/mcp_servers.yaml` to permit writes; writes still require per-action user approval.
- The `search_web` tool result now carries a `citation_rules` advisory string alongside `results`; consumers that iterate result keys should ignore unknown keys.
- `PendingAction.preview` is now server-derived from args; any downstream consumer that trusted the free-text `preview` for authorization should switch to `args` or the newly-added `model_description` field.

# Pepper — Continuous Improvement Plan

## Purpose

Turn Pepper from a capable local assistant into a trusted life and executive assistant that:

- notices what matters without being asked
- knows when to interrupt and when to stay quiet
- tracks commitments and follows through
- understands family, work, household, and time together
- researches and recommends well
- drafts actions safely and asks for approval only when appropriate
- continuously improves from real usage, simulated chats, and measured failures

This document is the operating plan for that evolution.

It is intentionally not a generic "AI assistant roadmap." It is grounded in what Pepper already has: life context, memory, scheduler, communications, calendar, routing, skills, pending actions, simulator transcripts, and hallucination checks.

---

## North Star

Pepper is "alive" when these statements are true most days:

- Pepper reliably surfaces the 1-3 things that actually deserve attention.
- Pepper catches urgent family, work, and logistics issues before you do.
- Pepper is calm and quiet when nothing important is happening.
- Pepper drafts useful actions and queues them for review instead of making you start from zero.
- Pepper remembers people, timing, promises, patterns, and open loops accurately.
- Pepper knows the difference between urgent, important, sensitive, and ignorable.
- Pepper improves from mistakes through instrumentation, evals, and small corrective iterations.

Perfection here does not mean "fully autonomous writes." It means "deeply trustworthy judgment under strong approval and privacy guardrails."

---

## What A True Life + Executive Assistant Must Do

Each area below is the capability ceiling. Pepper does not need all of it at once — the subphases below climb it deliberately.

### 1. Attention Management

- Triage inboxes, messages, Slack, and calendar into urgent / important / defer / ignore.
- Tell you what changed overnight, this morning, today, this week, and before the next major event.
- Notice when something time-sensitive is slipping.
- Decide *which channel* (Telegram push, web-UI card, silent log) and *what tone* to use — not every surfacing is an interruption.

### 2. Calendar and Time Protection

- Know what is happening today, this week, and around major deadlines.
- Prep you ~30 min before every meeting: attendees, last conversation, open items, suggested talking points.
- Capture action items and follow-up commitments ~5 min after a meeting ends.
- Detect conflicts, overbooking, travel friction, and unrealistic days.

### 3. Relationship Management

- Know who matters most.
- Notice quiet contacts, overdue replies, and relationship imbalance.
- Track birthdays, anniversaries, school deadlines, family events, and people who need a check-in.
- Build a pre-call context pack when a known contact is about to be reached.

### 4. Commitment Follow-Through

- Remember promises you made — explicit ("I'll send it by Friday") and implicit ("we should really do X").
- Re-surface them at the right moment.
- Distinguish resolved, stale, blocked, and still-active commitments.

### 5. Research and Recommendations

- Research travel, household, school, health, vendors, purchases, and local options.
- Present recommendations with current facts, tradeoffs, and why they matter for your family.
- Refresh time-sensitive facts before advising.
- Carry a research thread over days instead of restarting from zero.

### 6. Household and Family Operations

- Track logistics for children, spouse/partner, parents, travel, meals, bills, forms, renewals, and home tasks.
- Suggest who to contact and what to do next.
- Notice when family priorities conflict with work priorities.

### 7. News and External Awareness

- Surface important news relevant to your life, work, safety, and location.
- Distinguish breaking news from noise: freshness window + multi-source convergence before escalating.
- Emergency news (weather, safety, events in known family regions) follows a different, faster threshold.

### 8. Communication Drafting and Escalation

- Draft replies, follow-ups, reminders, and nudges in the right tone across every channel Pepper can read.
- Know when a draft is enough, when approval is needed, and when escalation is warranted.
- Never send sensitive communication without the right approval path.
- Route every outbound write through the pending-actions queue.

### 9. Memory and Judgment

- Build durable memory about people, goals, open loops, and routines.
- Use that memory to prioritize correctly, not just answer questions.
- Stay honest about uncertainty and missing access.

### 10. Self-Maintenance

- Detect degraded integrations, stale auth, slow tools, and routing failures.
- Evaluate itself continuously via the simulator + eval corpus.
- Turn repeated failures into explicit product work within 24-48 hours.
- Run a nightly self-retrospective that becomes weekly tuning proposals.

---

## Pepper Today: Comparison

### Gap Matrix

| Area | Status | Notes |
|---|---|---|
| Read across 5 comms channels | ✅ | Gmail, Yahoo, iMessage, WhatsApp, Slack |
| Priority grading v1 | ✅ | `PriorityGrader` — non-learning rule-based |
| Quiet-contact detection | ✅ | `find_quiet_contacts`, comms-health dashboard |
| Commitment capture + slot-based re-surfacing | ✅ | `CommitmentExtractor` + `CommitmentFollowup` |
| Clarifying-question path | ✅ | `needs_clarification` wired end-to-end in 6.7 |
| Draft-and-queue outbound writes | ✅ plumbing | `PendingActionsQueue` in-memory; only email draft skill uses it |
| Capability honesty | ✅ | Registry live, refreshes on failure, periodic re-probe |
| Simulator + hallucination checks + EA eval corpus | ✅ | `scripts/pepper_simulator.py`, `scripts/pepper_eval.py`, `test_exec_assistant_eval.py` |
| Morning brief | ✅ | Skill-driven; news integration partial |
| Pre-event prep fires automatically | 🟡 | `prep_for_meeting.md` exists but no scheduler trigger per event |
| Post-meeting capture | ❌ | No "meeting ended" hook |
| Reply drafting across all channels | 🟡 | Email only; SMS/iMessage/WhatsApp/Slack not covered |
| SLA follow-up on unreplied threads | ❌ | Per-thread tracking missing |
| Durable pending actions across restarts | ❌ | In-memory only |
| Birthdays / anniversaries / life events | ❌ | No registry, no surfacing |
| Pre-call context pack trigger | 🟡 | Contact enricher exists but not wired to call/meeting triggers |
| Household operations layer | ❌ | No structured ops queue |
| Research workflows with recency + preference memory | ❌ | Ad-hoc web search only |
| News digest filtered to life context | 🟡 | Brave Search integrated; no scheduled curated digest |
| Breaking-news watcher | ❌ | No RSS polling, no convergence rule |
| Emergency-news geolocation watcher | ❌ | Nothing |
| Autonomous outbound initiation | ❌ | Pepper speaks when spoken to or on morning cron |
| Channel + tone selection | ❌ | All proactive pushes go to Telegram |
| Attention engine (shared decision layer) | ❌ | Every proactive path makes its own decision |
| Daily self-retrospective | ❌ | Nothing structured |
| Adaptive tuning from real response patterns | ❌ | Priority grader is rule-only |

---

## The Real Gap

The biggest missing piece is not raw capability surface area. It is judgment orchestration:

- what deserves interruption
- what can wait for the morning brief
- what is just background noise
- what requires research before advice
- what is safe to draft automatically
- what should be escalated because it affects family, money, safety, or reputation

The continuous improvement program should therefore optimize for trust and judgment first, then expand capability surface area.

---

## Continuous Improvement Program

### Track 1 — Build The Attention Engine

Create a single decision layer that every proactive Pepper behavior uses.

**Inputs:**

- calendar proximity
- sender / contact importance
- relationship health
- commitment state
- keyword urgency
- event type
- time of day (+ quiet hours)
- owner preferences from `docs/LIFE_CONTEXT.md`
- source confidence / capability status
- news severity and local relevance

**Outputs:**

- `interrupt_now`
- `show_in_next_brief`
- `queue_as_pending_action`
- `watch_silently`
- `ignore`

Plus a chosen `delivery_channel` (Telegram urgent / Telegram normal / web-UI card / silent log) and `tone`.

**First implementation tasks:**

- introduce a shared `AttentionDecision` model that wraps the existing `PriorityGrader` as one signal among several
- externalize thresholds in `config/proactive_rules.yaml` so they are tuneable without code
- centralize morning brief, commitment follow-ups, comms-health prompts, and future news/research surfacing behind it
- add explicit reasons for every proactive surfacing decision
- log every proactive decision for later review

**Success criteria:**

- every proactive alert is traceable to a visible decision record
- false-positive interruptions decrease over time
- important-but-not-urgent items land in briefs instead of noisy interrupts

### Track 2 — Build The Personal Operations Layer

Pepper needs first-class structured objects for life operations, not just free-text memory.

**Add durable models for:**

- people and relationship milestones
- family events and school deadlines
- household tasks and renewals
- commitments and pending actions
- research topics and recommendations
- recurring seasonal events: birthdays, anniversaries, travel prep, school cycles, taxes

**Likely first additions:**

- `subsystems/people/` (the first real inhabitant of the deferred People layer — deliberately minimal and implementation-agnostic)
- birthday / anniversary registry sourced from macOS Contacts (read-only, local) + life context, user-confirmed before stored
- life-event ledger: job change, relocation, health event, major project — detected from conversation + comms, always user-confirmed
- household ops queue: forms, bills, renewals, kid logistics, travel tasks
- recommendation memory: what Pepper suggested, what you chose, and whether it worked
- pre-call context pack: when a calendar event has a known attendee, auto-build a prep card

**Success criteria:**

- "who should I reach out to?" and "what family things are coming up?" feel grounded
- birthdays and key family dates are proactively surfaced
- Pepper can carry a research thread over days instead of restarting from zero

### Track 3 — Build The Research + Recommendation Loop

Pepper should become good at answering:

- what should we book?
- who should we call?
- what should we buy?
- what are the best options for the family right now?

**Plan:**

- add a `SituationDetector` that inspects calendar + memory nightly and emits situation records (upcoming trip, recurring unresolved decision, topic the principal has asked about repeatedly)
- `proactive_research` skill fires on situation records; drafts a research brief (Claude for drafting, summaries only; local for all personal-data context)
- research workflow captures requirements, sources checked, recency, and recommendation rationale
- separate timeless preferences from time-sensitive facts
- require current verification for travel, prices, schedules, weather, school deadlines, and news
- persist recommendation outcomes so Pepper learns your actual taste and trust thresholds

**Success criteria:**

- recommendations cite fresh evidence
- Pepper remembers household preferences
- research output becomes more concise and more accurate over repeated cycles

### Track 4 — Build The News + Alerting Layer

Pepper should not merely "have web search." It needs policy for what news matters.

**Classes:**

- emergency / safety
- local breaking news
- market / work-relevant developments
- family-relevant disruptions: weather, traffic, school, travel
- general background headlines for the brief

**Implement:**

- `news_digest` scheduled skill: Brave Search + curated RSS, filtered against life-context interests via local LLM, summarized to 5-7 items
- breaking-news watcher: polls trusted RSS every ~15 min; escalates only when (topic matches interests) AND (freshness < 30 min) AND (multiple trusted outlets converge)
- emergency-news watcher: geolocation-matched to life-context regions (principal + family); a single trusted source is sufficient for weather/disaster/security
- escalation routes through the Attention Engine → `interrupt_now` with `Telegram urgent` channel
- quiet-hours policy suppresses non-emergency escalations

**Success criteria:**

- emergency news bypasses normal brief cadence
- routine headlines stay out of the way unless relevant
- the morning brief contains useful outside-world context, not generic noise

### Track 5 — Harden The Action Loop

Pepper should close loops safely.

**Improve:**

- persist `PendingActionsQueue` in PostgreSQL so drafts survive restarts
- outbound action categories (email, iMessage, WhatsApp, Slack, calendar event create) with per-category approval policies
- extend `draft_reply_to_contact` to every readable channel, each gated through the queue
- `reply_sla_tracker` scheduled skill: flags threads where the principal read a message > N hours ago without replying; N is per-contact, slowly learned from real reply latency
- follow-up reminders on queued but unapproved drafts
- clearer approval UX in web and Telegram
- explicit "why Pepper suggested this action now" line on every queued draft

**Success criteria:**

- no good draft disappears on restart
- draft → approve/edit/reject becomes a daily habit path
- commitment capture and action suggestion feel connected

### Track 6 — Build The Self-Improvement Harness

This is the meta-layer that makes Pepper better every week. Built on the existing simulator + eval scripts, not from scratch.

**Every failure should end up in one of these buckets:**

- routing failure
- missing tool or missing data source
- stale or weak life context
- poor prioritization
- weak drafting quality
- missing clarification
- false urgency
- missed urgency
- hallucination / overclaim
- degraded subsystem / auth / permission issue

**For each bucket, define:**

- how it is detected
- where it is logged
- what test/eval reproduces it
- what "fixed" means

**New infrastructure:**

- **Scenario specs** (`agent/tests/ea_scenarios/*.yaml`): multi-turn scripted situations with input state, simulated events over time, required and forbidden behaviors, scoring weights. Examples: morning-with-urgent-slack, mom-birthday-tomorrow, meeting-prep-auto, flight-to-sf, breaking-news-family-area, stopped-replying, false-alarm. Extends the existing `pepper_simulator` themes with deterministic, graded scenarios.
- **`CritiqueReviewer`**: generalizes `SkillReviewer` so it accepts a scenario transcript + expected behaviors and emits proposed fixes (skill diff, routing rule, prompt edit, new tool). Fixes land in a shared "Pepper improvements" review queue in the web UI.
- **`daily_retro` skill**: runs nightly on local Ollama; reads the day's conversations, proactive pushes, tool failures, approvals/rejections; produces a "what I got right / what I missed / what to do differently tomorrow" note; saves to memory tagged `self_retro`.
- **`retro_rollup` skill**: weekly; reads the past 7 retros; proposes adjustments to priority-grader weights, attention-engine thresholds, news-digest item count, reply-SLA windows per contact. Proposals land in the improvements queue.

**Success criteria:**

- repeated mistakes turn into evals and tests within 24-48 hours
- quality improves because of failures, not in spite of them
- the improvements queue is the primary source of Pepper's own roadmap within 6 weeks

---

## Operating Loop: How We Improve Pepper Continuously

This is the loop to run over and over.

### Step 1. Capture Failures

Sources:

- real chats
- morning brief output
- weekly review output
- pending-action approvals/rejections
- simulator transcripts (`scripts/pepper_simulator.py`)
- hallucination detector
- routing eval regressions (`test_exec_assistant_eval.py`)
- capability failures and auth degradations

Artifacts:

- tagged log entries
- short failure notes in docs or issue tracker
- new eval cases copied from the exact failing phrasing
- new scenario specs under `agent/tests/ea_scenarios/` when the failure is multi-turn

### Step 2. Write The Smallest Correct Plan

For each iteration, define:

- exact failure being fixed
- metric that should improve
- files likely to change
- what should be tested manually
- what should be tested automatically

Keep scope tight. One judgment behavior at a time beats broad rewrites.

### Step 3. Implement

Typical change shapes:

- prompt / skill improvement
- router / grader / scheduler logic
- attention-engine rule change in `proactive_rules.yaml`
- new structured model or queue
- UI for status, approvals, or observability
- new subsystem or MCP integration

### Step 4. Restart Pepper

After meaningful runtime changes:

- restart backend
- verify health
- verify scheduler
- verify capability registry state

### Step 5. Simulate Real Interactions

Run targeted and broad checks:

- `python scripts/pepper_simulator.py --theme <theme> --once`
- `python scripts/pepper_eval.py --since 10m`
- `pytest agent/tests/ea_scenarios/<scenario>.py` for the specific failing scenario
- targeted `pytest` for touched areas
- one or two manual chats that match the real use case

### Step 6. Inspect Faults

Look at:

- wrong tools called
- no tools called when tools were needed
- low-quality prioritization
- weak explanations
- generic or noisy proactive behavior
- missed or excessive clarification
- state lost across turns or restarts
- privacy-boundary violations (hard fail — any single violation rejects the iteration)

### Step 7. Fix And Re-Test

Add one or more of:

- regression test
- simulator theme
- scenario spec
- eval corpus case
- logging/audit improvement
- code fix

Then rerun the same scenario until the failure stops.

### Step 8. Promote The Next Capability

Once a behavior is stable:

- add the next adjacent behavior
- avoid stacking two new judgment systems at once
- prefer extending the harness before extending autonomy
- let the `retro_rollup` output (once Track 6 lands) drive what gets promoted next

---

## What To Measure

Pepper should be judged on a small visible scorecard.

### Judgment Metrics

- urgent precision: how often an "interrupt now" item was truly urgent
- urgent recall: how often Pepper missed something that should have interrupted
- brief usefulness: percentage of brief items the owner considered genuinely useful
- draft usefulness: percentage of queued drafts needing only light edits
- recommendation trust: percentage of recommendations accepted or shortlisted

### Reliability Metrics

- tool-grounding rate on data-dependent questions
- hallucination rate from `scripts/pepper_eval.py`
- routing accuracy from `test_exec_assistant_eval.py`
- subsystem availability and auth freshness
- pending-action survival across restart
- privacy-boundary violation count (must stay at 0)

### Behavior Metrics

- clarification rate on ambiguous queries
- false interruption count per week
- missed commitment resurfacing count
- quiet-contact / relationship reminder usefulness
- pre-event prep usefulness

Expose this as `/scorecard` endpoint + a panel in the web UI.

---

## Priority Order

The right order is:

1. Trust and attention policy
2. Durable action loop
3. Family / household operations
4. Research and recommendations
5. News and external awareness
6. Broader autonomy and self-maintenance

Reason: a noisy or poorly prioritized assistant becomes a burden even if it has many integrations.

---

## Immediate 6-Week Plan

Each week's work plugs into the tracks above. Each week must end with scenarios green in `pepper_simulator` *and* a restart-and-test pass in live chat.

### Week 1-2: Instrument Judgment

- create a failure taxonomy and logging convention for proactive decisions
- ship the `/scorecard` endpoint + web UI panel
- expand simulator themes around urgency, news, family logistics, and relationship reminders
- add the first 6 scenario specs under `agent/tests/ea_scenarios/` (see Track 6 list)
- turn recent real failures into eval cases

### Week 2-3: Attention Engine V1

- introduce `AttentionDecision` + `config/proactive_rules.yaml`
- route morning brief items, commitment follow-ups, and comms-health prompts through one scoring path
- add channel + tone selection (v1 rule-based)
- log why each item was surfaced
- scenario coverage: false-alarm + missed-urgency cases

### Week 3-4: Durable Action Loop + Pre/Post-Event

- persist `PendingActionsQueue` in Postgres; add restart-safe retrieval and approval state
- surface stale queued drafts and remind appropriately
- scheduler dispatches `prep_for_meeting` 30 min before every calendar event
- new `post_meeting_capture` skill runs 5 min after each event
- scenario coverage: meeting-prep-auto, stale-draft-revived-after-restart

### Week 4-5: Family Ops V1

- stand up `subsystems/people/` with macOS Contacts read + life-event ledger
- birthday / anniversary registry with proactive surfacing via Attention Engine
- household ops queue: first pass on forms, bills, renewals, kid logistics
- life-context-backed tests for family priority conflicts
- scenario coverage: mom-birthday-tomorrow, family-vs-work-conflict

### Week 5-6: News + Research V1

- `news_digest` + breaking-news watcher + emergency-news watcher (Track 4)
- `SituationDetector` + `proactive_research` skill (Track 3)
- verify all surfacing goes through Attention Engine (no back-door interrupts)
- verify privacy boundaries: news/research never sends raw personal data externally
- scenario coverage: breaking-news-family-area, flight-to-sf research card

### Week 6 closing: Self-Improvement Wiring

- `CritiqueReviewer` generalizes `SkillReviewer`
- `daily_retro` skill scheduled nightly
- `retro_rollup` scheduled weekly
- Pepper improvements queue becomes the primary roadmap input from here on

---

## Definition Of "Ready For Daily Trust"

Pepper is ready to behave like a true life and executive assistant when:

- it can run for weeks without manual babysitting
- it rarely interrupts for the wrong reason
- it reliably catches urgent communication and time-sensitive family logistics
- it drafts useful follow-ups and preserves them safely
- it surfaces birthdays, deadlines, renewals, and important relationships proactively
- it gives researched recommendations with current facts
- it explains why it is surfacing something
- every important failure becomes a reproducible test or eval

---

## Practical Rule For Every Iteration

Do not ask "what feature should Pepper have next?"

Ask:

- what important thing did Pepper miss?
- what noisy thing did Pepper surface unnecessarily?
- what judgment call did Pepper get wrong?
- what loop did Pepper fail to close?
- what behavior should become measurable before we trust it more?

If we keep answering those questions with small, test-backed iterations, Pepper will come to life the right way.

---

## Out Of Scope (Explicitly Deferred)

- Health + finance subsystems — remain on `docs/WISHLIST.md`; their own future phase
- Voice interface — tracked on `docs/ARCHITECTURE.md` as Future
- Full People subsystem integration — `subsystems/people/` added in Week 4-5 is deliberately minimal and replaceable; deeper relationship intelligence only if usage demands it
- macOS desktop app — tracked separately in `docs/MACOS_DESKTOP_APP_PLAN.md`
- Red-team / security-agent layer — on `docs/WISHLIST.md`

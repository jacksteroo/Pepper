"""
Phase 6.4 — Evaluation Harness for Executive Assistant Reliability.

Measures whether Pepper's QueryRouter correctly classifies natural-language
EA asks at the top of the funnel. Unlike other test files that test happy-path
behavior, this harness tracks EA-specific failure modes:

  - Wrong intent classification  (e.g. GENERAL_CHAT when it should be PERSON_LOOKUP)
  - Wrong source routing         (email when user meant iMessage)
  - Missed entity extraction     (person name dropped)
  - Unnecessary clarification    (action_mode=ASK when it should be CALL_TOOLS)

Run standalone to see a metric summary:
    pytest agent/tests/test_exec_assistant_eval.py -v --tb=short

The test IDs act as a regression corpus. New cases should be added here as
real routing failures are observed in production logs (query_route log lines).
"""
from __future__ import annotations

import pytest
from dataclasses import dataclass
from typing import Callable

from agent.query_router import ActionMode, IntentType, QueryRouter, RoutingDecision


# ── Eval case structure ────────────────────────────────────────────────────────

@dataclass
class EvalCase:
    message: str
    expected_intent: IntentType
    expected_sources_contain: list[str]   # at least these sources must be present
    expected_action_mode: ActionMode
    expected_entities: list[str] = None   # if set, at least one must appear
    description: str = ""


# ── Eval corpus ────────────────────────────────────────────────────────────────
# These are paraphrase-heavy EA queries drawn from the Phase 6 roadmap.
# Categories: capability checks, inbox summaries, action items, person lookups,
# ambiguous source wording, mixed-source follow-ups, partial subsystem failure.

EVAL_CORPUS: list[EvalCase] = [
    # ── Capability checks ──────────────────────────────────────────────────────
    EvalCase(
        message="Do you have access to my messages?",
        expected_intent=IntentType.CAPABILITY_CHECK,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="ambiguous 'messages' — should be a capability check",
    ),
    EvalCase(
        message="Can you check my texts?",
        expected_intent=IntentType.CAPABILITY_CHECK,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="'check my texts' — capability check, not fetch",
    ),
    EvalCase(
        message="Do you have access to my email?",
        expected_intent=IntentType.CAPABILITY_CHECK,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="direct capability question about email",
    ),
    EvalCase(
        message="What can you access?",
        expected_intent=IntentType.CAPABILITY_CHECK,
        expected_sources_contain=["all"],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="generic capability question",
    ),
    EvalCase(
        message="Are you connected to Slack?",
        expected_intent=IntentType.CAPABILITY_CHECK,
        expected_sources_contain=["slack"],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="specific Slack capability check",
    ),

    # ── Inbox summaries ────────────────────────────────────────────────────────
    EvalCase(
        message="Anything important overnight?",
        expected_intent=IntentType.CROSS_SOURCE_TRIAGE,
        expected_sources_contain=["email"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'overnight' triage — should pull from comms sources",
    ),
    EvalCase(
        message="What came in this morning?",
        expected_intent=IntentType.CROSS_SOURCE_TRIAGE,
        expected_sources_contain=["email"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'this morning' cross-source triage",
    ),
    EvalCase(
        message="Summarize my emails",
        expected_intent=IntentType.INBOX_SUMMARY,
        expected_sources_contain=["email"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="email inbox summary",
    ),
    EvalCase(
        message="What's in my WhatsApp groups?",
        expected_intent=IntentType.CONVERSATION_LOOKUP,
        expected_sources_contain=["whatsapp"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="WhatsApp group lookup",
    ),
    EvalCase(
        message="Latest from Slack?",
        expected_intent=IntentType.INBOX_SUMMARY,
        expected_sources_contain=["slack"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="Slack latest messages",
    ),

    # ── Action items / follow-ups ──────────────────────────────────────────────
    EvalCase(
        message="Who do I owe replies to?",
        expected_intent=IntentType.CROSS_SOURCE_TRIAGE,
        expected_sources_contain=["email"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'owe replies' — should not route to GENERAL_CHAT",
    ),
    EvalCase(
        message="What needs my attention right now?",
        expected_intent=IntentType.CROSS_SOURCE_TRIAGE,
        expected_sources_contain=["email"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'needs my attention' triage",
    ),
    EvalCase(
        message="Any action items from Slack?",
        expected_intent=IntentType.ACTION_ITEMS,
        expected_sources_contain=["slack"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="Slack-specific action items",
    ),
    EvalCase(
        message="What do I need to follow up on?",
        expected_intent=IntentType.ACTION_ITEMS,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="generic follow-up request",
    ),
    EvalCase(
        message="What am I missing?",
        expected_intent=IntentType.CROSS_SOURCE_TRIAGE,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'what am i missing' — classic EA triage",
    ),

    # ── Person-centric lookups ─────────────────────────────────────────────────
    EvalCase(
        message="Did my mom send anything?",
        expected_intent=IntentType.PERSON_LOOKUP,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'mom' person lookup — should not route to GENERAL_CHAT",
    ),
    EvalCase(
        message="Any message from Sarah?",
        expected_intent=IntentType.PERSON_LOOKUP,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        expected_entities=["Sarah"],
        description="person lookup with name extraction",
    ),
    EvalCase(
        message="Has David replied?",
        expected_intent=IntentType.PERSON_LOOKUP,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        expected_entities=["David"],
        description="'has X replied' person lookup",
    ),
    EvalCase(
        message="Any word from the team?",
        expected_intent=IntentType.CROSS_SOURCE_TRIAGE,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'word from the team' — triage, not general chat",
    ),

    # ── Schedule lookups ───────────────────────────────────────────────────────
    EvalCase(
        message="What do I have today?",
        expected_intent=IntentType.SCHEDULE_LOOKUP,
        expected_sources_contain=["calendar"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'what do i have today' — must route to calendar, not general chat",
    ),
    EvalCase(
        message="Am I free tomorrow afternoon?",
        expected_intent=IntentType.SCHEDULE_LOOKUP,
        expected_sources_contain=["calendar"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="availability check",
    ),
    EvalCase(
        message="What meetings do I have this week?",
        expected_intent=IntentType.SCHEDULE_LOOKUP,
        expected_sources_contain=["calendar"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="meetings this week",
    ),

    # ── Ambiguous source wording ───────────────────────────────────────────────
    EvalCase(
        message="Check my messages",
        expected_intent=IntentType.CROSS_SOURCE_TRIAGE,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'messages' without source qualifier — should triage, not GENERAL_CHAT",
    ),
    EvalCase(
        message="Any texts?",
        expected_intent=IntentType.INBOX_SUMMARY,
        expected_sources_contain=["imessage"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'texts' → iMessage",
    ),
    EvalCase(
        message="What's happening on WhatsApp?",
        expected_intent=IntentType.CONVERSATION_LOOKUP,
        expected_sources_contain=["whatsapp"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="WhatsApp lookup, no action-item framing",
    ),

    # ── Compound capability requests (P1 fix) ─────────────────────────────────
    EvalCase(
        message="Can you read my email and tell me what's urgent?",
        expected_intent=IntentType.INBOX_SUMMARY,
        expected_sources_contain=["email"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="compound capability+work request must NOT short-circuit as capability check",
    ),
    EvalCase(
        message="Can you check my texts and show me the important ones?",
        expected_intent=IntentType.INBOX_SUMMARY,
        expected_sources_contain=["imessage"],
        expected_action_mode=ActionMode.CALL_TOOLS,
        description="'can you check texts and show me' = work request",
    ),

    # ── Kinship person lookups (P2 fix) ───────────────────────────────────────
    EvalCase(
        message="Any messages from mom?",
        expected_intent=IntentType.PERSON_LOOKUP,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        expected_entities=["mom"],
        description="kinship term 'mom' must be extracted as entity",
    ),
    EvalCase(
        message="Any word from dad?",
        expected_intent=IntentType.PERSON_LOOKUP,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.CALL_TOOLS,
        expected_entities=["dad"],
        description="'word from dad' — kinship person lookup",
    ),

    # ── True general chat ──────────────────────────────────────────────────────
    EvalCase(
        message="How are you?",
        expected_intent=IntentType.GENERAL_CHAT,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="pure greeting — must not trigger data fetches",
    ),
    EvalCase(
        message="Write me a haiku about coffee",
        expected_intent=IntentType.GENERAL_CHAT,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="creative writing — general chat",
    ),
    EvalCase(
        message="What's the capital of Japan?",
        expected_intent=IntentType.GENERAL_CHAT,
        expected_sources_contain=[],
        expected_action_mode=ActionMode.ANSWER_FROM_CONTEXT,
        description="world knowledge — general chat",
    ),
]


# ── Pytest parametrize ─────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def router():
    return QueryRouter()


def _case_id(case: EvalCase) -> str:
    return case.message[:50].replace(" ", "_").lower()


@pytest.mark.parametrize("case", EVAL_CORPUS, ids=[_case_id(c) for c in EVAL_CORPUS])
def test_routing_intent(router, case: EvalCase):
    """Intent type must match expected for all eval corpus messages."""
    decision = router.route(case.message)
    assert decision.intent_type == case.expected_intent, (
        f"Message: '{case.message}'\n"
        f"Description: {case.description}\n"
        f"Expected intent: {case.expected_intent.value}\n"
        f"Got intent:      {decision.intent_type.value}\n"
        f"Reasoning:       {decision.reasoning}"
    )


@pytest.mark.parametrize("case", EVAL_CORPUS, ids=[_case_id(c) for c in EVAL_CORPUS])
def test_routing_action_mode(router, case: EvalCase):
    """Action mode must match expected for all eval corpus messages."""
    decision = router.route(case.message)
    assert decision.action_mode == case.expected_action_mode, (
        f"Message: '{case.message}'\n"
        f"Description: {case.description}\n"
        f"Expected mode: {case.expected_action_mode.value}\n"
        f"Got mode:      {decision.action_mode.value}"
    )


@pytest.mark.parametrize(
    "case",
    [c for c in EVAL_CORPUS if c.expected_sources_contain],
    ids=[_case_id(c) for c in EVAL_CORPUS if c.expected_sources_contain],
)
def test_routing_sources(router, case: EvalCase):
    """Expected sources must be present in the routing decision."""
    decision = router.route(case.message)
    for expected_src in case.expected_sources_contain:
        assert decision.includes_source(expected_src), (
            f"Message: '{case.message}'\n"
            f"Description: {case.description}\n"
            f"Expected source '{expected_src}' in: {decision.target_sources}"
        )


@pytest.mark.parametrize(
    "case",
    [c for c in EVAL_CORPUS if c.expected_entities],
    ids=[_case_id(c) for c in EVAL_CORPUS if c.expected_entities],
)
def test_routing_entity_extraction(router, case: EvalCase):
    """At least one expected entity must be extracted from the message."""
    decision = router.route(case.message)
    extracted = {e.lower() for e in decision.entity_targets}
    assert any(e.lower() in extracted for e in case.expected_entities), (
        f"Message: '{case.message}'\n"
        f"Expected entities (one of): {case.expected_entities}\n"
        f"Got: {decision.entity_targets}"
    )


# ── Metric summary ─────────────────────────────────────────────────────────────

def test_routing_metric_summary(router):
    """Compute and print routing accuracy metrics for the full corpus.

    This test always passes — it only prints metrics so CI can see them.
    Intent and action_mode accuracy must be 100% (enforced by the parametrized
    tests above); this summary shows per-category breakdown.
    """
    intent_correct = 0
    mode_correct = 0
    source_correct = 0
    entity_correct = 0

    intent_by_type: dict[str, dict] = {}

    for case in EVAL_CORPUS:
        d = router.route(case.message)

        # Tally per intent-type breakdown
        key = case.expected_intent.value
        if key not in intent_by_type:
            intent_by_type[key] = {"total": 0, "intent_ok": 0, "mode_ok": 0}
        intent_by_type[key]["total"] += 1

        if d.intent_type == case.expected_intent:
            intent_correct += 1
            intent_by_type[key]["intent_ok"] += 1

        if d.action_mode == case.expected_action_mode:
            mode_correct += 1
            intent_by_type[key]["mode_ok"] += 1

        if case.expected_sources_contain:
            if all(d.includes_source(s) for s in case.expected_sources_contain):
                source_correct += 1

        if case.expected_entities:
            extracted = {e.lower() for e in d.entity_targets}
            if any(e.lower() in extracted for e in case.expected_entities):
                entity_correct += 1

    n = len(EVAL_CORPUS)
    n_with_sources = sum(1 for c in EVAL_CORPUS if c.expected_sources_contain)
    n_with_entities = sum(1 for c in EVAL_CORPUS if c.expected_entities)

    print(f"\n{'='*60}")
    print(f"EA Routing Eval — {n} cases")
    print(f"{'='*60}")
    print(f"  Intent accuracy:      {intent_correct}/{n} ({100*intent_correct//n}%)")
    print(f"  Action-mode accuracy: {mode_correct}/{n} ({100*mode_correct//n}%)")
    if n_with_sources:
        print(f"  Source accuracy:      {source_correct}/{n_with_sources} ({100*source_correct//n_with_sources}%)")
    if n_with_entities:
        print(f"  Entity accuracy:      {entity_correct}/{n_with_entities} ({100*entity_correct//n_with_entities}%)")
    print(f"\nPer-intent breakdown:")
    for intent_type, counts in sorted(intent_by_type.items()):
        t = counts["total"]
        i_ok = counts["intent_ok"]
        m_ok = counts["mode_ok"]
        print(f"  {intent_type:<30} intent={i_ok}/{t}  mode={m_ok}/{t}")
    print(f"{'='*60}")

    # This test always passes — the parametrized tests enforce correctness
    assert True

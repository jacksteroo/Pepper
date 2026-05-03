"""Router-classifier adapter — Epic 05 (#47).

Optimizes the **exemplar set + system instruction** that feed the
semantic intent classifier. The runner currently lives in this PR
as a structural validator; the LLM-driven scorer that calls
``agent.router_eval.evaluate`` against the candidate's exemplars is
operator-gated (requires a populated trace store + a way to spin up
``SemanticIntentClassifier`` with the candidate's exemplar set).

Coordination with #59
---------------------

Issue #47 calls out coordination with #59 (retire legacy regex router):
"After this, [the router] improves from traces" — i.e. the optimizer
becomes the only mechanism for router improvement once #59 lands.
This PR is purely additive; it does not touch the regex path.

Why a JSON-shaped "prompt"
--------------------------

The router prompt is more than a single template string — it's the
union of (a) the exemplar set the k-NN classifier indexes against and
(b) any system instruction wrapping it. Encoding both as a single
JSON document keeps the optimizer's mutation surface consistent
(strings in, strings out) while preserving the structure the runtime
needs to consume.

Default schema (validated by ``_structural_score``):

    {
      "instructions": "<system prompt for the classifier>",
      "exemplars": [
        {"query": "...", "intent_label": "..."},
        ...
      ]
    }

Mutations alter the instruction text and the exemplar count; they
must keep the schema valid so the gate's structural check passes.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agent.optimizer.adapters import register_adapter
from agent.optimizer.eval_gate import register_runner

if TYPE_CHECKING:  # pragma: no cover
    from agent.optimizer.schema import CandidatePrompt, TraceExample


TARGET_NAME: str = "router_classifier"

# Baseline exemplar set, intentionally small. The production router's
# real exemplar set is much bigger (under tests/router_*_seeds.jsonl);
# this default exists so the optimizer's loader has something to fall
# back to before the operator promotes a real one. Subclassing or
# re-registration is the path to swap in the production exemplar set.
DEFAULT_TEMPLATE: str = json.dumps(
    {
        "instructions": (
            "Classify the user's query into one of the supported intent "
            "labels. Return the single label that best matches; if "
            "uncertain, defer to clarification rather than guessing."
        ),
        "exemplars": [
            {
                "query": "When does my flight to Boston leave?",
                "intent_label": "schedule_lookup",
            },
            {
                "query": "Where is Susan staying for the tournament?",
                "intent_label": "person_lookup",
            },
            {
                "query": "Should I plan a trip with the family?",
                "intent_label": "general_chat",
            },
        ],
    },
    indent=2,
    sort_keys=True,
)

# Bounds on a candidate router prompt. Mostly sanity checks — the
# real volume comes from the production exemplar set, which is fed
# through this template by the runtime, not embedded in the prompt
# document itself.
_MIN_EXEMPLARS: int = 1
_MAX_EXEMPLARS: int = 200
_MAX_TEMPLATE_BYTES: int = 64 * 1024


def parse_template(template_text: str) -> dict:
    """Return the candidate's parsed JSON document.

    Raises ``ValueError`` if the document is not the expected shape.
    Used by the structural scorer and by future LLM-driven scorers
    that consume the exemplars.
    """
    try:
        doc = json.loads(template_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"router_classifier prompt is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError("router_classifier prompt must be a JSON object")
    if "exemplars" not in doc or not isinstance(doc["exemplars"], list):
        raise ValueError("router_classifier prompt missing 'exemplars' list")
    if "instructions" not in doc or not isinstance(doc["instructions"], str):
        raise ValueError("router_classifier prompt missing 'instructions' string")
    for i, ex in enumerate(doc["exemplars"]):
        if not isinstance(ex, dict):
            raise ValueError(f"exemplar[{i}] must be an object")
        if "query" not in ex or not isinstance(ex["query"], str):
            raise ValueError(f"exemplar[{i}] missing 'query' string")
        if "intent_label" not in ex or not isinstance(ex["intent_label"], str):
            raise ValueError(f"exemplar[{i}] missing 'intent_label' string")
    return doc


def _structural_score(template_text: str) -> float:
    """Score in [0, 1].

    Today this is a structural sanity check. The follow-up
    (operator-driven) replaces it with a real router-eval scorer that
    rebuilds ``SemanticIntentClassifier`` against the candidate's
    exemplar set and runs ``agent.router_eval.evaluate``.
    """
    if len(template_text.encode("utf-8", errors="replace")) > _MAX_TEMPLATE_BYTES:
        return 0.0
    try:
        doc = parse_template(template_text)
    except ValueError:
        return 0.0
    n_exemplars = len(doc["exemplars"])
    if not _MIN_EXEMPLARS <= n_exemplars <= _MAX_EXEMPLARS:
        return 0.0
    instr_len = len(doc["instructions"])
    if instr_len < 20:
        # Instruction text below 20 chars is effectively empty — the
        # k-NN classifier doesn't need it but the LLM fallback does.
        return 0.6
    # Healthy band: 3 to 50 exemplars + non-trivial instructions.
    if 3 <= n_exemplars <= 50 and 20 <= instr_len <= 1000:
        return 0.95
    # Otherwise above-floor but penalised.
    return 0.85


class RouterClassifierAdapter:
    """Adapter for the router-classifier exemplar+instruction prompt."""

    target = TARGET_NAME

    def score(self, prompt_text: str, example: "TraceExample") -> float:
        del example
        return _structural_score(prompt_text)

    def mutate(
        self,
        prompt_text: str,
        examples: "Sequence[TraceExample]",
        seed: int,
    ) -> list[str]:
        """Deterministic mutations.

        Each mutation parses the current document, alters one field,
        re-serialises. Mutations that fail to parse the input are
        silently skipped — they cannot improve on a broken baseline.
        """
        del examples
        del seed
        try:
            doc = parse_template(prompt_text)
        except ValueError:
            return []

        mutations: list[str] = []

        # 1. Tighter instructions (force hard-deferral wording).
        m1 = dict(doc)
        m1["instructions"] = (
            "Classify into a supported intent. If you're not at least "
            "70% confident, return 'unsupported' so a human is asked."
        )
        mutations.append(json.dumps(m1, indent=2, sort_keys=True))

        # 2. Add a synthetic exemplar covering an underrepresented
        # category — when there are fewer than the soft cap.
        if len(doc["exemplars"]) < _MAX_EXEMPLARS:
            m2 = dict(doc)
            m2["exemplars"] = list(doc["exemplars"]) + [
                {"query": "Add this to my todo list.", "intent_label": "task_capture"},
            ]
            mutations.append(json.dumps(m2, indent=2, sort_keys=True))

        # 3. Drop the first exemplar (when there's slack to do so).
        if len(doc["exemplars"]) > _MIN_EXEMPLARS:
            m3 = dict(doc)
            m3["exemplars"] = list(doc["exemplars"])[1:]
            mutations.append(json.dumps(m3, indent=2, sort_keys=True))

        return mutations


def _eval_runner(candidate: "CandidatePrompt") -> float:
    """Eval-gate runner for ``router_classifier``.

    Same structural scorer the adapter uses. The operator-driven
    upgrade replaces this via ``register_runner`` with a real router
    eval against ``tests/router_eval_set.jsonl`` (using the candidate's
    exemplars to instantiate ``SemanticIntentClassifier``).
    """
    return _structural_score(candidate.prompt_text)


# Registrations.
register_adapter(TARGET_NAME, RouterClassifierAdapter)
register_runner(TARGET_NAME, _eval_runner)

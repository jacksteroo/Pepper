"""Wait-action tool — Issue #55.

Allows Pepper to explicitly choose NOT to act, with a logged reason.
A wait is a deliberate non-action, not a failure.

The trace for a wait turn has:
  - output = ""  (empty string — TraceRow.output is non-nullable TEXT)
  - tools_called includes {name: "wait", args: {reason, until}}
  - assembled_context["is_wait"] = True  (the UI marker)

Scheduler integration: the caller (core.py) treats a response that
contains only a wait tool call as "success" — not a failure — when
computing job health metrics.
"""
from __future__ import annotations


async def execute_wait(args: dict) -> dict:
    """Execute the wait action.

    Returns a structured result that core.py and the trace emitter
    recognise as a deliberate non-action.
    """
    reason = args.get("reason")
    if not reason:
        return {"error": "reason is required"}
    until = args.get("until")  # optional ISO timestamp or natural language
    result: dict = {"status": "wait", "reason": reason}
    if until is not None:
        result["until"] = until
    return result


WAIT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "wait",
        "description": (
            "Choose not to surface a response right now. Use when the timing is wrong, "
            "the situation does not warrant action, or Jack will get there himself. "
            "MUST include a reason. This is a first-class deliberate choice — not a "
            "failure or a fallback."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why Pepper chose to wait right now.",
                },
                "until": {
                    "type": "string",
                    "description": (
                        "Optional ISO timestamp or human description of when to revisit "
                        "(e.g. '2026-05-10T09:00:00' or 'after the trip ends')."
                    ),
                },
            },
            "required": ["reason"],
        },
    },
}

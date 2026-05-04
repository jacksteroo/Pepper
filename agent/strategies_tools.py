"""Strategy Hub tools — Epic 06 (#54).

Two LLM-callable tools:
  ``query_strategies``        — retrieve relevant strategies for a situation.
  ``propose_strategy_update`` — enqueue a strategy change for human approval.

``propose_strategy_update`` NEVER writes to the strategies table directly;
it always routes through ``PendingActionsQueue``.
"""
from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


# ── Tool schemas (OpenAI function-calling format) ─────────────────────────────

STRATEGY_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_strategies",
            "description": (
                "Retrieve the most relevant behavioral strategies for a given "
                "situation. Returns a ranked list of strategies that should "
                "guide how Pepper handles the current context. Call this when "
                "you need to recall behavioral guidelines for an unusual or "
                "nuanced situation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "situation": {
                        "type": "string",
                        "description": (
                            "A short description of the current situation or "
                            "question type, e.g. 'email triage' or "
                            "'schedule conflict'."
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of strategies to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["situation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_strategy_update",
            "description": (
                "Propose a new or revised behavioral strategy for Jack's "
                "review. The proposal is queued for human approval and never "
                "written to the database directly. Use this when Jack corrects "
                "Pepper's behavior or when a pattern suggests a guideline "
                "should change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy_id": {
                        "type": "string",
                        "description": (
                            "UUID of an existing strategy to revise. "
                            "Omit or pass null to propose a brand-new strategy."
                        ),
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The full text of the proposed strategy.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why this change is being proposed — e.g. what "
                            "interaction or pattern triggered it."
                        ),
                    },
                },
                "required": ["new_text", "reason"],
            },
        },
    },
]


# ── Execution ─────────────────────────────────────────────────────────────────


async def execute_query_strategies(
    args: dict,
    *,
    db_factory,
    llm_client,
) -> dict:
    """Embed the situation and return top-k matching strategies.

    Returns ``{"strategies": [...], "count": n}``.
    """
    situation = (args.get("situation") or "").strip()
    if not situation:
        return {"error": "query_strategies requires 'situation'"}

    top_k = int(args.get("top_k") or 5)
    top_k = max(1, min(top_k, 20))

    # Embed the situation using the memory subsystem embedder
    # (nomic-embed-text, 768-dim — same model as strategies.embedding).
    try:
        embedding = await llm_client.embed(situation)
    except Exception as exc:
        logger.warning("query_strategies_embed_failed", error=str(exc))
        # Fall back to returning all active strategies when embedding fails.
        embedding = None

    try:
        async with db_factory() as session:
            from agent.strategies.repository import StrategyRepository

            repo = StrategyRepository(session)
            if embedding is not None:
                rows = await repo.query_by_similarity(embedding, top_k=top_k)
            else:
                rows = await repo.query_all_active()
                rows = rows[:top_k]

            # Bump usage count for surfaced strategies (fire-and-forget).
            for row in rows:
                try:
                    await repo.bump_usage(row.strategy_id)
                except Exception:
                    pass  # non-critical

            strategies = [
                {
                    "strategy_id": str(row.strategy_id),
                    "text": row.text,
                    "confidence": round(row.confidence, 3),
                    "usage_count": row.usage_count,
                }
                for row in rows
            ]
            await session.commit()

        logger.info(
            "query_strategies_result",
            situation_preview=situation[:60],
            count=len(strategies),
        )
        return {"strategies": strategies, "count": len(strategies)}

    except Exception as exc:
        logger.warning("query_strategies_failed", error=str(exc))
        return {"error": str(exc), "strategies": [], "count": 0}


async def execute_propose_strategy_update(
    args: dict,
    *,
    pending_actions,
) -> dict:
    """Enqueue a strategy update proposal.

    NEVER writes to the strategies table directly.
    Returns ``{"status": "pending", "action_id": "..."}``.
    """
    new_text = (args.get("new_text") or "").strip()
    reason = (args.get("reason") or "").strip()
    strategy_id = (args.get("strategy_id") or "").strip() or None

    if not new_text:
        return {"error": "propose_strategy_update requires 'new_text'"}
    if not reason:
        return {"error": "propose_strategy_update requires 'reason'"}

    queue_args = {
        "new_text": new_text,
        "reason": reason,
    }
    if strategy_id:
        queue_args["strategy_id"] = strategy_id

    preview = (
        f"Strategy update: {new_text[:100]}{'…' if len(new_text) > 100 else ''}"
    )

    action = pending_actions.queue(
        "apply_strategy_update",
        queue_args,
        preview=preview,
    )

    logger.info(
        "propose_strategy_update_queued",
        action_id=action.id,
        strategy_id=strategy_id,
        new_text_preview=new_text[:60],
    )

    return {
        "status": "pending",
        "action_id": action.id,
        "message": (
            "Strategy update queued for your review. "
            "Approve or reject it from the Pepper status panel."
        ),
    }

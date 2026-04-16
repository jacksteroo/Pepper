from __future__ import annotations

import asyncio
import time
import uuid
import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from agent.config import Settings
from agent.db import init_db, get_db
from agent.core import PepperCore
from agent.life_context import load_life_context
from agent.scheduler import PepperScheduler
from agent.auth import require_api_key

logger = structlog.get_logger()
settings = Settings()

# Global instances (set in lifespan)
pepper: PepperCore = None
scheduler: PepperScheduler = None


def _get_pepper():
    """Return pepper from app.state if pre-initialized, else the module-level global."""
    return getattr(app.state, 'pepper', None) or pepper


def _get_scheduler():
    return getattr(app.state, 'scheduler', None) or scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pepper, scheduler
    await init_db(settings)

    # Build a session factory backed by the initialised engine
    from contextlib import asynccontextmanager as _acm
    from sqlalchemy.ext.asyncio import AsyncSession
    from agent.db import get_engine

    @_acm
    async def _session_factory():
        async with AsyncSession(get_engine()) as session:
            yield session

    pepper = PepperCore(settings, db_session_factory=_session_factory)
    await pepper.initialize()
    scheduler = PepperScheduler(pepper, settings)
    scheduler.start()
    pepper._scheduler = scheduler
    logger.info("pepper_started")
    yield
    logger.info("pepper_stopping")
    if scheduler:
        scheduler.stop()


app = FastAPI(title="Pepper Core", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    session_id: str = None


class ChatResponse(BaseModel):
    response: str
    session_id: str


class LifeContextUpdate(BaseModel):
    section: str
    content: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    started_at = time.perf_counter()
    logger.info(
        "api_chat_in",
        session_id=session_id,
        text=req.message[:300],
        message_chars=len(req.message),
    )
    try:
        response = await _get_pepper().chat(req.message, session_id)
        logger.info(
            "api_chat_out",
            session_id=session_id,
            text=response[:300],
            response_chars=len(response),
            duration_ms=round((time.perf_counter() - started_at) * 1000),
        )
        return ChatResponse(response=response, session_id=session_id)
    except Exception as e:
        logger.error(
            "chat_error",
            session_id=session_id,
            error=str(e),
            duration_ms=round((time.perf_counter() - started_at) * 1000),
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/status", dependencies=[Depends(require_api_key)])
async def status():
    s = await _get_pepper().get_status()
    sched = _get_scheduler()
    if sched:
        s["scheduler"] = sched.get_status()
    return s


@app.get("/life-context", dependencies=[Depends(require_api_key)])
async def get_life_context():
    content = load_life_context(settings.LIFE_CONTEXT_PATH)
    return {"content": content, "path": settings.LIFE_CONTEXT_PATH}


@app.put("/life-context", dependencies=[Depends(require_api_key)])
async def put_life_context(req: LifeContextUpdate):
    async for db in get_db():
        from agent.life_context import update_life_context, build_system_prompt

        await update_life_context(req.section, req.content, db, settings.LIFE_CONTEXT_PATH)
        _get_pepper()._system_prompt = build_system_prompt(settings.LIFE_CONTEXT_PATH, settings)
    return {"ok": True}


@app.get("/conversations", dependencies=[Depends(require_api_key)])
async def get_conversations(limit: int = 50):
    async for db in get_db():
        from sqlalchemy import select, desc
        from agent.models import Conversation

        result = await db.execute(
            select(Conversation).order_by(desc(Conversation.created_at)).limit(limit)
        )
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "session_id": r.session_id,
                "role": r.role,
                "content": r.content,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@app.post("/brief/now", dependencies=[Depends(require_api_key)])
async def trigger_brief():
    sched = _get_scheduler()
    if not sched:
        raise HTTPException(status_code=503, detail="Scheduler not running")
    brief = await sched.generate_morning_brief()
    return {"ok": True, "brief": brief}


@app.post("/review/now", dependencies=[Depends(require_api_key)])
async def trigger_review():
    sched = _get_scheduler()
    if not sched:
        raise HTTPException(status_code=503, detail="Scheduler not running")
    review = await sched.generate_weekly_review()
    return {"ok": True, "review": review}


@app.get("/commitments", dependencies=[Depends(require_api_key)])
async def get_commitments():
    results = await _get_pepper().memory.search_recall(
        "COMMITMENT: OR follow up OR I will", limit=20
    )
    pending = [r for r in results if not r.get("content", "").startswith("[RESOLVED]")]
    return {"commitments": pending}


@app.post("/commitments/{memory_id}/complete", dependencies=[Depends(require_api_key)])
async def complete_commitment(memory_id: int):
    # Mark as resolved in memory by saving a resolved marker
    await _get_pepper().memory.save_to_recall(f"[RESOLVED] commitment id:{memory_id}", importance=0.4)
    return {"ok": True}


@app.get("/skills", dependencies=[Depends(require_api_key)])
async def list_skills():
    """List all loaded skills with their metadata."""
    matcher = _get_pepper()._skill_matcher
    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "triggers": s.triggers,
                "tools": s.tools,
                "model": s.model,
                "version": s.version,
            }
            for s in matcher.skills
        ],
        "count": len(matcher.skills),
    }


@app.get("/skill-improvements", dependencies=[Depends(require_api_key)])
async def get_skill_improvements(status: str = "pending"):
    """Return the skill improvement queue.

    status: 'pending' (default) | 'all'
    """
    reviewer = _get_pepper()._skill_reviewer
    items = reviewer.get_all_improvements() if status == "all" else reviewer.get_pending_improvements()
    return {"improvements": items, "count": len(items)}


class ImprovementAction(BaseModel):
    action: str  # "approve" | "reject"


@app.post("/skill-improvements/{improvement_id}", dependencies=[Depends(require_api_key)])
async def act_on_improvement(improvement_id: str, req: ImprovementAction):
    """Approve or reject a proposed skill improvement.

    Approving writes the improvement note to the skill file and increments
    the version number. The skill is reloaded on the next Pepper restart.
    """
    reviewer = _get_pepper()._skill_reviewer
    if req.action == "approve":
        ok = await reviewer.approve_improvement(improvement_id)
    elif req.action == "reject":
        ok = reviewer.reject_improvement(improvement_id)
    else:
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")
    if not ok:
        raise HTTPException(status_code=404, detail="Improvement not found or already actioned")
    return {"ok": True, "id": improvement_id, "action": req.action}


@app.get("/comms-health", dependencies=[Depends(require_api_key)])
async def get_comms_health(quiet_days: int = 14):
    """Communication health summary: quiet contacts, overdue responses, relationship balance."""
    from agent.comms_health_tools import (
        execute_get_comms_health_summary,
        execute_get_overdue_responses,
        execute_get_relationship_balance_report,
    )
    summary, overdue, balance = await asyncio.gather(
        execute_get_comms_health_summary({"quiet_days": quiet_days}),
        execute_get_overdue_responses({"hours": 48}),
        execute_get_relationship_balance_report({"days": 30}),
        return_exceptions=True,
    )
    return {
        "summary": summary if not isinstance(summary, Exception) else {"error": "Failed to load summary"},
        "overdue_responses": overdue if not isinstance(overdue, Exception) else {"error": "Failed to load overdue responses"},
        "relationship_balance": balance if not isinstance(balance, Exception) else {"error": "Failed to load relationship balance"},
    }

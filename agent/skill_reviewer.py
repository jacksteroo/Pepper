"""Phase 4.3: Background skill reviewer and improvement queue.

After a turn that used one or more skills, the reviewer checks whether the
skill workflow was actually followed. When it finds a gap it enqueues a
proposed improvement in skills/.improvements_queue.json.

Users review and approve/reject improvements via the web UI (GET/POST
/skill-improvements). Approved diffs are written back to the SKILL.md file
and the version number is incremented. Nothing is auto-applied.

Privacy rule: the reviewer ALWAYS uses the local Ollama model. It sees raw
conversation content and must never route to a frontier LLM.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent.skills import _parse_frontmatter

import structlog

logger = structlog.get_logger()

_QUEUE_PATH = Path(__file__).parent.parent / "skills" / ".improvements_queue.json"
_QUEUE_LOCK = threading.Lock()  # serializes concurrent background reviews


# ── Queue helpers ─────────────────────────────────────────────────────────────

def _load_queue() -> list[dict[str, Any]]:
    if not _QUEUE_PATH.exists():
        return []
    try:
        return json.loads(_QUEUE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("skill_queue_read_failed", error=str(e))
        return []


def _save_queue(queue: list[dict[str, Any]]) -> None:
    _QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_PATH.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Reviewer ─────────────────────────────────────────────────────────────────

class SkillReviewer:
    """Reviews post-turn interactions against the skills that were injected.

    Design invariants:
    - Review always uses the local Ollama model (sees raw conversation)
    - Proposed improvements are never auto-applied — human approval required
    - All failures are logged and swallowed — never surface to the user
    """

    def __init__(self, llm_client, skills: list, config) -> None:
        self._llm = llm_client
        self._skills: dict[str, Any] = {s.name: s for s in (skills or [])}
        self._config = config

    async def review_turn(
        self,
        skill_names: list[str],
        user_message: str,
        assistant_response: str,
        tool_calls_made: list[str],
    ) -> None:
        """Non-blocking post-turn review.

        Called via asyncio.create_task — runs after the response is delivered.
        Failures are swallowed so they never surface to the user.
        """
        if not skill_names or not self._skills:
            return

        for name in skill_names:
            skill = self._skills.get(name)
            if skill is None:
                continue
            try:
                await self._review_one(skill, user_message, assistant_response, tool_calls_made)
            except Exception as e:
                logger.warning("skill_review_failed", skill=name, error=str(e))

    async def _review_one(
        self,
        skill: Any,
        user_message: str,
        assistant_response: str,
        tool_calls_made: list[str],
    ) -> None:
        prompt = (
            "You are reviewing whether a workflow was followed correctly in an AI response.\n\n"
            f"Skill: {skill.name}\n"
            f"Workflow:\n{skill.content}\n\n"
            f"User message: {user_message[:300]}\n"
            f"Tools called during turn: {tool_calls_made}\n"
            f"Assistant response (first 400 chars): {assistant_response[:400]}\n\n"
            "Determine if the workflow was followed correctly. "
            "If yes, respond with exactly: LGTM\n"
            "If there is a concrete improvement, respond with:\n"
            "IMPROVEMENT: <one concise paragraph describing what should change>\n\n"
            "Reply with LGTM or IMPROVEMENT: only — nothing else."
        )

        try:
            result = await self._llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=f"local/{self._config.DEFAULT_LOCAL_MODEL}",
                options={"num_predict": 150},
            )
            content = (result.get("content") or "").strip()
        except Exception as e:
            logger.warning("skill_review_llm_failed", skill=skill.name, error=str(e))
            return

        if not content:
            return

        upper = content.upper()
        if upper.startswith("LGTM"):
            logger.debug("skill_review_lgtm", skill=skill.name)
            return

        if upper.startswith("IMPROVEMENT:"):
            improvement = content[len("IMPROVEMENT:"):].strip()
            if improvement:
                self._enqueue(skill.name, improvement, user_message)

    def _enqueue(self, skill_name: str, improvement: str, context: str) -> None:
        with _QUEUE_LOCK:
            queue = _load_queue()
            queue.append({
                "id": f"{skill_name}_{uuid.uuid4().hex[:12]}",
                "skill": skill_name,
                "improvement": improvement,
                "context": context[:200],
                "proposed_at": time.time(),
                "status": "pending",  # pending | approved | rejected
            })
            _save_queue(queue)
        logger.info(
            "skill_improvement_queued",
            skill=skill_name,
            preview=improvement[:100],
        )

    # ── Approval API (called by the web endpoint) ─────────────────────────────

    def get_pending_improvements(self) -> list[dict[str, Any]]:
        return [item for item in _load_queue() if item.get("status") == "pending"]

    def get_all_improvements(self) -> list[dict[str, Any]]:
        return _load_queue()

    async def approve_improvement(self, improvement_id: str) -> bool:
        """Apply an approved improvement to the skill file and increment version.

        Uses the local LLM to rewrite the workflow body incorporating the
        improvement, then writes the updated file back. The model sees the
        updated workflow on the next Pepper restart.

        Falls back to appending an ## Applied Improvement section if the LLM
        returns empty output, so the model still sees the change as plain text
        (not a hidden HTML comment).
        """
        queue = _load_queue()
        item = next((q for q in queue if q.get("id") == improvement_id), None)
        if item is None or item.get("status") != "pending":
            return False

        skill = self._skills.get(item["skill"])
        if skill is None:
            logger.warning("skill_approve_skill_not_found", skill=item["skill"])
            return False

        try:
            raw = skill.path.read_text(encoding="utf-8")
            _, current_body = _parse_frontmatter(raw)

            # Ask the local LLM to apply the improvement to the workflow body.
            # Always local-only: the raw skill content and improvement notes are
            # internal and should never leave the machine.
            prompt = (
                "You are updating a skill workflow document. "
                "Apply the approved improvement to the workflow below. "
                "Keep the same heading structure and numbered steps. "
                "Output ONLY the updated workflow content — no preamble, no explanation.\n\n"
                f"Current workflow:\n{current_body}\n\n"
                f"Improvement to apply:\n{item['improvement']}"
            )
            try:
                result = await self._llm.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model=f"local/{self._config.DEFAULT_LOCAL_MODEL}",
                )
                updated_body = (result.get("content") or "").strip()
            except Exception as llm_err:
                logger.warning("skill_approve_llm_failed", id=improvement_id, error=str(llm_err))
                updated_body = ""

            if not updated_body:
                # LLM returned empty — append as a visible section so the model
                # sees it on the next restart (not a hidden HTML comment).
                updated_body = (
                    current_body.rstrip()
                    + f"\n\n## Applied Improvement\n\n{item['improvement']}\n"
                )

            # Increment version in frontmatter
            raw = re.sub(
                r"^(version:\s*)(\d+)",
                lambda m: f"{m.group(1)}{int(m.group(2)) + 1}",
                raw,
                count=1,
                flags=re.MULTILINE,
            )

            # Replace everything after the closing --- with the updated body
            fm_match = re.match(r"^---\s*\n.*?\n---\s*\n?", raw, re.DOTALL)
            if fm_match:
                raw = fm_match.group(0) + "\n" + updated_body + "\n"
            else:
                raw = raw.rstrip() + "\n\n" + updated_body + "\n"

            skill.path.write_text(raw, encoding="utf-8")
            item["status"] = "approved"
            _save_queue(queue)
            logger.info("skill_improvement_approved", id=improvement_id, skill=item["skill"])
            return True
        except Exception as e:
            logger.warning("skill_improvement_apply_failed", id=improvement_id, error=str(e))
            return False

    def reject_improvement(self, improvement_id: str) -> bool:
        """Mark an improvement rejected (skill file unchanged)."""
        queue = _load_queue()
        item = next((q for q in queue if q.get("id") == improvement_id), None)
        if item is None or item.get("status") != "pending":
            return False
        item["status"] = "rejected"
        _save_queue(queue)
        logger.info("skill_improvement_rejected", id=improvement_id, skill=item.get("skill"))
        return True

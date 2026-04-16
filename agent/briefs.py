"""Commitment extraction for live conversation turns.

BriefFormatter has been removed — morning brief, weekly review, and commitment
check are now generated via pepper.chat() guided by skill files in skills/.
"""

import re
import json
import structlog
from datetime import datetime

logger = structlog.get_logger()


class CommitmentExtractor:
    """Uses local LLM to extract commitments/promises from text."""

    PROMISE_PATTERNS = [
        r"\bI'?ll\b", r"\bI will\b", r"\blet me\b", r"\bI'll follow up\b",
        r"\bI'll send\b", r"\bI'll intro\b", r"\bI'll get back\b",
        r"\bI'll reach out\b", r"\bI'll check\b", r"\bI'll make sure\b",
    ]

    def __init__(self, llm_client=None):
        self._llm = llm_client

    def has_commitment_language(self, text: str) -> bool:
        """Fast pre-check before calling LLM."""
        for pattern in self.PROMISE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    async def extract_from_text(self, text: str) -> list[dict]:
        """Returns list of {text, type, detected_at} dicts."""
        if not self.has_commitment_language(text):
            return []
        if not self._llm:
            return [{"text": text, "type": "promise", "detected_at": datetime.utcnow().isoformat()}]
        try:
            result = await self._llm.chat(
                messages=[{
                    "role": "user",
                    "content": (
                        "Extract any commitments or promises from this text. "
                        "A commitment is when the speaker says they will do something for another person. "
                        "Return a JSON array of objects with keys 'text' (the commitment) and 'type' ('promise', 'follow_up', or 'intro'). "
                        "Return an empty array [] if there are no commitments. "
                        "Respond with ONLY the JSON array, no explanation.\n\n"
                        f"Text: {text}"
                    )
                }],
                model=f"local/{self._llm.config.DEFAULT_LOCAL_MODEL}"
            )
            content = result.get("content", "[]").strip()
            match = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                items = json.loads(match.group())
                now = datetime.utcnow().isoformat()
                return [{"text": item.get("text", ""), "type": item.get("type", "promise"), "detected_at": now}
                        for item in items if item.get("text")]
        except Exception as e:
            logger.warning("commitment_extraction_failed", error=str(e))
        return []

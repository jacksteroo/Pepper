from __future__ import annotations

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Database
    POSTGRES_URL: str = "postgresql+asyncpg://pepper:pepper@localhost:5432/pepper"

    # LLM — Anthropic
    ANTHROPIC_API_KEY: Optional[str] = None

    # Web search
    BRAVE_API_KEY: Optional[str] = None

    # Routing
    GOOGLE_MAPS_API_KEY: Optional[str] = None

    # LLM — Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    DEFAULT_LOCAL_MODEL: str = "hermes-4.3-36b-tools:latest"
    # DEFAULT_FRONTIER_MODEL: intentionally defaults to local model so the system runs
    # fully offline without an ANTHROPIC_API_KEY. Set to "claude-sonnet-4-6" in .env
    # to enable frontier reasoning for high-stakes tasks (family decisions, drafts, etc.)
    # Raw personal data (messages, email bodies) is NEVER sent to frontier regardless.
    DEFAULT_FRONTIER_MODEL: str = "local/hermes-4.3-36b-tools:latest"

    # Telegram Bot
    TELEGRAM_BOT_TOKEN: Optional[str] = None
    TELEGRAM_ALLOWED_USER_IDS: str = ""  # comma-separated string

    # Life context
    LIFE_CONTEXT_PATH: str = "docs/LIFE_CONTEXT.md"
    OWNER_NAME: str = "the owner"

    # Timezone (IANA name, e.g. "America/Los_Angeles", "America/New_York")
    TIMEZONE: str = "America/Los_Angeles"

    # Query depth — set ALWAYS_HEAVY=false in .env to let the deterministic
    # router keep obvious general-chat turns on the fast path. Default is True:
    # every message goes through the full proactive fetch path.
    ALWAYS_HEAVY: bool = True

    # Morning brief schedule (24h, local time)
    MORNING_BRIEF_HOUR: int = 7
    MORNING_BRIEF_MINUTE: int = 0

    # Weekly review
    WEEKLY_REVIEW_DAY: int = 6   # 0=Monday, 6=Sunday
    WEEKLY_REVIEW_HOUR: int = 18

    # API auth
    API_KEY: Optional[str] = None

    # Context compression (Phase 3.2)
    # Effective context window for the local model in tokens. Override via the
    # MODEL_CONTEXT_TOKENS env var in .env. Compression triggers at 80% of
    # this value. hermes3 (LLaMA 3.1) supports up to 128K but Ollama's
    # default num_ctx is 2048–8192; set conservatively.
    MODEL_CONTEXT_TOKENS: int = 16384

    # System
    LOG_LEVEL: str = "INFO"
    LOG_TO_FILE: bool = True
    LOG_FILE_PATH: str = "logs/pepper.log"
    PORT: int = 8000
    MONTHLY_SPEND_LIMIT_USD: float = 50.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def select_model(self, task_type: str, data_sensitivity: str) -> str:
        """Route to local or frontier model per LLM_STRATEGY.md.

        Returns a model identifier:
          - "local/<model>"  → Ollama
          - bare string      → Anthropic (frontier)
        """
        if data_sensitivity == "raw_personal":
            # iMessage, email bodies, health metrics, financial transactions —
            # must never leave the machine
            return f"local/{self.DEFAULT_LOCAL_MODEL}"

        if task_type in ["family_conversation", "difficult_decision", "high_stakes_draft"]:
            # Frontier reasoning; caller must ensure only summaries are sent
            return self.DEFAULT_FRONTIER_MODEL

        if task_type in ["routine_retrieval", "scheduling", "reminders"]:
            return f"local/{self.DEFAULT_LOCAL_MODEL}"

        if task_type == "background_agent":
            # Deep reasoning via frontier model (always local per DEFAULT_FRONTIER_MODEL)
            return self.DEFAULT_FRONTIER_MODEL

        # Default: local
        return f"local/{self.DEFAULT_LOCAL_MODEL}"

    def get_allowed_telegram_user_ids(self) -> list[int]:
        """Parse TELEGRAM_ALLOWED_USER_IDS into a list of ints.

        Returns an empty list when the env var is empty, which means all
        users are allowed (callers must enforce their own policy).
        """
        if not self.TELEGRAM_ALLOWED_USER_IDS.strip():
            return []
        return [
            int(uid.strip())
            for uid in self.TELEGRAM_ALLOWED_USER_IDS.split(",")
            if uid.strip().isdigit()
        ]

    def is_frontier_model(self, model: str) -> bool:
        """Return True when *model* is a frontier (Anthropic) model."""
        return not model.startswith("local/")


# Module-level singleton — import and use directly.
settings = Settings()

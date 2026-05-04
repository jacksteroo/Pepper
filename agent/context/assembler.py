"""ContextAssembler — composes per-turn prompt + history from selectors.

The assembler is the seam between ``core._chat_impl`` and the per-concern
selectors. Its single public entry point is :meth:`assemble`. Behaviour is
byte-identical to the inline prompt-construction code that previously lived
in core (see #32 for the refactor).

Concretely the assembler:
  1. Calls :class:`LifeContextSelector` for the cached system prompt.
  2. Prepends the current-time + (optional) channel header.
  3. Appends each pre-fetched proactive context (memory, web, routing,
     calendar, email, imessage, whatsapp, slack) — in the same order, with
     the same ``\n\n`` separator — that core used to produce inline.
  4. Optionally appends the skills index (:class:`SkillMatchSelector`).
  5. Calls :class:`LastNTurnsSelector` for the working-memory history.

It does NOT do any of the per-turn rule injection (KEY FACT preambles,
GROUNDING RULES, status preambles, etc.) — those remain in core because they
mutate the user message rather than the system prompt and are not "context
selection" by the issue's framing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from agent.context.selectors import (
    CapabilityBlockSelector,
    IdentitySelector,
    LastNTurnsSelector,
    LifeContextSelector,
    RetrievedMemorySelector,
    SkillMatchSelector,
    StrategyBlockSelector,
)
from agent.context.types import AssembledContext, SelectorRecord, Turn
from agent.identity import DEFAULT_IDENTITY_PATH


class ContextAssembler:
    """Compose the per-turn LLM context from injected selectors."""

    def __init__(
        self,
        *,
        life_context_path: str,
        config: Any,
        capability_registry: Any | None,
        memory_manager: Any,
        skills_provider: Any,
        timezone: str,
        identity_path: str = DEFAULT_IDENTITY_PATH,
    ) -> None:
        self._timezone = timezone
        self._life_context = LifeContextSelector(
            life_context_path=life_context_path,
            config=config,
            capability_registry=capability_registry,
        )
        self._capability_block = CapabilityBlockSelector(
            capability_registry=capability_registry,
        )
        # Epic 06 (#52) — identity selector. Loads `data/pepper_identity.md`
        # and renders the two-section block per ADR-0008.
        self._identity = IdentitySelector(identity_path=identity_path)
        # Epic 06 (#54) — strategy block selector. Snapshot of active
        # strategies is injected via `set_active_strategies()` before
        # `assemble()` runs; ranking uses the user input.
        self._strategy_block = StrategyBlockSelector()
        self._retrieved_memory = RetrievedMemorySelector()
        self._skill_match = SkillMatchSelector(skills_provider=skills_provider)
        self._last_n_turns = LastNTurnsSelector(memory_manager=memory_manager)

    # Allow core to invalidate the cached life-context system prompt after
    # a successful update_life_context tool call. Matches the previous
    # ``self._system_prompt = build_system_prompt(...)`` rebuild in core.
    # Also resets the capability-block cache so any registry-status changes
    # surface on the next turn (capability block is rendered into the
    # life-context system prompt, so they share an invalidation cadence).
    def refresh_life_context(self) -> None:
        self._life_context.refresh()
        self._capability_block.refresh()

    def refresh_identity(self) -> None:
        """Drop the cached identity so the next turn re-reads the file.

        Called by the diff-approval flow (`agent.identity_diffs.approve`)
        and by reflector writes to the Questions section.
        """
        self._identity.refresh()

    @property
    def life_context_selector(self) -> LifeContextSelector:
        return self._life_context

    @property
    def identity_selector(self) -> IdentitySelector:
        return self._identity

    @property
    def strategy_block_selector(self) -> StrategyBlockSelector:
        return self._strategy_block

    def assemble(self, turn: Turn) -> AssembledContext:
        records: dict[str, SelectorRecord] = {}

        # 1. Base system prompt from soul + life context + capabilities.
        lc_record = self._life_context.select()
        records[lc_record.name] = lc_record
        base_system_prompt = lc_record.content or ""

        # 1a. Epic 06 (#52) — identity block per ADR-0008. Appended to
        #     the base system prompt before time/channel headers so the
        #     identity sits next to the life-context content the operator
        #     authored. Empty record (missing file or parse error) skips
        #     the append cleanly.
        id_record = self._identity.select()
        records[id_record.name] = id_record
        if id_record.content:
            base_system_prompt = (
                base_system_prompt.rstrip() + "\n\n" + id_record.content
            )

        # 1b. Epic 06 (#54) — strategy block. Top-N strategies relevant
        #     to the current input are appended after the identity. The
        #     active-strategy snapshot is provided by the caller via
        #     `set_active_strategies()` before `assemble()` runs. An
        #     empty snapshot or empty user_message yields no block — no
        #     content, no append.
        strat_record = self._strategy_block.select(
            situation=turn.user_message,
        )
        records[strat_record.name] = strat_record
        if strat_record.content:
            base_system_prompt = (
                base_system_prompt.rstrip() + "\n\n" + strat_record.content
            )

        # 2. Capability block — diagnostic only; already embedded in the
        #    life-context system prompt. We record provenance so #33 can
        #    attach "what sources were available" to traces.
        cap_record = self._capability_block.select()
        records[cap_record.name] = cap_record

        # 3. Time + channel headers (byte-identical to previous inline code).
        if turn.now_override is not None:
            now_local = turn.now_override
        else:
            tz = ZoneInfo(self._timezone)
            now_local = datetime.now(tz)

        time_header = (
            f"[Current time: "
            f"{now_local.strftime('%A, %B %-d, %Y at %-I:%M %p')} "
            f"{now_local.tzname()} ({self._timezone})]\n\n"
        )
        system = time_header + base_system_prompt
        if turn.channel:
            system = f"[Interface: You are responding via {turn.channel}.]\n\n" + system

        # 4. Retrieved memory (already fetched by caller via gather()).
        # ``memory_records`` is optional — when present, the selector emits
        # ``memory_ids`` for #33 provenance. Backward-compat: callers that
        # don't thread structured rows still get a populated context block.
        rm_record = self._retrieved_memory.select(
            turn.memory_context,
            memory_records=turn.memory_records,
        )
        records[rm_record.name] = rm_record
        if rm_record.content:
            system += f"\n\n{rm_record.content}"

        # 5. Other proactive contexts. These are NOT named selectors per the
        #    issue but they DO contribute to the prompt — preserved here as
        #    a flat ordered append to keep byte-identical output. The Turn
        #    dataclass exposes them as plain strings.
        for extra in (
            turn.web_context,
            turn.routing_context,
            turn.calendar_context,
            turn.email_context,
            turn.imessage_context,
            turn.whatsapp_context,
            turn.slack_context,
        ):
            if extra:
                system += f"\n\n{extra}"

        # NOTE: the heavy path's GROUNDING RULES block is injected here via
        # ``turn.extra_system_suffix``. Those rules depend on routing/intent
        # state that the assembler does not see, so core builds the string
        # itself and hands it in. We append it raw (no extra separator):
        # the previous inline code did ``system += "\n\n[GROUNDING RULES…]"``
        # so the leading "\n\n" is part of ``extra_system_suffix``.
        # KEY FACT / status preambles that mutate the *user* message stay in
        # core unchanged — they are not part of the system prompt. See
        # issue #32 for the scope rationale.
        if turn.extra_system_suffix:
            system += turn.extra_system_suffix

        # 6. Skills index — lazy progressive disclosure.
        sk_record = self._skill_match.select(include=turn.include_skills_index)
        records[sk_record.name] = sk_record
        if sk_record.content:
            system = system + "\n\n" + sk_record.content

        # 7. History.
        ln_record = self._last_n_turns.select(
            limit=turn.history_limit,
            isolated=turn.isolated,
        )
        records[ln_record.name] = ln_record
        history: list[dict[str, Any]] = list(ln_record.content or [])

        return AssembledContext(
            system_prompt=system,
            history=history,
            selectors=records,
        )

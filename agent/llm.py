from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

import httpx
import structlog

from agent.error_classifier import (
    ClassifiedLLMError,
    DataSensitivity,
    ErrorCategory,
    classify_error,
    decide_fallback,
)

logger = structlog.get_logger()

# hermes3 sometimes outputs tool calls as raw JSON text rather than using the
# structured tool_calls field in the Ollama API response. These patterns cover
# the two most common formats it uses.
_TEXT_TOOL_CALL_PATTERNS = [
    # {"name": "search_images", "arguments": {...}}
    re.compile(r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*"arguments"\s*:\s*(\{[^{}]*\})[^{}]*\}', re.DOTALL),
    # {"arguments": {...}, "name": "search_images"}
    re.compile(r'\{[^{}]*"arguments"\s*:\s*(\{[^{}]*\})[^{}]*"name"\s*:\s*"(\w+)"[^{}]*\}', re.DOTALL),
    # {"function": "search_memory", "args": {...}}
    re.compile(r'\{[^{}]*"function"\s*:\s*"(\w+)"[^{}]*"args"\s*:\s*(\{[^{}]*\})[^{}]*\}', re.DOTALL),
    # {"args": {...}, "function": "search_memory"}
    re.compile(r'\{[^{}]*"args"\s*:\s*(\{[^{}]*\})[^{}]*"function"\s*:\s*"(\w+)"[^{}]*\}', re.DOTALL),
]


def _extract_text_tool_calls(content: str) -> list[dict]:
    """Parse hermes3's text-formatted tool calls into the standard tool_calls structure."""
    for i, pattern in enumerate(_TEXT_TOOL_CALL_PATTERNS):
        m = pattern.search(content)
        if not m:
            continue
        try:
            # Even-indexed patterns have (name, args) groups; odd have (args, name).
            if i % 2 == 0:
                name, args_str = m.group(1), m.group(2)
            else:
                args_str, name = m.group(1), m.group(2)
            args = json.loads(args_str)
            logger.debug("text_tool_call_detected", name=name, args=args)
            return [{"id": f"call_{name}", "function": {"name": name, "arguments": args}}]
        except (json.JSONDecodeError, IndexError):
            continue
    return []


class ModelClient:
    """Unified LLM abstraction layer.

    All model calls go through this class.  The rest of Pepper never
    talks to Ollama or Anthropic directly.  Swapping models is a config
    change — nothing else needs to change.

    Phase 3.3: errors are classified by :mod:`agent.error_classifier` and
    routed to intelligent fallback paths.  The privacy invariant is enforced
    here: ``local_only=True`` or ``data_sensitivity=local_only`` calls can
    never be routed to a frontier model under any failure mode.
    """

    def __init__(self, config=None) -> None:
        if config is None:
            from agent.config import settings as config  # type: ignore[assignment]
        self.config = config

        # Lazily import / initialise Anthropic client only if a key is present
        self._anthropic = None
        if config.ANTHROPIC_API_KEY:
            from anthropic import AsyncAnthropic

            self._anthropic = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        options: dict | None = None,
        local_only: bool = False,
        data_sensitivity: str = DataSensitivity.SANITIZED,
    ) -> dict:
        """Route to local or frontier model with classified error handling.

        Returns a dict with keys:
          content      – string response text
          tool_calls   – list of tool-call dicts (may be empty)
          model_used   – resolved model string
          latency_ms   – wall-clock milliseconds for the call

        options: passed through to Ollama as the ``options`` field (e.g.
          ``{"num_predict": 5}`` to cap output tokens). Ignored for Anthropic.

        local_only: when True, any non-local model string is overridden to the
          configured DEFAULT_LOCAL_MODEL.  Use this for calls that process raw
          personal data (e.g. context compression) to enforce the privacy
          invariant even if the caller accidentally passes a frontier model.

        data_sensitivity: one of ``local_only``, ``sanitized``, ``public``.
          Used by the error classifier to determine whether a frontier fallback
          is allowed on failure.  Defaults to ``sanitized`` (safe for summaries
          and structured data; raw personal content must use ``local_only``).
        """
        model = model or f"local/{self.config.DEFAULT_LOCAL_MODEL}"

        # Privacy enforcement layer 1: local_only flag overrides any non-local model.
        if local_only and not model.startswith("local/"):
            enforced = f"local/{self.config.DEFAULT_LOCAL_MODEL}"
            logger.warning(
                "local_only_override",
                original_model=model,
                enforced_model=enforced,
            )
            model = enforced
            # Ensure the classifier also treats this call as local_only
            data_sensitivity = DataSensitivity.LOCAL_ONLY

        # Privacy enforcement layer 2: local_only sensitivity locks to local models.
        if data_sensitivity == DataSensitivity.LOCAL_ONLY and not model.startswith("local/"):
            enforced = f"local/{self.config.DEFAULT_LOCAL_MODEL}"
            logger.warning(
                "sensitivity_local_only_override",
                original_model=model,
                enforced_model=enforced,
            )
            model = enforced

        # Debug: log what we're sending
        user_msgs = [m for m in messages if m.get("role") == "user"]
        last_user = user_msgs[-1]["content"][:200] if user_msgs else ""
        logger.debug(
            "llm_request",
            model=model,
            n_messages=len(messages),
            last_user=last_user,
            data_sensitivity=data_sensitivity,
        )

        current_model = model
        max_attempts = 3

        for attempt in range(max_attempts):
            try:
                start = time.time()

                if current_model.startswith("local/"):
                    local_model_name = current_model.removeprefix("local/")
                    result = await self._ollama_chat(local_model_name, messages, tools, options=options)
                else:
                    result = await self._anthropic_chat(current_model, messages, tools)

                latency_ms = (time.time() - start) * 1000
                logger.info(
                    "llm_call",
                    model=current_model,
                    latency_ms=round(latency_ms),
                    n_tools=len(result.get("tool_calls") or []),
                    input_tokens=result.get("input_tokens"),
                    output_tokens=result.get("output_tokens"),
                    attempt=attempt,
                )
                logger.debug("llm_response", model=current_model, content=result.get("content", "")[:300])
                result["model_used"] = current_model
                result["latency_ms"] = latency_ms
                return result

            except ClassifiedLLMError:
                # Already classified — re-raise without wrapping
                raise

            except Exception as exc:
                category = classify_error(exc)
                decision = decide_fallback(category, data_sensitivity, current_model, self.config)

                logger.warning(
                    "llm_error_classified",
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    category=category,
                    model=current_model,
                    should_retry=decision.should_retry,
                    error=str(exc)[:300],
                )

                is_last_attempt = attempt == max_attempts - 1

                if not decision.should_retry or is_last_attempt:
                    raise ClassifiedLLMError(category, decision.user_message) from exc

                # Privacy guard on model switch: local_only calls cannot upgrade to frontier
                new_model = decision.model
                if data_sensitivity == DataSensitivity.LOCAL_ONLY and not new_model.startswith("local/"):
                    logger.error(
                        "fallback_blocked_privacy",
                        category=category,
                        original_model=current_model,
                        proposed_model=new_model,
                        sensitivity=data_sensitivity,
                    )
                    raise ClassifiedLLMError(category, decision.user_message) from exc

                if new_model != current_model:
                    logger.info(
                        "llm_model_fallback",
                        from_model=current_model,
                        to_model=new_model,
                        reason=category,
                    )
                    current_model = new_model

                if decision.backoff_seconds > 0:
                    await asyncio.sleep(decision.backoff_seconds)

        # Should not reach here — loop always raises or returns
        raise ClassifiedLLMError(ErrorCategory.UNKNOWN, "Max LLM retry attempts exceeded.")

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding vector.  Always local — never calls external API."""
        return await self._local_embed(text)

    # ------------------------------------------------------------------
    # Private — Ollama
    # ------------------------------------------------------------------

    async def _ollama_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
    ) -> dict:
        payload: dict = {"model": model, "messages": messages, "stream": False}
        if tools:
            # Strip internal metadata (side_effects) before sending to Ollama
            payload["tools"] = [
                {k: v for k, v in t.items() if k != "side_effects"} for t in tools
            ]
        if options:
            payload["options"] = options

        logger.info(
            "ollama_chat_request",
            model=model,
            n_messages=len(messages),
            tool_names=[t.get("function", {}).get("name") for t in tools or []],
            options=options or {},
        )

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.config.OLLAMA_BASE_URL}/api/chat",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        message = data.get("message", {})
        content = message.get("content", "")
        tool_calls = message.get("tool_calls", [])

        # hermes3 sometimes emits tool calls as raw JSON text content instead of
        # using the structured tool_calls field. Detect and normalise.
        if not tool_calls and content:
            tool_calls = _extract_text_tool_calls(content)
            if tool_calls:
                content = ""

        logger.info(
            "ollama_chat_response",
            model=model,
            content_chars=len(content),
            tool_call_count=len(tool_calls),
            tool_names=[c.get("function", {}).get("name") for c in tool_calls],
        )

        return {"content": content, "tool_calls": tool_calls}

    # ------------------------------------------------------------------
    # Private — Anthropic
    # ------------------------------------------------------------------

    def _convert_messages_for_anthropic(self, messages: list[dict]) -> list[dict]:
        """Convert OpenAI-style tool messages to Anthropic format.

        OpenAI:  assistant message has tool_calls list; results are role="tool"
        Anthropic: assistant message has content=[{type:tool_use,...}];
                   results are role="user" with content=[{type:tool_result,...}]
        """
        converted = []
        i = 0
        while i < len(messages):
            m = messages[i]
            role = m.get("role")

            if role == "assistant" and m.get("tool_calls"):
                content: list[dict] = []
                if m.get("content"):
                    content.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn["name"],
                        "input": args,
                    })
                converted.append({"role": "assistant", "content": content})

            elif role == "tool":
                # Collect consecutive tool results into one user message
                tool_results: list[dict] = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    tm = messages[i]
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tm["tool_call_id"],
                        "content": tm["content"],
                    })
                    i += 1
                converted.append({"role": "user", "content": tool_results})
                continue

            else:
                converted.append(m)

            i += 1
        return converted

    async def _anthropic_chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict:
        if not self._anthropic:
            raise ValueError(
                "Anthropic API key not configured — set ANTHROPIC_API_KEY in .env"
            )

        # Extract system prompt; Anthropic uses a separate 'system' parameter
        system: Optional[str] = next(
            (m["content"] for m in messages if m["role"] == "system"), None
        )
        user_messages = self._convert_messages_for_anthropic(
            [m for m in messages if m["role"] != "system"]
        )

        kwargs: dict = {
            "model": model,
            "max_tokens": 4096,
            "messages": user_messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            # Convert OpenAI function-calling format → Anthropic tool format
            kwargs["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {}),
                }
                for t in tools
                if t.get("type") == "function"
            ]

        logger.info(
            "anthropic_chat_request",
            model=model,
            n_messages=len(user_messages),
            has_system=bool(system),
            tool_names=[t.get("function", {}).get("name") for t in tools or []],
        )

        response = await self._anthropic.messages.create(**kwargs)

        content = ""
        tool_calls: list[dict] = []
        for block in response.content:
            if block.type == "text":
                content = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "function": {
                            "name": block.name,
                            "arguments": block.input,
                        },
                    }
                )

        logger.info(
            "anthropic_chat_response",
            model=model,
            content_chars=len(content),
            tool_call_count=len(tool_calls),
            tool_names=[c.get("function", {}).get("name") for c in tool_calls],
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        return {
            "content": content,
            "tool_calls": tool_calls,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    # ------------------------------------------------------------------
    # Private — local embeddings
    # ------------------------------------------------------------------

    async def _local_embed(self, text: str) -> list[float]:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{self.config.OLLAMA_BASE_URL}/api/embeddings",
                        json={"model": "nomic-embed-text", "prompt": text},
                    )
                    resp.raise_for_status()
                    embedding = resp.json().get("embedding", [])
                    if not embedding:
                        raise ValueError(
                            "Ollama returned empty embedding — is nomic-embed-text pulled?"
                        )
                    return embedding
            except Exception as exc:
                if attempt == 2:
                    raise
                category = classify_error(exc)
                if category == ErrorCategory.AUTH:
                    raise
                await asyncio.sleep(2 ** attempt)
        raise RuntimeError("Embedding failed after 3 attempts")  # unreachable

"""
Tests for Phase 3.3 — Error Classifier + Smart Fallback.

Covers:
- classify_error maps httpx status codes to correct categories
- classify_error uses string heuristics when SDK types are unavailable
- decide_fallback returns correct model + retry policy per failure type
- Privacy invariant: local_only calls never route to frontier under any failure
- ModelClient.chat() retries on network/rate-limit, fails fast on auth
- ModelClient.chat() enforces local_only override at two layers
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent.error_classifier import (
    ClassifiedLLMError,
    DataSensitivity,
    ErrorCategory,
    FallbackDecision,
    classify_error,
    decide_fallback,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_mock_config(local_model: str = "hermes-4.3-36b-tools:latest", frontier_model: str = "claude-sonnet-4-6"):
    cfg = MagicMock()
    cfg.DEFAULT_LOCAL_MODEL = local_model
    cfg.DEFAULT_FRONTIER_MODEL = frontier_model
    cfg.OLLAMA_BASE_URL = "http://localhost:11434"
    cfg.ANTHROPIC_API_KEY = "sk-test"
    return cfg


def make_httpx_status_error(status_code: int) -> Exception:
    """Build a minimal httpx.HTTPStatusError for testing."""
    import httpx
    request = httpx.Request("POST", "http://localhost:11434/api/chat")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=response,
    )


def make_httpx_connect_error() -> Exception:
    import httpx
    return httpx.ConnectError("Connection refused")


def make_httpx_timeout() -> Exception:
    import httpx
    return httpx.TimeoutException("Request timed out")


# ── classify_error — httpx status codes ──────────────────────────────────────

class TestClassifyErrorHttpx:
    def test_429_is_rate_limit(self):
        assert classify_error(make_httpx_status_error(429)) == ErrorCategory.RATE_LIMIT

    def test_401_is_auth(self):
        assert classify_error(make_httpx_status_error(401)) == ErrorCategory.AUTH

    def test_403_is_auth(self):
        assert classify_error(make_httpx_status_error(403)) == ErrorCategory.AUTH

    def test_503_is_model_unavailable(self):
        assert classify_error(make_httpx_status_error(503)) == ErrorCategory.MODEL_UNAVAILABLE

    def test_502_is_model_unavailable(self):
        assert classify_error(make_httpx_status_error(502)) == ErrorCategory.MODEL_UNAVAILABLE

    def test_504_is_model_unavailable(self):
        assert classify_error(make_httpx_status_error(504)) == ErrorCategory.MODEL_UNAVAILABLE

    def test_connect_error_is_network(self):
        assert classify_error(make_httpx_connect_error()) == ErrorCategory.NETWORK

    def test_timeout_is_network(self):
        assert classify_error(make_httpx_timeout()) == ErrorCategory.NETWORK

    def test_500_is_model_unavailable(self):
        assert classify_error(make_httpx_status_error(500)) == ErrorCategory.MODEL_UNAVAILABLE


# ── classify_error — string heuristics ───────────────────────────────────────

class TestClassifyErrorHeuristics:
    def test_context_overflow_hint(self):
        exc = ValueError("context length exceeded 8192 tokens")
        assert classify_error(exc) == ErrorCategory.CONTEXT_OVERFLOW

    def test_context_window_hint(self):
        exc = RuntimeError("prompt is too long for context window")
        assert classify_error(exc) == ErrorCategory.CONTEXT_OVERFLOW

    def test_max_tokens_hint(self):
        exc = Exception("max_tokens exceeded")
        assert classify_error(exc) == ErrorCategory.CONTEXT_OVERFLOW

    def test_connection_hint(self):
        exc = OSError("connection refused")
        assert classify_error(exc) == ErrorCategory.NETWORK

    def test_rate_limit_hint(self):
        exc = Exception("rate limit exceeded, please slow down")
        assert classify_error(exc) == ErrorCategory.RATE_LIMIT

    def test_auth_hint(self):
        exc = Exception("unauthorized: invalid api key")
        assert classify_error(exc) == ErrorCategory.AUTH

    def test_model_unavailable_hint(self):
        exc = Exception("model not found: hermes-4.3-36b-tools:latest")
        assert classify_error(exc) == ErrorCategory.MODEL_UNAVAILABLE

    def test_internal_server_error_hint(self):
        exc = RuntimeError("500 Internal Server Error from Ollama")
        assert classify_error(exc) == ErrorCategory.MODEL_UNAVAILABLE

    def test_generic_exception_is_unknown(self):
        exc = RuntimeError("something completely unrelated went wrong")
        assert classify_error(exc) == ErrorCategory.UNKNOWN


# ── decide_fallback — basic behaviour ────────────────────────────────────────

class TestDecideFallbackBasic:
    def setup_method(self):
        self.cfg = make_mock_config()

    def test_auth_never_retries(self):
        d = decide_fallback(ErrorCategory.AUTH, DataSensitivity.SANITIZED, "local/hermes-4.3-36b-tools:latest", self.cfg)
        assert d.should_retry is False
        assert "authentication" in d.user_message.lower() or "auth" in d.user_message.lower()

    def test_auth_never_retries_frontier(self):
        d = decide_fallback(ErrorCategory.AUTH, DataSensitivity.SANITIZED, "claude-sonnet-4-6", self.cfg)
        assert d.should_retry is False

    def test_context_overflow_never_retries_in_llm(self):
        # core.py handles this by compressing and re-calling; llm.py should not retry
        d = decide_fallback(ErrorCategory.CONTEXT_OVERFLOW, DataSensitivity.SANITIZED, "local/hermes-4.3-36b-tools:latest", self.cfg)
        assert d.should_retry is False

    def test_network_retries_same_model(self):
        d = decide_fallback(ErrorCategory.NETWORK, DataSensitivity.SANITIZED, "local/hermes-4.3-36b-tools:latest", self.cfg)
        assert d.should_retry is True
        assert d.model == "local/hermes-4.3-36b-tools:latest"
        assert d.backoff_seconds > 0

    def test_unknown_does_not_retry(self):
        d = decide_fallback(ErrorCategory.UNKNOWN, DataSensitivity.SANITIZED, "local/hermes-4.3-36b-tools:latest", self.cfg)
        assert d.should_retry is False

    def test_model_unavailable_local_does_not_retry(self):
        # Ollama runtime failures should retry locally, never cross-provider
        d = decide_fallback(ErrorCategory.MODEL_UNAVAILABLE, DataSensitivity.SANITIZED, "local/hermes-4.3-36b-tools:latest", self.cfg)
        assert d.should_retry is True
        assert d.model == "local/hermes-4.3-36b-tools:latest"
        assert "ollama" in d.user_message.lower() or "local" in d.user_message.lower()

    def test_rate_limit_local_retries_same(self):
        d = decide_fallback(ErrorCategory.RATE_LIMIT, DataSensitivity.SANITIZED, "local/hermes-4.3-36b-tools:latest", self.cfg)
        assert d.should_retry is True
        assert d.model == "local/hermes-4.3-36b-tools:latest"

    def test_rate_limit_frontier_sanitized_falls_back_to_local(self):
        d = decide_fallback(ErrorCategory.RATE_LIMIT, DataSensitivity.SANITIZED, "claude-sonnet-4-6", self.cfg)
        assert d.should_retry is True
        assert d.model.startswith("local/")

    def test_model_unavailable_frontier_sanitized_falls_back_to_local(self):
        d = decide_fallback(ErrorCategory.MODEL_UNAVAILABLE, DataSensitivity.PUBLIC, "claude-sonnet-4-6", self.cfg)
        assert d.should_retry is True
        assert d.model.startswith("local/")


# ── Privacy invariant tests ───────────────────────────────────────────────────

class TestPrivacyInvariant:
    """
    CRITICAL: These tests are regression guards for the privacy invariant.
    A local_only call must NEVER be routed to a frontier model under ANY failure mode.
    """

    def setup_method(self):
        self.cfg = make_mock_config()

    def test_local_only_rate_limit_does_not_fallback_to_frontier(self):
        """Rate limit on a local_only call should retry locally, not fall back to Claude."""
        d = decide_fallback(
            ErrorCategory.RATE_LIMIT,
            DataSensitivity.LOCAL_ONLY,
            "local/hermes-4.3-36b-tools:latest",
            self.cfg,
        )
        assert d.model.startswith("local/"), (
            f"PRIVACY VIOLATION: local_only rate-limit routed to frontier model '{d.model}'"
        )

    def test_local_only_model_unavailable_does_not_fallback_to_frontier(self):
        """Ollama unavailable with local_only data: surface error, never route to Claude."""
        d = decide_fallback(
            ErrorCategory.MODEL_UNAVAILABLE,
            DataSensitivity.LOCAL_ONLY,
            "local/hermes-4.3-36b-tools:latest",
            self.cfg,
        )
        # Must not retry with a frontier model
        assert d.model.startswith("local/"), (
            f"PRIVACY VIOLATION: local_only model_unavailable proposed frontier '{d.model}'"
        )

    def test_local_only_network_does_not_fallback_to_frontier(self):
        d = decide_fallback(
            ErrorCategory.NETWORK,
            DataSensitivity.LOCAL_ONLY,
            "local/hermes-4.3-36b-tools:latest",
            self.cfg,
        )
        assert d.model.startswith("local/"), (
            f"PRIVACY VIOLATION: local_only network error proposed frontier '{d.model}'"
        )

    def test_local_only_on_frontier_rate_limit_retries_frontier_not_silently_switched(self):
        """
        Pathological case: a local_only call somehow ended up on a frontier model
        (should never happen in practice — two upstream guards prevent it).
        The fallback router must not silently switch it to a local model either;
        it should retry the frontier model with backoff rather than expose the
        data to a local model that may log differently.

        This test documents the intended behaviour: retry frontier with backoff
        (the upstream guards are the real protection; this is the last-resort path).
        """
        d = decide_fallback(
            ErrorCategory.RATE_LIMIT,
            DataSensitivity.LOCAL_ONLY,
            "claude-sonnet-4-6",   # incorrectly on frontier
            self.cfg,
        )
        # Should retry but NOT cross to a different privacy domain
        assert d.should_retry is True
        assert d.model == "claude-sonnet-4-6"  # stays on frontier, doesn't silently change

    def test_all_categories_with_local_only_never_produce_frontier_model(self):
        """Exhaustive check: no error category produces a frontier model for local_only data."""
        for cat in ErrorCategory:
            d = decide_fallback(cat, DataSensitivity.LOCAL_ONLY, "local/hermes-4.3-36b-tools:latest", self.cfg)
            assert d.model.startswith("local/"), (
                f"PRIVACY VIOLATION: category={cat} with LOCAL_ONLY data proposed "
                f"non-local model '{d.model}'"
            )


# ── ModelClient.chat — classified retry integration ──────────────────────────

class TestModelClientClassifiedRetry:
    """Test that ModelClient.chat() uses the error classifier correctly."""

    def make_client(self):
        from agent.llm import ModelClient
        cfg = make_mock_config()
        cfg.ANTHROPIC_API_KEY = None  # no frontier
        client = ModelClient.__new__(ModelClient)
        client.config = cfg
        client._anthropic = None
        return client

    @pytest.mark.asyncio
    async def test_auth_error_raises_immediately_no_retry(self):
        client = self.make_client()
        call_count = 0

        async def fail_auth(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise make_httpx_status_error(401)

        client._ollama_chat = fail_auth
        with pytest.raises(ClassifiedLLMError) as exc_info:
            await client.chat([{"role": "user", "content": "hello"}])

        assert exc_info.value.category == ErrorCategory.AUTH
        assert call_count == 1  # no retries

    @pytest.mark.asyncio
    async def test_network_error_retries(self):
        client = self.make_client()
        call_count = 0

        async def fail_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise make_httpx_connect_error()
            return {"content": "ok", "tool_calls": []}

        client._ollama_chat = fail_then_succeed
        # Patch asyncio.sleep to avoid delays in tests
        with patch("agent.llm.asyncio.sleep", new_callable=AsyncMock):
            result = await client.chat([{"role": "user", "content": "hello"}])

        assert result["content"] == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_network_error_exhausted_raises_classified(self):
        client = self.make_client()

        async def always_fail(*args, **kwargs):
            raise make_httpx_connect_error()

        client._ollama_chat = always_fail
        with patch("agent.llm.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(ClassifiedLLMError) as exc_info:
                await client.chat([{"role": "user", "content": "hello"}])

        assert exc_info.value.category == ErrorCategory.NETWORK

    @pytest.mark.asyncio
    async def test_context_overflow_raises_without_retry(self):
        client = self.make_client()

        async def overflow(*args, **kwargs):
            raise ValueError("context length exceeded 8192 tokens")

        client._ollama_chat = overflow
        with pytest.raises(ClassifiedLLMError) as exc_info:
            await client.chat([{"role": "user", "content": "hello"}])

        assert exc_info.value.category == ErrorCategory.CONTEXT_OVERFLOW

    @pytest.mark.asyncio
    async def test_local_model_unavailable_retries_then_succeeds(self):
        client = self.make_client()
        call_count = 0

        async def fail_500_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise make_httpx_status_error(500)
            return {"content": "ok", "tool_calls": []}

        client._ollama_chat = fail_500_then_succeed
        with patch("agent.llm.asyncio.sleep", new_callable=AsyncMock):
            result = await client.chat([{"role": "user", "content": "hello"}])

        assert result["content"] == "ok"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_local_only_flag_overrides_frontier_model(self):
        """local_only=True must enforce local model even if caller passes frontier string."""
        client = self.make_client()
        used_models = []

        async def capture_model(model, *args, **kwargs):
            used_models.append(model)
            return {"content": "ok", "tool_calls": []}

        client._ollama_chat = capture_model

        await client.chat(
            [{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-6",
            local_only=True,
        )

        assert len(used_models) == 1
        assert used_models[0] == "hermes-4.3-36b-tools:latest"  # local_only stripped the prefix

    @pytest.mark.asyncio
    async def test_local_only_sensitivity_overrides_frontier_model(self):
        """data_sensitivity=local_only must enforce local model regardless of model param."""
        client = self.make_client()
        used_models = []

        async def capture_model(model, *args, **kwargs):
            used_models.append(model)
            return {"content": "ok", "tool_calls": []}

        client._ollama_chat = capture_model

        await client.chat(
            [{"role": "user", "content": "hello"}],
            model="claude-sonnet-4-6",
            data_sensitivity=DataSensitivity.LOCAL_ONLY,
        )

        assert len(used_models) == 1
        assert used_models[0] == "hermes-4.3-36b-tools:latest"

    @pytest.mark.asyncio
    async def test_local_only_fallback_blocked_at_model_switch(self):
        """
        If decide_fallback somehow returns a frontier model for a local_only call,
        ModelClient must block the switch and raise ClassifiedLLMError.
        """
        client = self.make_client()
        call_count = 0

        async def fail_network(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise make_httpx_connect_error()

        client._ollama_chat = fail_network

        # Patch decide_fallback to return a (malicious) frontier model for a local_only call
        bad_decision = FallbackDecision(
            model="claude-sonnet-4-6",  # frontier — should be blocked
            should_retry=True,
            backoff_seconds=0.0,
            user_message="test",
        )
        with patch("agent.llm.decide_fallback", return_value=bad_decision):
            with patch("agent.llm.asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(ClassifiedLLMError):
                    await client.chat(
                        [{"role": "user", "content": "hello"}],
                        model="local/hermes-4.3-36b-tools:latest",
                        data_sensitivity=DataSensitivity.LOCAL_ONLY,
                    )

        # Must not have tried the frontier model at all
        assert call_count == 1  # only the first local call was attempted


# ── ClassifiedLLMError ───────────────────────────────────────────────────────

class TestClassifiedLLMError:
    def test_attributes(self):
        err = ClassifiedLLMError(ErrorCategory.AUTH, "Check your API key.")
        assert err.category == ErrorCategory.AUTH
        assert err.user_message == "Check your API key."
        assert str(err) == "Check your API key."

    def test_is_exception(self):
        err = ClassifiedLLMError(ErrorCategory.NETWORK, "Network error.")
        assert isinstance(err, Exception)

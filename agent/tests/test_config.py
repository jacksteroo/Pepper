from agent.config import Settings


def test_model_context_tokens_reads_from_env(monkeypatch):
    monkeypatch.setenv("MODEL_CONTEXT_TOKENS", "32768")

    config = Settings(_env_file=None)

    assert config.MODEL_CONTEXT_TOKENS == 32768


def test_model_context_tokens_uses_default_when_env_missing(monkeypatch):
    monkeypatch.delenv("MODEL_CONTEXT_TOKENS", raising=False)

    config = Settings(_env_file=None)

    assert config.MODEL_CONTEXT_TOKENS == 16384

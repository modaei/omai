import pytest

from omai.config.settings import Settings


def test_daily_user_limit_is_disabled_when_env_var_is_missing(monkeypatch):
    monkeypatch.delenv("OMAI_DAILY_USER_LIMIT_ENABLED", raising=False)
    monkeypatch.delenv("OMAI_DAILY_USER_LIMIT_REQUESTS", raising=False)

    settings = Settings.from_env()

    assert settings.omai_daily_user_limit_enabled is False
    assert settings.omai_daily_user_limit_requests == 150


def test_daily_user_limit_can_be_enabled_from_env(monkeypatch):
    monkeypatch.setenv("OMAI_DAILY_USER_LIMIT_ENABLED", "true")

    settings = Settings.from_env()

    assert settings.omai_daily_user_limit_enabled is True


def test_log_level_is_normalized_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")

    settings = Settings.from_env()

    assert settings.log_level == "DEBUG"


def test_invalid_log_level_is_rejected(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "verbose")

    settings = Settings.from_env()

    with pytest.raises(ValueError, match="LOG_LEVEL must be one of"):
        settings.validate()


def test_local_llm_is_disabled_when_env_vars_are_missing(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)

    settings = Settings.from_env()

    assert settings.local_llm_enabled is False
    assert settings.local_llm_api_key == "ollama"


def test_local_llm_can_be_enabled_from_env(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("LOCAL_LLM_TIMEOUT_SECONDS", "7")

    settings = Settings.from_env()

    assert settings.local_llm_enabled is True
    assert settings.local_llm_api_key == "ollama"
    assert settings.local_llm_timeout_seconds == 7

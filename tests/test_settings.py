import pytest
from datetime import date

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


def test_demo_mode_requires_fixed_scope_and_date(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    settings = Settings.from_env()

    with pytest.raises(ValueError, match="DEMO_SITE_ID"):
        settings.validate()


def test_demo_mode_accepts_fixed_scope(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_SITE_ID", "4")
    monkeypatch.setenv("DEMO_USER_ID", "1")
    monkeypatch.setenv("DEMO_CURRENT_DATE", "2026-07-31")
    settings = Settings.from_env()

    assert settings.demo_current_date == date(2026, 7, 31)


def test_cors_origins_are_loaded_from_env(monkeypatch):
    monkeypatch.setenv(
        "OMAI_CORS_ALLOWED_ORIGINS",
        "https://demo.example.com, https://preview.example.com",
    )

    settings = Settings.from_env()

    assert settings.omai_cors_allowed_origins == (
        "https://demo.example.com",
        "https://preview.example.com",
    )

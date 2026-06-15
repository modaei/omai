from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str
    omreports_api_url: str
    omreports_timeout_seconds: float
    max_report_days: int
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    db_pool_size: int
    db_max_overflow: int
    db_pool_timeout: int
    db_pool_recycle: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_model=os.getenv(
                "OPENROUTER_MODEL", "openai/gpt-5-mini"
            ).strip(),
            openrouter_base_url=os.getenv(
                "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
            ).rstrip("/"),
            omreports_api_url=os.getenv(
                "OMREPORTS_API_URL", "http://127.0.0.1:50008/report/"
            ).strip(),
            omreports_timeout_seconds=float(
                os.getenv("OMREPORTS_TIMEOUT_SECONDS", "30")
            ),
            max_report_days=int(os.getenv("MAX_REPORT_DAYS", "366")),
            db_host=os.getenv("DB_HOST", "127.0.0.1").strip(),
            db_port=int(os.getenv("DB_PORT", "3306")),
            db_user=os.getenv("DB_USER", "").strip(),
            db_password=os.getenv("DB_PASSWORD", ""),
            db_name=os.getenv("DB_NAME", "").strip(),
            db_pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            db_max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "5")),
            db_pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "15")),
            db_pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def validate(self) -> None:
        if not self.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is not configured.")
        if self.omreports_timeout_seconds <= 0:
            raise ValueError("OMREPORTS_TIMEOUT_SECONDS must be positive.")
        if self.max_report_days <= 0:
            raise ValueError("MAX_REPORT_DAYS must be positive.")

    def validate_database(self) -> None:
        missing = []
        if not self.db_host:
            missing.append("DB_HOST")
        if not self.db_user:
            missing.append("DB_USER")
        if not self.db_name:
            missing.append("DB_NAME")
        if missing:
            raise ValueError(
                "Database settings are incomplete: " + ", ".join(missing)
            )
        if self.db_port <= 0:
            raise ValueError("DB_PORT must be positive.")

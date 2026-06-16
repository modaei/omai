from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    llm_api_key: str
    llm_model: str
    llm_base_url: str
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
    omai_max_concurrent: int
    omai_slot_timeout: float
    omai_conversation_history_limit: int
    omai_conversation_ttl_hours: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            llm_api_key=os.getenv(
                "LLM_API_KEY", os.getenv("OPENROUTER_API_KEY", "")
            ).strip(),
            llm_model=os.getenv(
                "LLM_MODEL", os.getenv("OPENROUTER_MODEL", "gpt-5-mini")
            ).strip(),
            llm_base_url=os.getenv(
                "LLM_BASE_URL",
                os.getenv("OPENROUTER_BASE_URL", "https://api.openai.com/v1"),
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
            omai_max_concurrent=int(os.getenv("OMAI_MAX_CONCURRENT", "10")),
            omai_slot_timeout=float(os.getenv("OMAI_SLOT_TIMEOUT", "15")),
            omai_conversation_history_limit=int(
                os.getenv("OMAI_CONVERSATION_HISTORY_LIMIT", "20")
            ),
            omai_conversation_ttl_hours=int(
                os.getenv("OMAI_CONVERSATION_TTL_HOURS", "168")
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    def validate(self) -> None:
        if not self.llm_api_key:
            raise ValueError("LLM_API_KEY is not configured.")
        if self.omreports_timeout_seconds <= 0:
            raise ValueError("OMREPORTS_TIMEOUT_SECONDS must be positive.")
        if self.max_report_days <= 0:
            raise ValueError("MAX_REPORT_DAYS must be positive.")
        if self.omai_max_concurrent <= 0:
            raise ValueError("OMAI_MAX_CONCURRENT must be positive.")
        if self.omai_slot_timeout <= 0:
            raise ValueError("OMAI_SLOT_TIMEOUT must be positive.")
        if self.omai_conversation_history_limit <= 0:
            raise ValueError("OMAI_CONVERSATION_HISTORY_LIMIT must be positive.")
        if self.omai_conversation_ttl_hours <= 0:
            raise ValueError("OMAI_CONVERSATION_TTL_HOURS must be positive.")

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

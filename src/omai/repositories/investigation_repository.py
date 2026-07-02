from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from omai.config.settings import Settings


class InvestigationRepositoryError(RuntimeError):
    """Raised when an investigation job cannot be persisted or loaded."""


class InvestigationNotFoundError(InvestigationRepositoryError):
    """Raised when a job does not belong to the requested user and site."""


@dataclass(frozen=True)
class Investigation:
    id: int
    uuid: str
    conversation_id: int
    conversation_uuid: str
    user_id: int
    site_id: int
    user_message_id: int
    question: str
    response_mode: str
    status: str
    stage: str
    progress_percent: int
    updated_at: datetime
    assistant_message_id: int | None = None
    answer: str | None = None
    error: str | None = None


class InvestigationRepository:
    """Own durable investigation jobs and enforce user/site access on every read."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @classmethod
    def from_settings(cls, settings: Settings) -> "InvestigationRepository":
        settings.validate_database()
        url = URL.create(
            "mysql+pymysql",
            username=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
        return cls(
            create_engine(
                url,
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
                pool_timeout=settings.db_pool_timeout,
                pool_recycle=settings.db_pool_recycle,
                pool_pre_ping=True,
            )
        )

    def create(
        self,
        *,
        conversation_id: int,
        user_id: int,
        site_id: int,
        user_message_id: int,
        question: str,
        response_mode: str,
        investigation_id: str | None = None,
    ) -> Investigation:
        investigation_uuid = (
            str(UUID(investigation_id)) if investigation_id else str(uuid4())
        )
        now = _utcnow()
        try:
            with self.engine.begin() as connection:
                result = connection.execute(
                    text(
                        """
                        INSERT INTO ai_investigations (
                            uuid, ai_conversation_id, user_id, site_id,
                            user_message_id, status, stage, progress_percent,
                            response_mode, started_at, created_at, updated_at
                        ) VALUES (
                            :uuid, :conversation_id, :user_id, :site_id,
                            :user_message_id, 'running', 'understanding_request', 5,
                            :response_mode, :now, :now, :now
                        )
                        """
                    ),
                    {
                        "uuid": investigation_uuid,
                        "conversation_id": conversation_id,
                        "user_id": user_id,
                        "site_id": site_id,
                        "user_message_id": user_message_id,
                        "response_mode": response_mode,
                        "now": now,
                    },
                )
                job_id = int(result.lastrowid)
        except SQLAlchemyError as exc:
            raise InvestigationRepositoryError(
                f"Could not create investigation: {exc}"
            ) from exc
        return Investigation(
            id=job_id,
            uuid=investigation_uuid,
            conversation_id=conversation_id,
            conversation_uuid="",
            user_id=user_id,
            site_id=site_id,
            user_message_id=user_message_id,
            question=question,
            response_mode=response_mode,
            status="running",
            stage="understanding_request",
            progress_percent=5,
            updated_at=now,
        )

    def get(self, investigation_id: str, user_id: int, site_id: int) -> Investigation:
        try:
            normalized_uuid = str(UUID(investigation_id))
        except ValueError as exc:
            raise InvestigationNotFoundError("Investigation not found.") from exc
        job = self._select_one(
            "WHERE i.uuid = :uuid AND i.user_id = :user_id AND i.site_id = :site_id",
            {"uuid": normalized_uuid, "user_id": user_id, "site_id": site_id},
        )
        if job is None:
            raise InvestigationNotFoundError("Investigation not found.")
        if job.status == "running" and job.updated_at < _utcnow() - timedelta(minutes=30):
            self.fail(job.id, "Synchronous investigation request did not complete.")
            refreshed = self._select_one("WHERE i.id = :id", {"id": job.id})
            if refreshed is not None:
                return refreshed
        return job

    def update_progress(
        self,
        job_id: int,
        stage: str,
        progress_percent: int,
        normalized_request: dict[str, Any] | None = None,
    ) -> None:
        now = _utcnow()
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE ai_investigations
                        SET stage = :stage,
                            progress_percent = :progress,
                            normalized_request = COALESCE(:normalized_request, normalized_request),
                            updated_at = :now
                        WHERE id = :id AND status = 'running'
                        """
                    ),
                    {
                        "id": job_id,
                        "stage": stage,
                        "progress": max(0, min(progress_percent, 100)),
                        "normalized_request": _json(normalized_request),
                        "now": now,
                    },
                )
        except SQLAlchemyError as exc:
            raise InvestigationRepositoryError(
                f"Could not update investigation progress: {exc}"
            ) from exc

    def complete(self, job_id: int, assistant_message_id: int) -> None:
        self._finish(job_id, "completed", "completed", 100, assistant_message_id, None)

    def fail(self, job_id: int, error: str) -> None:
        self._finish(job_id, "failed", "failed", 100, None, error[:2000])

    def mark_cancelled(self, job_id: int) -> None:
        self._finish(job_id, "cancelled", "cancelled", 100, None, None)

    def request_cancel(self, investigation_id: str, user_id: int, site_id: int) -> Investigation:
        job = self.get(investigation_id, user_id, site_id)
        if job.status != "running":
            return job
        now = _utcnow()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ai_investigations
                    SET cancel_requested_at = :now,
                        status = 'cancelled',
                        stage = 'cancelled',
                        progress_percent = 100,
                        completed_at = :now,
                        updated_at = :now
                    WHERE id = :id AND status = 'running'
                    """
                ),
                {"id": job.id, "now": now},
            )
        return self.get(investigation_id, user_id, site_id)

    def cancel_requested(self, job_id: int) -> bool:
        with self.engine.connect() as connection:
            value = connection.execute(
                text("SELECT cancel_requested_at FROM ai_investigations WHERE id = :id"),
                {"id": job_id},
            ).scalar_one_or_none()
        return value is not None

    def _finish(
        self,
        job_id: int,
        status: str,
        stage: str,
        progress: int,
        assistant_message_id: int | None,
        error: str | None,
    ) -> None:
        now = _utcnow()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE ai_investigations
                    SET status = :status, stage = :stage, progress_percent = :progress,
                        assistant_message_id = :assistant_message_id, error = :error,
                        completed_at = :now, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {
                    "id": job_id,
                    "status": status,
                    "stage": stage,
                    "progress": progress,
                    "assistant_message_id": assistant_message_id,
                    "error": error,
                    "now": now,
                },
            )

    def _select_one(self, where: str, params: dict[str, Any]) -> Investigation | None:
        try:
            with self.engine.connect() as connection:
                row = connection.execute(
                    text(
                        f"""
                        SELECT i.*, c.uuid AS conversation_uuid,
                               um.content AS question, am.content AS answer
                        FROM ai_investigations i
                        JOIN ai_conversations c ON c.id = i.ai_conversation_id
                        JOIN ai_messages um ON um.id = i.user_message_id
                        LEFT JOIN ai_messages am ON am.id = i.assistant_message_id
                        {where}
                        LIMIT 1
                        """
                    ),
                    params,
                ).mappings().first()
        except SQLAlchemyError as exc:
            raise InvestigationRepositoryError(
                f"Could not load investigation: {exc}"
            ) from exc
        if row is None:
            return None
        return Investigation(
            id=int(row["id"]),
            uuid=str(row["uuid"]),
            conversation_id=int(row["ai_conversation_id"]),
            conversation_uuid=str(row["conversation_uuid"]),
            user_id=int(row["user_id"]),
            site_id=int(row["site_id"]),
            user_message_id=int(row["user_message_id"]),
            question=str(row["question"]),
            response_mode=str(row["response_mode"]),
            status=str(row["status"]),
            stage=str(row["stage"]),
            progress_percent=int(row["progress_percent"]),
            updated_at=_as_datetime(row["updated_at"]),
            assistant_message_id=(
                int(row["assistant_message_id"])
                if row["assistant_message_id"] is not None
                else None
            ),
            answer=str(row["answer"]) if row["answer"] is not None else None,
            error=str(row["error"]) if row["error"] is not None else None,
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromisoformat(str(value)).replace(tzinfo=None)


def _json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))

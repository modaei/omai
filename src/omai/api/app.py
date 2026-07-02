from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from threading import BoundedSemaphore
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omai.api.schemas import (
    ChatRequest,
    ChatResponse,
    InvestigationIdentity,
    InvestigationStatusResponse,
)
from omai.investigations.router import is_investigation_request
from omai.investigations.service import InvestigationResult, run_investigation
from omai.investigations.workflow import InvestigationCancelled
from omai.repositories.conversation_repository import (
    Conversation,
    ConversationNotFoundError,
    ConversationRepository,
)
from omai.agents.chat_agent import reasoning_effort_for_response_mode
from omai.repositories.daily_usage_repository import (
    DailyUsageLimitExceeded,
    DailyUsageRepository,
)
from omai.repositories.investigation_repository import (
    Investigation,
    InvestigationNotFoundError,
    InvestigationRepository,
)
from omai.config.logging import configure_logging
from omai.config.settings import Settings
from omai.services.chat_service import answer_chat
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE, is_in_domain
from omai.ui.rag_sources import extract_rag_sources


ALLOWED_HOSTS = {"127.0.0.1", "::1"}

logger = logging.getLogger(__name__)

ChatHandler = Callable[
    [Settings, int, str | None, list[dict[str, str]], str, str],
    tuple[str, list[dict[str, Any]], dict[str, Any]],
]
InvestigationHandler = Callable[
    [
        Settings,
        Investigation,
        Conversation,
        InvestigationRepository,
        ConversationRepository,
    ],
    InvestigationResult,
]


def create_app(
    settings: Settings | None = None,
    chat_handler: ChatHandler = answer_chat,
    conversation_repository: ConversationRepository | None = None,
    daily_usage_repository: DailyUsageRepository | None = None,
    investigation_repository: InvestigationRepository | None = None,
    investigation_handler: InvestigationHandler = run_investigation,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    app = FastAPI(title="ometrics-ai")
    app.state.settings = settings
    app.state.chat_handler = chat_handler
    app.state.conversation_repository = conversation_repository
    app.state.daily_usage_repository = daily_usage_repository
    app.state.investigation_repository = investigation_repository
    app.state.investigation_handler = investigation_handler
    app.state.chat_slots = BoundedSemaphore(settings.omai_max_concurrent)

    def investigations_for(engine=None) -> InvestigationRepository:
        repository = app.state.investigation_repository
        if repository is None:
            repository = (
                InvestigationRepository(engine)
                if engine is not None
                else InvestigationRepository.from_settings(settings)
            )
            app.state.investigation_repository = repository
        return repository

    @app.middleware("http")
    async def allow_only_localhost(request: Request, call_next):
        client = request.client
        if not client or not is_allowed_client_host(client.host):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Access allowed only from localhost",
                },
            )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat", response_model=ChatResponse)
    def chat(payload: ChatRequest) -> ChatResponse:
        if not app.state.chat_slots.acquire(timeout=settings.omai_slot_timeout):
            raise HTTPException(status_code=503, detail="Omai service is busy")

        try:
            repository = (
                app.state.conversation_repository
                or ConversationRepository.from_settings(settings)
            )
            usage_repository = (
                app.state.daily_usage_repository
                or (
                    DailyUsageRepository(
                        repository.engine,
                        request_limit=settings.omai_daily_user_limit_requests,
                        timezone_name=settings.timezone,
                        enabled=settings.omai_daily_user_limit_enabled,
                    )
                    if app.state.conversation_repository is not None
                    else DailyUsageRepository.from_settings(settings)
                )
            )
            conversation_id = payload.conversation_id_text()
            if conversation_id:
                conversation = repository.get(
                    conversation_id,
                    payload.user_id,
                    payload.site_id,
                )
                usage_repository.consume(payload.user_id, payload.site_id)
            else:
                usage_repository.consume(payload.user_id, payload.site_id)
                conversation = repository.create(payload.user_id, payload.site_id)
            history = repository.load_history(conversation)
            reasoning_effort = reasoning_effort_for_response_mode(payload.response_mode)
            if not history and not is_in_domain(payload.message):
                repository.append_message(conversation, "user", payload.message)
                assistant_message_id = repository.append_message(
                    conversation,
                    "assistant",
                    OUT_OF_DOMAIN_RESPONSE,
                    reasoning_effort=reasoning_effort,
                )
                return ChatResponse(
                    conversation_id=conversation.uuid,
                    answer=OUT_OF_DOMAIN_RESPONSE,
                    assistant_message_id=assistant_message_id,
                )
            if is_investigation_request(payload.message) and is_in_domain(
                payload.message, history
            ):
                user_message_id = repository.append_message(
                    conversation, "user", payload.message
                )
                investigations = investigations_for(repository.engine)
                investigation = investigations.create(
                    conversation_id=conversation.id,
                    user_id=payload.user_id,
                    site_id=payload.site_id,
                    user_message_id=user_message_id,
                    question=payload.message,
                    response_mode=payload.response_mode,
                    investigation_id=payload.investigation_id_text(),
                )
                try:
                    result = app.state.investigation_handler(
                        settings,
                        investigation,
                        conversation,
                        investigations,
                        repository,
                    )
                except InvestigationCancelled:
                    return ChatResponse(
                        conversation_id=conversation.uuid,
                        answer="Investigation cancelled.",
                        investigation_id=investigation.uuid,
                        status="cancelled",
                        stage="cancelled",
                        progress_percent=100,
                    )
                return ChatResponse(
                    conversation_id=conversation.uuid,
                    answer=result.answer,
                    assistant_message_id=result.assistant_message_id,
                    investigation_id=investigation.uuid,
                    status="completed",
                    stage="completed",
                    progress_percent=100,
                )
            answer, tool_calls, stats = app.state.chat_handler(
                settings,
                payload.site_id,
                None,
                history,
                payload.message,
                payload.response_mode,
            )
            repository.append_message(conversation, "user", payload.message)
            assistant_message_id = repository.append_message(
                conversation,
                "assistant",
                answer,
                reasoning_effort=reasoning_effort,
                info=_assistant_info(tool_calls, stats, payload.response_mode),
            )
            return ChatResponse(
                conversation_id=conversation.uuid,
                answer=answer,
                assistant_message_id=assistant_message_id,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DailyUsageLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": str(exc),
                    "reset_at": exc.reset_at.isoformat(),
                },
                headers={"Retry-After": str(_retry_after_seconds(exc.reset_at))},
            ) from exc
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Chat request failed")
            raise HTTPException(status_code=503, detail="Chat request failed") from exc
        finally:
            app.state.chat_slots.release()

    @app.get(
        "/investigations/{investigation_id}",
        response_model=InvestigationStatusResponse,
    )
    def investigation_status(
        investigation_id: str,
        user_id: int,
        site_id: int,
    ) -> InvestigationStatusResponse:
        try:
            repository = investigations_for()
            return _investigation_response(
                repository.get(investigation_id, user_id, site_id)
            )
        except InvestigationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/investigations/{investigation_id}/cancel",
        response_model=InvestigationStatusResponse,
    )
    def cancel_investigation(
        investigation_id: str,
        payload: InvestigationIdentity,
    ) -> InvestigationStatusResponse:
        try:
            repository = investigations_for()
            job = repository.request_cancel(
                investigation_id,
                payload.user_id,
                payload.site_id,
            )
            return _investigation_response(job)
        except InvestigationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def is_allowed_client_host(host: str) -> bool:
    return host in ALLOWED_HOSTS


def _retry_after_seconds(reset_at: datetime) -> int:
    reset_at_utc = reset_at
    if reset_at_utc.tzinfo is None:
        reset_at_utc = reset_at_utc.replace(tzinfo=timezone.utc)
    seconds = int((reset_at_utc - datetime.now(timezone.utc)).total_seconds())
    return max(1, seconds)


def _assistant_info(
    tool_calls: list[dict[str, Any]],
    stats: dict[str, Any],
    response_mode: str,
) -> dict[str, Any]:
    return {
        "response_mode": response_mode,
        "tool_calls": [
            {
                "tool": str(tool_call.get("tool")),
                "arguments": tool_call.get("arguments", {}),
            }
            for tool_call in tool_calls
            if tool_call.get("tool")
        ],
        "time_statistics": stats,
        "rag_documents": extract_rag_sources(tool_calls),
        "sql_queries": _sql_query_audit(tool_calls),
    }


def _sql_query_audit(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract compact SQL audit records for `ai_messages.info`.

    SQL tool results can contain returned rows or validation payloads. The audit
    stores only the query request metadata and deliberately ignores the result.
    """
    audit_records: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        tool_name = str(tool_call.get("tool", ""))
        if tool_name not in {"draft_operational_sql", "execute_operational_sql"}:
            continue

        arguments = tool_call.get("arguments", {})
        audit_records.append(
            {
                "tool": tool_name,
                "question": arguments.get("question"),
                "sql": arguments.get("sql"),
                "notes": arguments.get("notes"),
            }
        )
    return audit_records


def _investigation_response(job: Investigation) -> InvestigationStatusResponse:
    return InvestigationStatusResponse(
        investigation_id=job.uuid,
        conversation_id=job.conversation_uuid,
        status=job.status,
        stage=job.stage,
        progress_percent=job.progress_percent,
        answer=job.answer,
        assistant_message_id=job.assistant_message_id,
        error=(
            "The operational investigation could not be completed."
            if job.status == "failed"
            else None
        ),
    )


app = create_app()

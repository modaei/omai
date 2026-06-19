from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from threading import BoundedSemaphore
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omai.api.schemas import ChatRequest, ChatResponse
from omai.repositories.conversation_repository import (
    ConversationNotFoundError,
    ConversationRepository,
)
from omai.repositories.daily_usage_repository import (
    DailyUsageLimitExceeded,
    DailyUsageRepository,
)
from omai.config.logging import configure_logging
from omai.config.settings import Settings
from omai.services.chat_service import answer_chat
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE, is_in_domain


ALLOWED_HOSTS = {"127.0.0.1", "::1"}

logger = logging.getLogger(__name__)

ChatHandler = Callable[
    [Settings, int, str | None, list[dict[str, str]], str],
    tuple[str, list[dict[str, Any]], dict[str, Any]],
]


def create_app(
    settings: Settings | None = None,
    chat_handler: ChatHandler = answer_chat,
    conversation_repository: ConversationRepository | None = None,
    daily_usage_repository: DailyUsageRepository | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    app = FastAPI(title="ometrics-ai")
    app.state.settings = settings
    app.state.chat_handler = chat_handler
    app.state.conversation_repository = conversation_repository
    app.state.daily_usage_repository = daily_usage_repository
    app.state.chat_slots = BoundedSemaphore(settings.omai_max_concurrent)

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
            if not history and not is_in_domain(payload.message):
                repository.append_message(conversation, "user", payload.message)
                repository.append_message(
                    conversation, "assistant", OUT_OF_DOMAIN_RESPONSE
                )
                return ChatResponse(
                    conversation_id=conversation.uuid,
                    answer=OUT_OF_DOMAIN_RESPONSE,
                )
            answer, _tool_calls, _stats = app.state.chat_handler(
                settings,
                payload.site_id,
                None,
                history,
                payload.message,
            )
            repository.append_message(conversation, "user", payload.message)
            repository.append_message(conversation, "assistant", answer)
            return ChatResponse(conversation_id=conversation.uuid, answer=answer)
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

    return app


def is_allowed_client_host(host: str) -> bool:
    return host in ALLOWED_HOSTS


def _retry_after_seconds(reset_at: datetime) -> int:
    reset_at_utc = reset_at
    if reset_at_utc.tzinfo is None:
        reset_at_utc = reset_at_utc.replace(tzinfo=timezone.utc)
    seconds = int((reset_at_utc - datetime.now(timezone.utc)).total_seconds())
    return max(1, seconds)


app = create_app()

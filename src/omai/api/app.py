from __future__ import annotations

import logging
from collections.abc import Callable
from threading import BoundedSemaphore
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from omai.api.schemas import ChatRequest, ChatResponse
from omai.repositories.conversation_repository import (
    ConversationNotFoundError,
    ConversationRepository,
)
from omai.config.settings import Settings
from omai.services.chat_service import answer_chat


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
) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="ometrics-ai")
    app.state.settings = settings
    app.state.chat_handler = chat_handler
    app.state.conversation_repository = conversation_repository
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
            conversation = repository.get_or_create(
                payload.conversation_id_text(),
                payload.user_id,
                payload.site_id,
            )
            history = repository.load_history(conversation)
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


app = create_app()

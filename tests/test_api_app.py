from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from omai.api.app import create_app, is_allowed_client_host
from omai.api.schemas import ChatRequest
from omai.clients.conversation_store import ConversationStore
from omai.config.settings import Settings


def make_settings() -> Settings:
    return Settings(
        openrouter_api_key="test-key",
        openrouter_model="openai/gpt-5-mini",
        openrouter_base_url="https://openrouter.ai/api/v1",
        omreports_api_url="http://127.0.0.1:50008/report/",
        omreports_timeout_seconds=30,
        max_report_days=366,
        db_host="127.0.0.1",
        db_port=3306,
        db_user="user",
        db_password="pass",
        db_name="ometrics",
        db_pool_size=5,
        db_max_overflow=5,
        db_pool_timeout=15,
        db_pool_recycle=1800,
        omai_max_concurrent=2,
        omai_slot_timeout=1,
        omai_conversation_history_limit=20,
        omai_conversation_ttl_hours=168,
        log_level="INFO",
    )


def fake_chat_handler(settings, site_id, site_name, history, question):
    return (
        f"{site_name or 'db lookup'}: {question}",
        [{"tool": "sample", "arguments": {"site_id": site_id}}],
        {
            "total_seconds": 0.1,
            "model_seconds": 0.05,
            "tool_seconds": 0.02,
            "model_calls": 1,
            "tool_calls": [{"tool": "sample", "seconds": 0.02}],
            "history_count": len(history),
        },
    )


def make_conversation_store() -> ConversationStore:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE ai_conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uuid CHAR(36) NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    site_id INTEGER NOT NULL,
                    title VARCHAR(255),
                    expires_at DATETIME,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE ai_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ai_conversation_id INTEGER NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    content TEXT NOT NULL,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
    return ConversationStore(engine, ttl_hours=168, history_limit=20)


def route_endpoint(app, path: str, method: str):
    for route in app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"Route not found: {method} {path}")


def test_localhost_host_check():
    assert is_allowed_client_host("127.0.0.1") is True
    assert is_allowed_client_host("::1") is True
    assert is_allowed_client_host("10.0.0.4") is False


def test_chat_endpoint_creates_conversation_and_returns_answer_only():
    store = make_conversation_store()
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_store=store,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    response = endpoint(
        ChatRequest.model_validate(
            {
                "message": "How much gas was flared in May?",
                "user_id": 9,
                "site_id": 4,
            }
        )
    )

    body = response.model_dump()
    assert body["answer"] == "db lookup: How much gas was flared in May?"
    assert body["conversation_id"]
    assert "tool_calls" not in body
    assert "stats" not in body
    conversation = store.get(body["conversation_id"], user_id=9, site_id=4)
    assert store.load_history(conversation) == [
        {"role": "user", "content": "How much gas was flared in May?"},
        {"role": "assistant", "content": body["answer"]},
    ]


def test_chat_endpoint_continues_existing_conversation():
    store = make_conversation_store()
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_store=store,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    first = endpoint(
        ChatRequest.model_validate(
            {"message": "First", "user_id": 9, "site_id": 4}
        )
    )
    second = endpoint(
        ChatRequest.model_validate(
            {
                "conversation_id": first.conversation_id,
                "message": "Second",
                "user_id": 9,
                "site_id": 4,
            }
        )
    )

    assert second.conversation_id == first.conversation_id
    assert "Second" in second.answer
    conversation = store.get(second.conversation_id, user_id=9, site_id=4)
    assert len(store.load_history(conversation)) == 4


def test_chat_endpoint_rejects_conversation_for_wrong_site():
    store = make_conversation_store()
    conversation = store.create(user_id=9, site_id=4)
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_store=store,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    with pytest.raises(HTTPException) as exc:
        endpoint(
            ChatRequest.model_validate(
                {
                    "conversation_id": conversation.uuid,
                    "message": "Wrong site",
                    "user_id": 9,
                    "site_id": 5,
                }
            )
        )

    assert exc.value.status_code == 404


def test_chat_endpoint_rejects_expired_conversation():
    store = make_conversation_store()
    conversation = store.create(user_id=9, site_id=4)
    with store.engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE ai_conversations
                SET expires_at = :expires_at
                WHERE id = :id
                """
            ),
            {
                "id": conversation.id,
                "expires_at": datetime.now(timezone.utc).replace(tzinfo=None)
                - timedelta(hours=1),
            },
        )
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_store=store,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    with pytest.raises(HTTPException) as exc:
        endpoint(
            ChatRequest.model_validate(
                {
                    "conversation_id": conversation.uuid,
                    "message": "Expired",
                    "user_id": 9,
                    "site_id": 4,
                }
            )
        )

    assert exc.value.status_code == 404


def test_chat_endpoint_rejects_blank_message():
    try:
        ChatRequest.model_validate({"message": "   ", "user_id": 9, "site_id": 4})
    except ValueError as exc:
        assert "message" in str(exc)
    else:
        raise AssertionError("blank message was accepted")


def test_chat_request_requires_site_id():
    try:
        ChatRequest.model_validate({"message": "Hello", "user_id": 9})
    except ValueError as exc:
        assert "site_id" in str(exc)
    else:
        raise AssertionError("missing site_id was accepted")


def test_chat_request_rejects_site_name_field():
    try:
        ChatRequest.model_validate(
            {
                "message": "Hello",
                "user_id": 9,
                "site_id": 4,
                "site_name": "HARTZOG DRAW",
            }
        )
    except ValueError as exc:
        assert "site_name" in str(exc)
    else:
        raise AssertionError("site_name was accepted")


def test_chat_request_requires_user_id():
    try:
        ChatRequest.model_validate({"message": "Hello", "site_id": 4})
    except ValueError as exc:
        assert "user_id" in str(exc)
    else:
        raise AssertionError("missing user_id was accepted")


def test_chat_request_rejects_history_field():
    try:
        ChatRequest.model_validate(
            {
                "message": "Hello",
                "user_id": 9,
                "site_id": 4,
                "history": [],
            }
        )
    except ValueError as exc:
        assert "history" in str(exc)
    else:
        raise AssertionError("history was accepted")


def test_app_registers_chat_and_health_routes():
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_store=make_conversation_store(),
    )

    assert route_endpoint(app, "/chat", "POST")
    assert route_endpoint(app, "/health", "GET")

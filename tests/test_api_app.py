from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from omai.api.app import create_app, is_allowed_client_host
from omai.api.schemas import ChatRequest, RagIndexEventRequest
from omai.config.settings import Settings
from omai.repositories.conversation_repository import ConversationRepository
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE


def make_settings() -> Settings:
    return Settings(
        llm_api_key="test-key",
        llm_model="gpt-5-mini",
        llm_base_url="https://api.openai.com/v1",
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
        omai_daily_user_limit_enabled=True,
        omai_daily_user_limit_requests=100,
        timezone="UTC",
        vector_db_host="127.0.0.1",
        vector_db_port=5432,
        vector_db_user="ometrics",
        vector_db_password="",
        vector_db_name="ometrics",
        vector_db_pool_size=5,
        vector_db_max_overflow=5,
        vector_db_pool_timeout=15,
        vector_db_pool_recycle=1800,
        rag_embedding_model="text-embedding-3-small",
        rag_embedding_dimensions=1536,
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


def failing_chat_handler(settings, site_id, site_name, history, question):
    raise AssertionError("chat handler should not be called")


def make_conversation_repository() -> ConversationRepository:
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
        connection.execute(
            text(
                """
                CREATE TABLE ai_daily_usage_limits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    site_id INTEGER NOT NULL,
                    usage_date DATE NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    created_at DATETIME,
                    updated_at DATETIME,
                    UNIQUE(user_id, site_id, usage_date)
                )
                """
            )
        )
    return ConversationRepository(engine, ttl_hours=168, history_limit=20)


def daily_usage_count(
    repository: ConversationRepository,
    user_id: int = 9,
    site_id: int | None = None,
) -> int:
    site_filter = "" if site_id is None else " AND site_id = :site_id"
    params = {"user_id": user_id, "site_id": site_id}
    with repository.engine.connect() as connection:
        value = connection.execute(
            text(
                f"""
                SELECT COALESCE(SUM(request_count), 0)
                FROM ai_daily_usage_limits
                WHERE user_id = :user_id{site_filter}
                """
            ),
            params,
        ).scalar_one()
    return int(value)


def seed_daily_usage(
    repository: ConversationRepository,
    user_id: int = 9,
    site_id: int = 4,
    request_count: int = 1,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with repository.engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO ai_daily_usage_limits
                    (
                        user_id,
                        site_id,
                        usage_date,
                        request_count,
                        created_at,
                        updated_at
                    )
                VALUES
                    (
                        :user_id,
                        :site_id,
                        :usage_date,
                        :request_count,
                        :now,
                        :now
                    )
                """
            ),
            {
                "user_id": user_id,
                "site_id": site_id,
                "usage_date": datetime.now(timezone.utc).date(),
                "request_count": request_count,
                "now": now,
            },
        )


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
    repository = make_conversation_repository()
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_repository=repository,
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
    conversation = repository.get(body["conversation_id"], user_id=9, site_id=4)
    assert repository.load_history(conversation) == [
        {"role": "user", "content": "How much gas was flared in May?"},
        {"role": "assistant", "content": body["answer"]},
    ]


def test_chat_endpoint_continues_existing_conversation():
    repository = make_conversation_repository()
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_repository=repository,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    first = endpoint(
        ChatRequest.model_validate(
            {"message": "How much gas was flared in May?", "user_id": 9, "site_id": 4}
        )
    )
    second = endpoint(
        ChatRequest.model_validate(
            {
                "conversation_id": first.conversation_id,
                "message": "What about June?",
                "user_id": 9,
                "site_id": 4,
            }
        )
    )

    assert second.conversation_id == first.conversation_id
    assert "What about June?" in second.answer
    conversation = repository.get(second.conversation_id, user_id=9, site_id=4)
    assert len(repository.load_history(conversation)) == 4


def test_chat_endpoint_rejects_conversation_for_wrong_site():
    repository = make_conversation_repository()
    conversation = repository.create(user_id=9, site_id=4)
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_repository=repository,
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
    assert daily_usage_count(repository) == 0


def test_chat_endpoint_rejects_expired_conversation():
    repository = make_conversation_repository()
    conversation = repository.create(user_id=9, site_id=4)
    with repository.engine.begin() as connection:
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
        conversation_repository=repository,
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
    assert daily_usage_count(repository) == 0


def test_chat_endpoint_refuses_out_of_domain_question_without_calling_model():
    repository = make_conversation_repository()
    app = create_app(
        settings=make_settings(),
        chat_handler=failing_chat_handler,
        conversation_repository=repository,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    response = endpoint(
        ChatRequest.model_validate(
            {
                "message": "How old is Paris?",
                "user_id": 9,
                "site_id": 4,
            }
        )
    )

    body = response.model_dump()
    assert body["answer"] == OUT_OF_DOMAIN_RESPONSE
    conversation = repository.get(body["conversation_id"], user_id=9, site_id=4)
    assert repository.load_history(conversation) == [
        {"role": "user", "content": "How old is Paris?"},
        {
            "role": "assistant",
            "content": OUT_OF_DOMAIN_RESPONSE,
        },
    ]


def test_chat_endpoint_rejects_when_daily_user_limit_is_reached():
    repository = make_conversation_repository()
    seed_daily_usage(repository, user_id=9, request_count=1)
    app = create_app(
        settings=replace(make_settings(), omai_daily_user_limit_requests=1),
        chat_handler=failing_chat_handler,
        conversation_repository=repository,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    with pytest.raises(HTTPException) as exc:
        endpoint(
            ChatRequest.model_validate(
                {
                    "message": "How much gas was flared in May?",
                    "user_id": 9,
                    "site_id": 4,
                }
            )
        )

    assert exc.value.status_code == 429
    assert exc.value.detail["message"] == "Daily AI usage limit reached."
    assert exc.value.detail["reset_at"]
    assert int(exc.value.headers["Retry-After"]) > 0
    assert daily_usage_count(repository, user_id=9) == 1
    with repository.engine.connect() as connection:
        conversation_count = connection.execute(
            text("SELECT COUNT(*) FROM ai_conversations")
        ).scalar_one()
    assert conversation_count == 0


def test_chat_endpoint_daily_user_limit_can_be_disabled():
    repository = make_conversation_repository()
    seed_daily_usage(repository, user_id=9, request_count=1)
    app = create_app(
        settings=replace(
            make_settings(),
            omai_daily_user_limit_enabled=False,
            omai_daily_user_limit_requests=1,
        ),
        chat_handler=fake_chat_handler,
        conversation_repository=repository,
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

    assert response.answer == "db lookup: How much gas was flared in May?"
    assert daily_usage_count(repository, user_id=9) == 1


def test_chat_endpoint_daily_user_limit_is_scoped_by_site():
    repository = make_conversation_repository()
    seed_daily_usage(repository, user_id=9, site_id=4, request_count=1)
    app = create_app(
        settings=replace(make_settings(), omai_daily_user_limit_requests=1),
        chat_handler=fake_chat_handler,
        conversation_repository=repository,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    response = endpoint(
        ChatRequest.model_validate(
            {
                "message": "How much gas was flared in May?",
                "user_id": 9,
                "site_id": 5,
            }
        )
    )

    assert response.answer == "db lookup: How much gas was flared in May?"
    assert daily_usage_count(repository, user_id=9, site_id=4) == 1
    assert daily_usage_count(repository, user_id=9, site_id=5) == 1


def test_chat_endpoint_accepts_first_turn_numbered_operational_lookup():
    repository = make_conversation_repository()
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_repository=repository,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    response = endpoint(
        ChatRequest.model_validate(
            {
                "message": "What can you tell me about 4293?",
                "user_id": 9,
                "site_id": 4,
            }
        )
    )

    assert response.answer == "db lookup: What can you tell me about 4293?"


def test_chat_endpoint_lets_model_handle_follow_up_even_if_short():
    repository = make_conversation_repository()
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_repository=repository,
    )
    endpoint = route_endpoint(app, "/chat", "POST")

    first = endpoint(
        ChatRequest.model_validate(
            {
                "message": "Summarize the general notes in current year.",
                "user_id": 9,
                "site_id": 4,
            }
        )
    )
    second = endpoint(
        ChatRequest.model_validate(
            {
                "conversation_id": first.conversation_id,
                "message": "YTD",
                "user_id": 9,
                "site_id": 4,
            }
        )
    )

    assert second.answer == "db lookup: YTD"
    conversation = repository.get(second.conversation_id, user_id=9, site_id=4)
    assert repository.load_history(conversation)[-2:] == [
        {"role": "user", "content": "YTD"},
        {"role": "assistant", "content": "db lookup: YTD"},
    ]


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
        conversation_repository=make_conversation_repository(),
    )

    assert route_endpoint(app, "/chat", "POST")
    assert route_endpoint(app, "/health", "GET")
    assert route_endpoint(app, "/rag/index-event", "POST")


def test_rag_index_event_endpoint_indexes_single_source(monkeypatch):
    class FakeStore:
        def __init__(self):
            self.migrated = False

        def migrate(self):
            self.migrated = True

    class FakeIndexer:
        def __init__(self):
            self.store = FakeStore()

        def index_source(self, site_id, source_type, source_id):
            assert site_id == 4
            assert source_type == "general_note"
            assert source_id == "123"
            return type(
                "Result",
                (),
                {"documents": 1, "chunks": 1, "deleted_chunks": 1},
            )()

    fake_indexer = FakeIndexer()
    monkeypatch.setattr(
        "omai.api.app._rag_indexer_from_settings",
        lambda settings: fake_indexer,
    )
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_repository=make_conversation_repository(),
    )
    endpoint = route_endpoint(app, "/rag/index-event", "POST")

    response = endpoint(
        RagIndexEventRequest.model_validate(
            {
                "site_id": 4,
                "source_type": "general_note",
                "source_id": "123",
                "operation": "updated",
            }
        )
    )

    assert response.ok is True
    assert response.documents == 1
    assert response.chunks == 1
    assert response.deleted_chunks == 1
    assert fake_indexer.store.migrated is True


def test_rag_index_event_endpoint_rejects_unknown_source_type():
    app = create_app(
        settings=make_settings(),
        chat_handler=fake_chat_handler,
        conversation_repository=make_conversation_repository(),
    )
    endpoint = route_endpoint(app, "/rag/index-event", "POST")

    with pytest.raises(HTTPException) as exc:
        endpoint(
            RagIndexEventRequest.model_validate(
                {
                    "site_id": 4,
                    "source_type": "not_real",
                    "source_id": "123",
                    "operation": "updated",
                }
            )
        )

    assert exc.value.status_code == 400

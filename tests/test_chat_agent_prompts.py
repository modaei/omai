from omai.prompts.chat_agent import (
    AUTHORITATIVE_CONTEXT_PROMPT_VERSION,
    CHAT_SYSTEM_PROMPT_VERSION,
    SQL_DRAFT_VALID_PROMPT_VERSION,
    SQL_EXECUTED_PROMPT_VERSION,
    SQL_FAILED_PROMPT_VERSION,
    build_authoritative_context_message,
    build_chat_system_message,
    build_sql_draft_valid_message,
    build_sql_executed_message,
    build_sql_failed_message,
    build_sql_success_final_message,
)
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE


def test_chat_system_prompt_has_versioned_xml_sections_and_variables():
    message = build_chat_system_message(
        site_name="HARTZOG DRAW",
        site_id=4,
        today="2026-07-27",
    )
    content = message.content

    assert CHAT_SYSTEM_PROMPT_VERSION in content
    assert "<identity>" in content
    assert "<site_context>" in content
    assert "<tool_routing>" in content
    assert "<report_rules>" in content
    assert "<sql_rules>" in content
    assert "<unsupported_actions>" in content
    assert "The selected site is HARTZOG DRAW" in content
    assert "The internal site_id is 4" in content
    assert "never mention it in answers" in content
    assert "Today is 2026-07-27." in content
    assert OUT_OF_DOMAIN_RESPONSE in content
    assert "{site_id}" not in content
    assert "{today}" not in content


def test_authoritative_context_prompt_is_versioned_and_rendered():
    message = build_authoritative_context_message("prefetched facts")

    assert AUTHORITATIVE_CONTEXT_PROMPT_VERSION in message.content
    assert "<authoritative_context>" in message.content
    assert "prefetched facts" in message.content
    assert "{authoritative_context}" not in message.content


def test_sql_control_prompts_are_versioned():
    executed = build_sql_executed_message({"row_count": 1})
    success = build_sql_success_final_message()
    draft_valid = build_sql_draft_valid_message()
    failed = build_sql_failed_message()

    assert SQL_EXECUTED_PROMPT_VERSION in executed.content
    assert SQL_EXECUTED_PROMPT_VERSION in success.content
    assert SQL_DRAFT_VALID_PROMPT_VERSION in draft_valid.content
    assert SQL_FAILED_PROMPT_VERSION in failed.content
    assert "<sql_execution_result>" in executed.content
    assert "<sql_final_response>" in success.content
    assert "<sql_draft_valid>" in draft_valid.content
    assert "<sql_failed>" in failed.content

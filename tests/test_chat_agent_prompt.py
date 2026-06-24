from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from omai.agents.chat_agent import (
    answer_chat_question,
    build_model,
    reasoning_effort_for_response_mode,
)
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE


class FakeModel:
    def __init__(self):
        self.messages = None

    def bind_tools(self, tools, parallel_tool_calls=False):
        return self

    def invoke(self, messages):
        self.messages = messages
        return AIMessage(content="Done.")


class FakeToolModel:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools, parallel_tool_calls=False):
        return self

    def invoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "name": "sample_tool",
                        "args": {"value": "abc"},
                    }
                ],
            )
        return AIMessage(content="Final answer.")


class FakeToolBoundSqlModel:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("tool-bound model should not be used after SQL success")
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "call_sql",
                    "name": "execute_operational_sql",
                    "args": {
                        "question": "Average downtime by code",
                        "sql": "SELECT 1 LIMIT 1",
                    },
                }
            ],
        )


class FakeSqlStopModel:
    def __init__(self):
        self.bound_model = FakeToolBoundSqlModel()
        self.final_messages = None

    def bind_tools(self, tools, parallel_tool_calls=False):
        return self.bound_model

    def invoke(self, messages):
        self.final_messages = messages
        return AIMessage(content="Final answer from SQL rows.")


class FakeLastRoundSqlBoundModel:
    def __init__(self):
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        if self.calls < 6:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call_sample_{self.calls}",
                        "name": "sample_tool",
                        "args": {"value": str(self.calls)},
                    }
                ],
            )
        if self.calls == 6:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_sql",
                        "name": "execute_operational_sql",
                        "args": {
                            "question": "Average downtime by code",
                            "sql": "SELECT 1 LIMIT 1",
                        },
                    }
                ],
            )
        raise AssertionError("tool-bound model should not be used after SQL success")


class FakeLastRoundSqlModel:
    def __init__(self):
        self.bound_model = FakeLastRoundSqlBoundModel()

    def bind_tools(self, tools, parallel_tool_calls=False):
        return self.bound_model

    def invoke(self, messages):
        return AIMessage(content="Final answer after last-round SQL.")


class FakeUnitsDisclaimerModel:
    def bind_tools(self, tools, parallel_tool_calls=False):
        return self

    def invoke(self, messages):
        return AIMessage(
            content=(
                "I ran the Gas Flared report for 2026-05-01 to 2026-05-31.\n\n"
                "Total gas flared (May 2026): 2,093\n"
                "The report did not specify units."
            )
        )


@tool
def sample_tool(value: str) -> str:
    """Return a sample value."""
    return f"result: {value}"


@tool
def execute_operational_sql(question: str, sql: str) -> str:
    """Return a successful SQL execution payload."""
    return '{"ok":true,"executed":true,"row_count":0,"rows":[]}'


def test_system_prompt_rejects_unsupported_actions():
    model = FakeModel()

    answer, traces, stats = answer_chat_question(
        model=model,
        tools=[],
        site_id=1,
        site_name="HARTZOG DRAW",
        history=[],
        question="Can you email this report?",
    )

    system_prompt = model.messages[0].content
    assert answer == "Done."
    assert traces == []
    assert stats["model_calls"] == 1
    assert stats["model_seconds"] >= 0
    assert stats["tool_seconds"] == 0
    assert "Only say you can perform actions that are backed by the available tools" in system_prompt
    assert "Use search_ometrics_capabilities only for explicit Ometrics software-help questions" in system_prompt
    assert "Do not use capability guidance for factual data requests" in system_prompt
    assert "top/bottom results" in system_prompt
    assert "Only run report tools" in system_prompt
    assert "Use summarize_report_by_month" in system_prompt
    assert "do not call run_report separately for each" in system_prompt
    assert "outside Ometrics" in system_prompt
    assert OUT_OF_DOMAIN_RESPONSE in system_prompt
    assert "Use search_operational_context" in system_prompt
    assert "Use summarize_shutdown_causes" in system_prompt
    assert "first prefer the most specific domain tool" in system_prompt
    assert "capability tools only as the lowest-priority path" in system_prompt
    assert "not for data retrieval" in system_prompt
    assert "execute_operational_sql as the second priority" in system_prompt
    assert "Use draft_operational_sql only when you need schema" in system_prompt
    assert "Operational SQL runs on MySQL/MariaDB" in system_prompt
    assert "LOWER(column) LIKE" in system_prompt
    assert "DATE_FORMAT" in system_prompt
    assert "After execute_operational_sql returns ok=true" in system_prompt
    assert "After draft_operational_sql returns ok=true" in system_prompt
    assert "Do not draft alternate versions" in system_prompt
    assert "row_count is 0" in system_prompt
    assert "short follow-up commands" in system_prompt
    assert "use the prior conversation context" in system_prompt
    assert "available_columns" in system_prompt
    assert "do not run SELECT * only for schema discovery" in system_prompt
    assert "bare numeric well reference" in system_prompt
    assert "well-name suffix" in system_prompt
    assert "Use summarize_work_order_costs" in system_prompt
    assert "Use search_operational_context for general work-order questions" in system_prompt
    assert "Do not use search_operational_context to calculate work order totals" in system_prompt
    assert "format them as US dollars" in system_prompt
    assert "`$` prefix" in system_prompt
    assert "use barrels" in system_prompt
    assert "Start directly with a short interpretation" in system_prompt
    assert "Do not include a 'Sources' section" in system_prompt
    assert "unless the user explicitly asks for sources" in system_prompt
    assert "Do not add replacement record-list sections" in system_prompt
    assert "Summary of returned records" in system_prompt
    assert "Do not list each returned record by date" in system_prompt
    assert "Do not add default breakdowns" in system_prompt
    assert "counts by level" in system_prompt
    assert "dates with entries" in system_prompt
    assert "named 'Interpretation', 'Facts', or 'Inference'" in system_prompt
    assert "find_all_missing_readings" in system_prompt
    assert "find_all_missing_readings_for_range" in system_prompt
    assert "do not call find_all_missing_readings separately for each date" in system_prompt
    assert "Use reading tools for tank volume questions" in system_prompt
    assert "oil_volume" in system_prompt
    assert "do not say tank charts or" in system_prompt
    assert "The selected site is HARTZOG DRAW" in system_prompt
    assert "never mention it in answers" in system_prompt
    assert "Do not mention missing unit labels" in system_prompt
    assert "unspecified unit disclaimers" in system_prompt
    assert "accepts a previous offer" in system_prompt
    assert "ask whether the user wants you to run that report" in system_prompt
    assert "For workflow guidance" in system_prompt
    assert "Do not invent UI steps" in system_prompt
    assert "which report should I use for allocation" in system_prompt
    assert "Create a new LACT reading" in system_prompt
    assert "Battery = No Battery" in system_prompt
    assert "Use ISO YYYY-MM-DD dates only for tool arguments" in system_prompt
    assert "format dates as MM/DD/YYYY" in system_prompt
    assert "You do not have tools to send emails" in system_prompt
    assert "Do not add generic follow-up offers" in system_prompt
    assert "Do not refer to entities by database ID" in system_prompt
    assert "do not call them assets" in system_prompt


def test_successful_sql_execution_forces_final_answer_without_more_tools():
    model = FakeSqlStopModel()

    answer, traces, stats = answer_chat_question(
        model=model,
        tools=[execute_operational_sql],
        site_id=1,
        site_name="HARTZOG DRAW",
        history=[],
        question="Average downtime by code",
    )

    assert answer == "Final answer from SQL rows."
    assert len(traces) == 1
    assert traces[0]["tool"] == "execute_operational_sql"
    assert stats["model_calls"] == 2
    assert model.bound_model.calls == 1
    assert model.final_messages is not None


def test_last_round_successful_sql_still_gets_final_answer():
    model = FakeLastRoundSqlModel()

    answer, traces, stats = answer_chat_question(
        model=model,
        tools=[sample_tool, execute_operational_sql],
        site_id=1,
        site_name="HARTZOG DRAW",
        history=[],
        question="Average downtime by code",
    )

    assert answer == "Final answer after last-round SQL."
    assert len(traces) == 6
    assert traces[-1]["tool"] == "execute_operational_sql"
    assert stats["model_calls"] == 7
    assert model.bound_model.calls == 6


def test_tool_trace_includes_tool_result_for_local_debugging():
    answer, traces, stats = answer_chat_question(
        model=FakeToolModel(),
        tools=[sample_tool],
        site_id=1,
        history=[],
        question="Use the sample tool.",
    )

    assert answer == "Final answer."
    assert traces == [
        {
            "tool": "sample_tool",
            "arguments": {"value": "abc"},
            "result": "result: abc",
        }
    ]
    assert stats["model_calls"] == 2
    assert stats["model_seconds"] >= 0
    assert stats["tool_seconds"] >= 0
    assert len(stats["tool_calls"]) == 1
    assert stats["tool_calls"][0]["tool"] == "sample_tool"
    assert stats["tool_calls"][0]["seconds"] >= 0


def test_units_disclaimer_is_removed_from_final_answer():
    answer, traces, stats = answer_chat_question(
        model=FakeUnitsDisclaimerModel(),
        tools=[],
        site_id=1,
        history=[],
        question="How much gas was flared during May?",
    )

    assert "Total gas flared" in answer
    assert "The report did not specify units" not in answer
    assert traces == []
    assert stats["model_calls"] == 1


def test_response_modes_map_to_reasoning_effort():
    assert reasoning_effort_for_response_mode("fast") == "medium"
    assert reasoning_effort_for_response_mode("intelligent") == "high"
    assert reasoning_effort_for_response_mode("unknown") == "medium"


def test_build_model_sets_reasoning_effort():
    model = build_model(
        api_key="test-key",
        model="gpt-5-mini",
        base_url="https://api.openai.com/v1",
        reasoning_effort="high",
    )

    assert model.reasoning_effort == "high"

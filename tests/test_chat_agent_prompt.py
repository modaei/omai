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
    assert "Use search_ometrics_capabilities" in system_prompt
    assert "how to know" in system_prompt
    assert "Only run report tools" in system_prompt
    assert "Use summarize_report_by_month" in system_prompt
    assert "do not call run_report separately for each" in system_prompt
    assert "outside Ometrics" in system_prompt
    assert OUT_OF_DOMAIN_RESPONSE in system_prompt
    assert "Use search_operational_context" in system_prompt
    assert "Use summarize_shutdown_causes" in system_prompt
    assert "bare numeric well reference" in system_prompt
    assert "well-name suffix" in system_prompt
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
    assert "Use reading tools for tank volume questions" in system_prompt
    assert "oil_volume" in system_prompt
    assert "do not say tank charts or" in system_prompt
    assert "The selected site is HARTZOG DRAW" in system_prompt
    assert "never mention it in answers" in system_prompt
    assert "Do not mention units" in system_prompt
    assert "unless the user asks about units" in system_prompt
    assert "accepts a previous offer" in system_prompt
    assert "ask whether the user wants you to run that report" in system_prompt
    assert "For workflow guidance" in system_prompt
    assert "Do not invent UI steps" in system_prompt
    assert "which report or feature to use" in system_prompt
    assert "Create a new LACT reading" in system_prompt
    assert "Battery = No Battery" in system_prompt
    assert "Use ISO YYYY-MM-DD dates only for tool arguments" in system_prompt
    assert "format dates as MM/DD/YYYY" in system_prompt
    assert "You do not have tools to send emails" in system_prompt
    assert "Do not add generic follow-up offers" in system_prompt
    assert "Do not refer to entities by database ID" in system_prompt
    assert "do not call them assets" in system_prompt


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
    assert reasoning_effort_for_response_mode("faster") == "medium"
    assert reasoning_effort_for_response_mode("more_accurate") == "xhigh"
    assert reasoning_effort_for_response_mode("unknown") == "medium"


def test_build_model_sets_reasoning_effort():
    model = build_model(
        api_key="test-key",
        model="gpt-5-mini",
        base_url="https://api.openai.com/v1",
        reasoning_effort="xhigh",
    )

    assert model.reasoning_effort == "xhigh"

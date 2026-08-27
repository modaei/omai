import json
from datetime import date

from langchain_core.messages import AIMessage

from omai.agents.rod_pump_graph import (
    compact_rod_pump_context,
    is_explicit_reassessment,
    is_rod_pump_question,
    try_answer_rod_pump_question,
)
from omai.tools.rod_pump_analysis_tools import build_rod_pump_analysis_tools


class Client:
    def analyze(self, site_id, well_name, start_time, end_time):
        return {
            "site_id": site_id,
            "well": {"id": 82, "name": "HDU 5823", "key": "HDU_5823"},
            "interval": {"start": "2026-07-01T00:00:00+00:00", "end": "2026-07-02T00:00:00+00:00"},
            "diagnoses": [{"name": "fluid pound", "severity": "high", "confidence": 72}],
            "health": {"category": "watchlist"},
            "trend_features": {"pump_fillage": {"latest": 61.8}},
            "dynograph_features": {},
            "chart_note_events": [],
            "warnings": [],
        }


class Model:
    def __init__(self, responses):
        self.responses = list(responses)
        self.bindings = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append([tool.name for tool in tools])
        return self

    def invoke(self, messages):
        return self.responses.pop(0)


def test_routes_clear_rod_pump_language_and_follow_ups_only_with_context():
    assert is_rod_pump_question("Analyze rod-pump health for 5823")
    assert not is_rod_pump_question("What happened with 5823?")
    assert is_rod_pump_question("Can I change the setting?", {"well": {"name": "HDU 5823"}})
    assert not is_rod_pump_question("How much oil did Battery 6 produce?", {"well": {"name": "HDU 5823"}})
    assert is_rod_pump_question(
        "Assess 5823", rod_pump_well_resolver=lambda candidate: candidate == "5823"
    )
    assert not is_rod_pump_question(
        "Assess 5823", rod_pump_well_resolver=lambda candidate: False
    )


def test_explicit_reassessment_detection():
    assert is_explicit_reassessment("Please reassess using the last 3 days")
    assert not is_explicit_reassessment("Can I change max load remotely?")


def test_specialist_uses_only_analysis_tool_and_returns_direct_response():
    model = Model([
        AIMessage(content="", tool_calls=[{"name": "analyze_rod_pump", "args": {"well_name": "5823"}, "id": "call-1"}]),
        AIMessage(content="HDU 5823 has high-severity fluid pound at 72% confidence."),
    ])
    answer, traces, stats = try_answer_rod_pump_question(
        model=model,
        tools=build_rod_pump_analysis_tools(Client(), 4),
        question="Analyze rod-pump health for 5823",
        history=[],
        site_name="Hartzog",
        today=date(2026, 7, 2),
    )

    assert answer == "HDU 5823 has high-severity fluid pound at 72% confidence."
    assert model.bindings == [["analyze_rod_pump"]]
    assert traces[0]["tool"] == "analyze_rod_pump"
    assert stats["model_calls"] == 2


def test_follow_up_reuses_compact_context_without_analysis_call():
    context = compact_rod_pump_context({"ok": True, **Client().analyze(4, "5823", None, None)})
    model = Model([AIMessage(content="Keep the current setting until approved evidence supports a change.")])
    answer, traces, stats = try_answer_rod_pump_question(
        model=model,
        tools=build_rod_pump_analysis_tools(Client(), 4),
        question="Can I change max load remotely?",
        history=[{"role": "assistant", "content": "Previous assessment."}],
        site_name="Hartzog",
        today=date(2026, 7, 2),
        prior_context=context,
    )

    assert "Keep the current setting" in answer
    assert traces == []
    assert model.bindings == []
    assert stats["model_calls"] == 1
    assert context["well"]["name"] == "HDU 5823"

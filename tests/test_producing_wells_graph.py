import json
from datetime import date

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from omai.agents.producing_wells_graph import (
    prepare_well_population_dependency,
    try_answer_producing_well_question,
)


class AnyArguments(BaseModel):
    producing_date: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    range_mode: str = "any_day"


class WellTestArguments(BaseModel):
    start_date: str
    end_date: str


class ActiveArguments(BaseModel):
    active_date: str


def make_tools(calls):
    def get_producing_wells(**arguments):
        calls.append(("get_producing_wells", arguments))
        return json.dumps(
            {
                "ok": True,
                "producing_count": 3,
                "producing_wells": [
                    {"well": "Well - ALPHA"},
                    {"well": "Well - BRAVO"},
                    {"well": "Well - CHARLIE"},
                ],
            }
        )

    def search_well_tests(start_date, end_date):
        calls.append(
            ("search_well_tests", {"start_date": start_date, "end_date": end_date})
        )
        return json.dumps(
            {
                "ok": True,
                "well_tests": [
                    {"entity_display_name": "Well - ALPHA"},
                    {"entity_display_name": "Well - CHARLIE"},
                ],
            }
        )

    def get_active_wells(active_date):
        calls.append(("get_active_wells", {"active_date": active_date}))
        return json.dumps(
            {
                "ok": True,
                "date": active_date,
                "active_count": 2,
                "active_wells": [
                    {"well": "Well - ALPHA"},
                    {"well": "Well - BRAVO"},
                ],
                "partial_shutdown_well_names": ["Well - CHARLIE"],
                "rules": {"short_shutdown_full_day_hours": 24},
            }
        )
    return [
        StructuredTool.from_function(
            get_producing_wells,
            name="get_producing_wells",
            description="test",
            args_schema=AnyArguments,
        ),
        StructuredTool.from_function(
            search_well_tests,
            name="search_well_tests",
            description="test",
            args_schema=WellTestArguments,
        ),
        StructuredTool.from_function(
            get_active_wells,
            name="get_active_wells",
            description="test",
            args_schema=ActiveArguments,
        ),
    ]


def test_routes_producer_well_test_coverage_without_model_call():
    calls = []
    result = try_answer_producing_well_question(
        tools=make_tools(calls),
        question="Which producing wells had no well test in June 2026?",
        today=date(2026, 7, 2),
    )

    assert result is not None
    answer, traces, stats = result
    assert calls[0][0] == "get_producing_wells"
    assert calls[0][1]["start_date"] == "2026-06-01"
    assert calls[0][1]["end_date"] == "2026-06-30"
    assert calls[0][1]["range_mode"] == "any_day"
    assert calls[1][0] == "search_well_tests"
    assert "BRAVO" in answer
    assert "ALPHA" not in answer
    assert [trace["tool"] for trace in traces] == [
        "get_producing_wells",
        "search_well_tests",
    ]
    assert stats["model_calls"] == 0
    assert stats["model_seconds"] == 0


def test_routes_simple_current_producer_count_to_single_date_tool():
    calls = []
    result = try_answer_producing_well_question(
        tools=make_tools(calls),
        question="How many producing wells do we have today?",
        today=date(2026, 7, 2),
    )

    assert result is not None
    answer, _, stats = result
    assert calls[0][0] == "get_producing_wells"
    assert calls[0][1]["producing_date"] == "2026-07-02"
    assert answer == "There were 3 producing wells on 07/02/2026."
    assert stats["model_calls"] == 0


def test_unrelated_question_falls_through_to_general_agent():
    calls = []

    result = try_answer_producing_well_question(
        tools=make_tools(calls),
        question="How much oil did Battery 6 produce in June?",
        today=date(2026, 7, 2),
    )

    assert result is None
    assert calls == []


def test_prefetches_producing_population_for_broader_question():
    calls = []

    dependency = prepare_well_population_dependency(
        tools=make_tools(calls),
        question="Summarize alarms for producing wells in June 2026.",
        today=date(2026, 7, 2),
    )

    assert dependency is not None
    assert dependency["tool_name"] == "get_producing_wells"
    context = json.loads(dependency["context"])
    assert context["authoritative_population"] == "producing_wells"
    assert context["well_names"] == ["ALPHA", "BRAVO", "CHARLIE"]
    assert dependency["stats"]["model_calls"] == 0


def test_prefetches_active_population_for_single_day_broader_question():
    calls = []

    dependency = prepare_well_population_dependency(
        tools=make_tools(calls),
        question="Which active wells had alarms yesterday?",
        today=date(2026, 7, 2),
    )

    assert dependency is not None
    assert calls == [("get_active_wells", {"active_date": "2026-07-01"})]
    context = json.loads(dependency["context"])
    assert context["well_names"] == ["ALPHA", "BRAVO"]
    assert context["partial_shutdown_well_names"] == ["CHARLIE"]


def test_does_not_apply_undefined_active_well_range_semantics():
    calls = []

    dependency = prepare_well_population_dependency(
        tools=make_tools(calls),
        question="Which active wells had alarms in June 2026?",
        today=date(2026, 7, 2),
    )

    assert dependency is None
    assert calls == []

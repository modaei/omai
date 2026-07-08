import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from omai.agents.data_point_graph import try_answer_data_point_value_question


class Arguments(BaseModel):
    facility_name: str
    data_point_name: str
    device_name: str | None = None


def make_tool(calls, *, multiple=False, ambiguous=False):
    def get_data_point_values(**arguments):
        calls.append(arguments)
        if ambiguous:
            return json.dumps(
                {
                    "ok": True,
                    "status": "ambiguous",
                    "selector": "facility",
                    "requested": arguments["facility_name"],
                    "candidates": ["ABC_2510", "HDU_2510"],
                }
            )
        values = [
            {
                "data_point_name": "stroke_min",
                "value": 4.84,
                "value_available": True,
                "received_at": "07/02/2026 10:00:00 UTC",
            }
        ]
        if multiple:
            values = [
                {
                    "data_point_name": "min_load_ls",
                    "value": 15449,
                    "value_available": True,
                    "received_at": "07/02/2026 10:00:00 UTC",
                },
                {
                    "data_point_name": "min_load_sp",
                    "value": 13800,
                    "value_available": True,
                    "received_at": "07/02/2026 10:00:01 UTC",
                },
            ]
        return json.dumps(
            {
                "ok": True,
                "status": "found",
                "facility_name": "HDU_2510",
                "device_name": arguments.get("device_name") or "HDU_2510",
                "device_defaulted": arguments.get("device_name") is None,
                "values": values,
            }
        )

    return StructuredTool.from_function(
        get_data_point_values,
        name="get_data_point_values",
        description="test",
        args_schema=Arguments,
    )


def test_routes_common_value_question_without_model_call_and_hides_default_device():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls)],
        question="What is the value of data point stroke min in 2510?",
    )

    assert result is not None
    answer, traces, stats = result
    assert calls[0]["facility_name"] == "2510"
    assert calls[0]["data_point_name"] == "stroke min"
    assert "HDU_2510" in answer
    assert " on HDU_2510" not in answer
    assert "4.84" in answer
    assert traces[0]["tool"] == "get_data_point_values"
    assert stats["model_calls"] == 0


def test_formats_all_closest_data_point_matches():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls, multiple=True)],
        question="What is the value of datapoint min load in 2510?",
    )

    assert "min_load_ls" in result[0]
    assert "min_load_sp" in result[0]


def test_explicit_device_is_included_in_answer():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls)],
        question="What is the value of data_point stroke min in facility 2510 device Controller A?",
    )

    assert calls[0]["device_name"] == "Controller A"
    assert "on Controller A" in result[0]


def test_ambiguous_facility_requests_clarification():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls, ambiguous=True)],
        question="What is the value of data-point stroke min in 2510?",
    )

    assert "Please specify one" in result[0]
    assert "ABC_2510" in result[0]


def test_unrelated_question_falls_through():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls)], question="Why was production low in June?"
    )

    assert result is None
    assert calls == []


def test_other_value_question_without_data_point_marker_falls_through():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls)],
        question="What is the value of top level in 10-1 Oil tank?",
    )

    assert result is None
    assert calls == []


def test_routes_facility_first_data_point_question_without_model():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls, multiple=True)],
        question="what is 4048 data point peak load",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "4048"
    assert calls[0]["data_point_name"] == "peak load"
    assert calls[0]["device_name"] is None
    assert result[2]["model_calls"] == 0


def test_routes_point_first_data_point_question_without_value_words():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls)],
        question="show me data_point stroke min for well 2510",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "2510"
    assert calls[0]["data_point_name"] == "stroke min"


def test_routes_facility_first_question_with_explicit_device():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls)],
        question=(
            "get facility 2510 device Controller A data-point stroke min"
        ),
    )

    assert result is not None
    assert calls[0]["facility_name"] == "2510"
    assert calls[0]["device_name"] == "Controller A"
    assert calls[0]["data_point_name"] == "stroke min"


def test_possessive_facility_is_normalized_in_facility_first_question():
    calls = []
    result = try_answer_data_point_value_question(
        tools=[make_tool(calls)],
        question="what is 4048's datapoint stroke min",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "4048"


def test_multiple_or_incomplete_data_point_markers_fall_through():
    calls = []

    assert (
        try_answer_data_point_value_question(
            tools=[make_tool(calls)],
            question="compare 4048 data point peak load with 2510 data point peak load",
        )
        is None
    )
    assert (
        try_answer_data_point_value_question(
            tools=[make_tool(calls)],
            question="what is data point peak load",
        )
        is None
    )
    assert calls == []

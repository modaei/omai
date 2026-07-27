import json
from datetime import date

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from omai.agents.data_point_graph import (
    try_answer_data_point_trend_question,
    try_answer_data_point_value_question,
)


class Arguments(BaseModel):
    facility_name: str
    data_point_name: str
    device_name: str | None = None


class TrendArguments(BaseModel):
    facility_name: str
    data_point_name: str
    days: int = 7
    start_date: str | None = None
    end_date: str | None = None
    device_name: str | None = None
    equipment_type: str | None = None


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


def make_trend_tool(calls):
    def analyze_data_point_trend(**arguments):
        calls.append(arguments)
        return json.dumps(
            {
                "ok": True,
                "status": "found",
                "facility_name": "HDU_2510",
                "device_name": "HDU_2510",
                "device_defaulted": True,
                "equipment": None,
                "data_point_name": "Pump Fillage",
                "days": arguments.get("days", 7),
                "start_date": arguments.get("start_date"),
                "end_date": arguments.get("end_date"),
                "interval_label": (
                    f"{arguments['start_date']} to {arguments['end_date']}"
                    if arguments.get("start_date") and arguments.get("end_date")
                    else None
                ),
                "interval": {
                    "start": "06/01/2026 00:00:00 UTC",
                    "end": "06/08/2026 00:00:00 UTC",
                },
                "sample_count": 6,
                "summary": {
                    "status": "ok",
                    "first": {"time": "06/01/2026 00:00:00 UTC", "value": 10},
                    "latest": {"time": "06/08/2026 00:00:00 UTC", "value": 20},
                    "min": 10,
                    "max": 20,
                    "average": 15,
                    "change": 10,
                    "change_percent": 100,
                    "trend": "rising",
                    "anomalies": [],
                },
            }
        )

    return StructuredTool.from_function(
        analyze_data_point_trend,
        name="analyze_data_point_trend",
        description="test",
        args_schema=TrendArguments,
    )


def make_oscillating_trend_tool(calls):
    def analyze_data_point_trend(**arguments):
        calls.append(arguments)
        return json.dumps(
            {
                "ok": True,
                "status": "found",
                "facility_name": "HDU_2510",
                "device_name": "HDU_2510",
                "device_defaulted": True,
                "equipment": None,
                "data_point_name": "Current Load",
                "days": arguments.get("days", 7),
                "interval_label": "07/01/2026 to 07/31/2026",
                "interval": {
                    "start": "07/01/2026 00:00:00 UTC",
                    "end": "07/31/2026 23:59:59 UTC",
                },
                "sample_count": 175,
                "summary": {
                    "status": "ok",
                    "first": {"time": "07/01/2026 00:00:00 UTC", "value": 20703.0},
                    "latest": {"time": "07/31/2026 23:30:00 UTC", "value": 17913.5},
                    "min": 0.0,
                    "max": 24582.8,
                    "average": 19613.4,
                    "median": 20120.0,
                    "typical_band": {"low": 18050.0, "high": 22240.0},
                    "pattern": "oscillating",
                    "variability": "high",
                    "overall_shape": "oscillating",
                    "change": -2789.5,
                    "change_percent": -13.47,
                    "trend": "falling",
                    "significant_changes": [
                        {
                            "direction": "drop",
                            "time": "07/12/2026 01:30:00 UTC",
                            "from": 18200,
                            "to": 0,
                            "change": -18200,
                            "change_percent": -100,
                        }
                    ],
                    "anomalies": [
                        "Drop to zero at 07/12/2026 01:30:00 UTC; this looks abnormal compared with the normal band."
                    ],
                },
            }
        )

    return StructuredTool.from_function(
        analyze_data_point_trend,
        name="analyze_data_point_trend",
        description="test",
        args_schema=TrendArguments,
    )


def make_flat_spike_trend_tool(calls):
    def analyze_data_point_trend(**arguments):
        calls.append(arguments)
        return json.dumps(
            {
                "ok": True,
                "status": "found",
                "facility_name": "HDU_2510",
                "device_name": "HDU_2510",
                "device_defaulted": True,
                "equipment": None,
                "data_point_name": "Min Load LS",
                "days": arguments.get("days", 7),
                "start_date": arguments.get("start_date"),
                "end_date": arguments.get("end_date"),
                "interval_label": "06/01/2026 to 06/30/2026",
                "interval": {
                    "start": "06/01/2026 00:00:00 UTC",
                    "end": "06/30/2026 23:59:59 UTC",
                },
                "sample_count": 186,
                "summary": {
                    "status": "ok",
                    "first": {
                        "time": "06/11/2026 17:00:00 CEST",
                        "timestamp": "2026-06-11T17:00:00+02:00",
                        "value": 15924,
                    },
                    "latest": {
                        "time": "06/30/2026 23:30:00 CEST",
                        "timestamp": "2026-06-30T23:30:00+02:00",
                        "value": 15491,
                    },
                    "min": 15401.4,
                    "max": 65535,
                    "average": 17975.4,
                    "median": 15677.08,
                    "typical_band": {"low": 15504.5, "high": 15882.5},
                    "pattern": "mostly_flat",
                    "variability": "low",
                    "overall_shape": "mostly_flat",
                    "change": -433,
                    "change_percent": -2.72,
                    "trend": "stable",
                    "average_distorted_by_outliers": True,
                    "significant_changes": [
                        {
                            "direction": "spike",
                            "time": "06/14/2026 20:00:00 CEST",
                            "from": 15800,
                            "to": 65535,
                            "change": 49735,
                            "change_percent": 314.78,
                        }
                    ],
                    "anomalies": [
                        "Abnormal high spike/plateau from 06/14/2026 20:00:00 CEST to 06/15/2026 16:00:00 CEST, peaking at 65,535."
                    ],
                },
            }
        )

    return StructuredTool.from_function(
        analyze_data_point_trend,
        name="analyze_data_point_trend",
        description="test",
        args_schema=TrendArguments,
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


def test_routes_point_first_trend_question_without_model_call():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="analyze pump fillage trend of 2510 in the past 14 days",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "2510"
    assert calls[0]["data_point_name"] == "pump fillage"
    assert calls[0]["days"] == 14
    assert "Pump Fillage" in result[0]
    assert result[2]["model_calls"] == 0


def test_routes_month_trend_question_without_including_month_in_facility():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="analyze current load trend of 2510 in July",
        today=date(2026, 7, 23),
    )

    assert result is not None
    assert calls[0]["facility_name"] == "2510"
    assert calls[0]["data_point_name"] == "current load"
    assert calls[0]["start_date"] == "2026-07-01"
    assert calls[0]["end_date"] == "2026-07-31"


def test_routes_stroke_length_month_trend_question():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="analyze stroke length trend of 2510 in June",
        today=date(2026, 7, 23),
    )

    assert result is not None
    assert calls[0]["facility_name"] == "2510"
    assert calls[0]["data_point_name"] == "stroke length"
    assert calls[0]["start_date"] == "2026-06-01"
    assert calls[0]["end_date"] == "2026-06-30"


def test_trend_answer_reads_like_graph_interpretation():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_oscillating_trend_tool(calls)],
        question="analyze current load trend of well 2510 in July",
        today=date(2026, 7, 23),
    )

    assert result is not None
    answer = result[0]
    assert "mostly oscillated, usually staying between 18,050 and 22,240" in answer
    assert "high short-term variability" in answer
    assert "dropped from 18,200 to 0" in answer
    assert "a change of 18,200 (100.00%)" in answer
    assert "Anomaly notes" not in answer
    assert "Range: min" not in answer


def test_flat_trend_answer_does_not_call_it_oscillation_and_mentions_high_spike():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_flat_spike_trend_tool(calls)],
        question="analyze min load ls trend of 2510 in June",
        today=date(2026, 7, 23),
    )

    assert result is not None
    answer = result[0]
    assert "mostly flat" in answer
    assert "mostly oscillated" not in answer
    assert "rose from 15,800 to 65,535" in answer
    assert "a change of 49,735 (314.78%)" in answer
    assert "Anomaly notes" not in answer
    assert "average is distorted by the outlier" not in answer
    assert "Returned samples cover 2026-06-11 to 2026-06-30" in answer


def test_routes_equipment_first_trend_question_without_model_call():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="analyze tank 10-1 Oil level trend",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "10-1 Oil"
    assert calls[0]["data_point_name"] == "level"
    assert calls[0]["equipment_type"] == "tank"


def test_routes_tank_level_question_without_explicit_trend_word():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="analyze 13-7 Water level",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "13-7 Water"
    assert calls[0]["data_point_name"] == "level"
    assert calls[0]["equipment_type"] == "tank"


def test_infers_tank_from_level_trend_location():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="analyze level trend of 13-7 Water",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "13-7 Water"
    assert calls[0]["data_point_name"] == "level"
    assert calls[0]["equipment_type"] == "tank"


def test_strips_tank_prefix_from_point_first_trend_location():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="analyze level trend of tank 13-7 Water",
    )

    assert result is not None
    assert calls[0]["facility_name"] == "13-7 Water"
    assert calls[0]["data_point_name"] == "level"
    assert calls[0]["equipment_type"] == "tank"


def test_non_trend_question_falls_through():
    calls = []
    result = try_answer_data_point_trend_question(
        tools=[make_trend_tool(calls)],
        question="what is the value of data point pump fillage in 2510",
    )

    assert result is None
    assert calls == []

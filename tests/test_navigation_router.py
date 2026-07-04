import json
from datetime import date

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.agents.navigation_router import try_answer_navigation_request


class ViewArguments(BaseModel):
    view_type: str
    start_date: str | None = None
    end_date: str | None = None
    search_text: str | None = None
    status: str | None = None


class EntryArguments(BaseModel):
    entry_type: str
    entity_name: str | None = None
    values: dict = Field(default_factory=dict)


def make_tools(calls, *, view_status="ready", entry_status="ready"):
    def prepare_data_view(**arguments):
        calls.append(("prepare_data_view", arguments))
        return json.dumps(
            {
                "status": view_status,
                "destination": "index",
                "view_type": arguments["view_type"],
                "message": "Do you want short shutdowns or long shutdowns?",
            }
        )

    def prepare_data_entry(**arguments):
        calls.append(("prepare_data_entry", arguments))
        if entry_status == "needs_clarification":
            return json.dumps(
                {
                    "status": entry_status,
                    "message": "Which LACT should this use?",
                    "candidates": [
                        {"type": "LACT", "name": "LACT 1"},
                        {"type": "LACT", "name": "LACT 2"},
                    ],
                }
            )
        return json.dumps(
            {
                "status": entry_status,
                "entry_type": arguments["entry_type"],
            }
        )

    return [
        StructuredTool.from_function(
            prepare_data_view,
            name="prepare_data_view",
            description="test",
            args_schema=ViewArguments,
        ),
        StructuredTool.from_function(
            prepare_data_entry,
            name="prepare_data_entry",
            description="test",
            args_schema=EntryArguments,
        ),
    ]


def test_show_me_report_routes_with_relative_dates_without_model():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="Show me oil production this month",
        today=date(2026, 7, 3),
    )

    assert result is not None
    assert calls[0][0] == "prepare_data_view"
    assert calls[0][1]["view_type"] == "oil_production"
    assert calls[0][1]["start_date"] == "2026-07-01"
    assert calls[0][1]["end_date"] == "2026-07-03"
    assert result[2]["model_calls"] == 0
    assert json.loads(result[1][0]["result"])["status"] == "ready"


def test_show_me_operational_view_extracts_search_text_before_month():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="Show me well tests for 4048 in June 2026",
        today=date(2026, 7, 3),
    )

    assert result is not None
    assert calls[0][1] == {
        "view_type": "well_tests",
        "start_date": "2026-06-01",
        "end_date": "2026-06-30",
        "search_text": "4048",
        "status": None,
    }


def test_show_me_resolves_named_start_date_until_today():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="show me oil production from june 1st until today",
        today=date(2026, 7, 4),
    )

    assert result is not None
    assert calls[0][1]["start_date"] == "2026-06-01"
    assert calls[0][1]["end_date"] == "2026-07-04"
    assert result[2]["model_calls"] == 0


def test_show_me_resolves_explicit_range_connector_formats():
    examples = (
        ("from 2026-06-01 through 2026-07-04", "2026-06-01", "2026-07-04"),
        ("from 06/01/2026 to 07/04/2026", "2026-06-01", "2026-07-04"),
        ("between june 1st and july 4th", "2026-06-01", "2026-07-04"),
        ("from june until today", "2026-06-01", "2026-07-04"),
    )

    for date_text, expected_start, expected_end in examples:
        calls = []
        result = try_answer_navigation_request(
            tools=make_tools(calls),
            question=f"show me oil production {date_text}",
            today=date(2026, 7, 4),
        )

        assert result is not None
        assert calls[0][1]["start_date"] == expected_start
        assert calls[0][1]["end_date"] == expected_end


def test_show_me_separates_trailing_search_text_from_explicit_date_range():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question=(
            "show me tank readings from June 2nd until June 10th "
            "for 15-7 Water"
        ),
        today=date(2026, 7, 4),
    )

    assert result is not None
    assert calls[0][1]["view_type"] == "tank_readings"
    assert calls[0][1]["start_date"] == "2026-06-02"
    assert calls[0][1]["end_date"] == "2026-06-10"
    assert calls[0][1]["search_text"] == "15-7 water"
    assert result[2]["model_calls"] == 0


def test_explicit_range_connectors_allow_trailing_search_text():
    examples = (
        "from 2026-06-02 through 2026-06-10 for 15-7 Water",
        "from 06/02/2026 to 06/10/2026 for 15-7 Water",
        "between June 2nd and June 10th for 15-7 Water",
    )

    for date_text in examples:
        calls = []
        result = try_answer_navigation_request(
            tools=make_tools(calls),
            question=f"show me tank readings {date_text}",
            today=date(2026, 7, 4),
        )

        assert result is not None
        assert calls[0][1]["start_date"] == "2026-06-02"
        assert calls[0][1]["end_date"] == "2026-06-10"
        assert calls[0][1]["search_text"] == "15-7 water"


def test_unrecognized_show_me_request_falls_through():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="Show me everything interesting",
        today=date(2026, 7, 3),
    )

    assert result is None
    assert calls == []


def test_data_entry_routes_entity_and_labelled_values_without_model():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question=(
            "Create a flare reading for Battery 6 Flare with pressure 10 "
            "and volume 20 today"
        ),
        today=date(2026, 7, 3),
    )

    assert result is not None
    arguments = calls[0][1]
    assert arguments["entry_type"] == "flare_reading"
    assert arguments["entity_name"] == "battery 6 flare"
    assert arguments["values"] == {
        "date": "2026-07-03",
        "pressure": 10.0,
        "volume": 20.0,
    }
    assert result[2]["model_calls"] == 0


def test_generic_reading_and_client_clarification_are_deterministic():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls, entry_status="needs_clarification"),
        question="Record a reading",
        today=date(2026, 7, 3),
    )

    assert result is not None
    assert calls[0][1]["entry_type"] == "generic_reading"
    assert "LACT - LACT 1" in result[0]


def test_unknown_numeric_entry_value_falls_through_to_model():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="Create a well test for 4048 with unknown metric 25",
        today=date(2026, 7, 3),
    )

    assert result is None
    assert calls == []


def test_multiple_entry_types_fall_through_to_model():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="Create LACT and flare readings",
        today=date(2026, 7, 3),
    )

    assert result is None
    assert calls == []


def test_generic_reading_infers_lact_type_from_complete_entity_phrase():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="create a reading for Battery 6 Lact with reading 4545",
        today=date(2026, 7, 4),
    )

    assert result is not None
    assert calls[0][1]["entry_type"] == "lact_reading"
    assert calls[0][1]["entity_name"] == "battery 6 lact"
    assert calls[0][1]["values"] == {"reading": 4545.0}
    assert result[2]["model_calls"] == 0


def test_generic_reading_infers_every_supported_equipment_marker():
    examples = {
        "Battery 6 Flare": "flare_reading",
        "Battery 6 Flow Meter": "flow_meter_reading",
        "Injection Pump 1": "pump_reading",
        "Battery 6 Treater": "treater_reading",
        "Battery 6 Knock Out": "knockout_reading",
        "Water Plant 1": "water_plant_reading",
        "Battery 6 Lact": "lact_reading",
        "Tank 6-1": "tank_reading",
    }

    for entity_name, expected_type in examples.items():
        calls = []
        result = try_answer_navigation_request(
            tools=make_tools(calls),
            question=f"Create a reading for {entity_name}",
            today=date(2026, 7, 4),
        )

        assert result is not None
        assert calls[0][1]["entry_type"] == expected_type
        assert calls[0][1]["entity_name"] == entity_name.lower()


def test_generic_reading_with_multiple_entity_markers_falls_through():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="Create a reading for Battery 6 Lact Flare",
        today=date(2026, 7, 4),
    )

    assert result is None
    assert calls == []


def test_general_note_extracts_unquoted_trailing_comments():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="create general notes comments I'm having a good day",
        today=date(2026, 7, 4),
    )

    assert result is not None
    assert calls[0][1]["entry_type"] == "general_note"
    assert calls[0][1]["values"] == {
        "comments": "i'm having a good day",
    }
    assert result[2]["model_calls"] == 0


def test_unquoted_comments_can_contain_numbers_without_llm_fallback():
    calls = []
    result = try_answer_navigation_request(
        tools=make_tools(calls),
        question="create general note comments checked 3 devices",
        today=date(2026, 7, 4),
    )

    assert result is not None
    assert calls[0][1]["values"]["comments"] == "checked 3 devices"

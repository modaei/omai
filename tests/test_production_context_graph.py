import json

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from omai.agents.production_context_graph import prepare_production_context_dependency


class MonthlyReportArguments(BaseModel):
    report_name: str
    year: int
    battery_name: str | None = None
    value_key: str | None = None


class OperationalContextArguments(BaseModel):
    query: str
    start_date: str | None = None
    end_date: str | None = None
    entity_name: str | None = None
    limit: int = 8


def make_tools(calls):
    def summarize_report_by_month(**arguments):
        calls.append(("summarize_report_by_month", arguments))
        return json.dumps(
            {
                "ok": True,
                "report_name": arguments["report_name"],
                "year": arguments["year"],
                "battery_name": arguments["battery_name"],
                "months": [{"month": "2026-06", "total": 4314}],
            }
        )

    def search_operational_context(**arguments):
        calls.append(("search_operational_context", arguments))
        return json.dumps(
            {
                "ok": True,
                "count": 1,
                "matches": [{"text": "Battery 6 had emulsion correction notes."}],
            }
        )

    return [
        StructuredTool.from_function(
            summarize_report_by_month,
            name="summarize_report_by_month",
            description="test",
            args_schema=MonthlyReportArguments,
        ),
        StructuredTool.from_function(
            search_operational_context,
            name="search_operational_context",
            description="test",
            args_schema=OperationalContextArguments,
        ),
    ]


class FakeWellFilterClient:
    def find_wells(self, site_id, well_name=None, filters=None):
        assert site_id == 4
        assert filters == [{"field": "battery", "value": "Battery 6"}]
        return {
            "wells": [
                {"name": "HARTZOG DRAW UNIT 4048"},
                {"name": "HARTZOG DRAW UNIT 5823"},
            ]
        }


class FakeReadingClient:
    def list_equipment(self, site_id, equipment_type, battery_name=None, **kwargs):
        assert site_id == 4
        assert battery_name == "Battery 6"
        if equipment_type == "tank":
            return {"entities": [{"entity_name": "5-2 Float Over"}]}
        if equipment_type == "pump":
            return {"entities": [{"entity_name": "Battery 6 Pump"}]}
        return {"entities": []}


def test_prefetches_production_variation_report_and_context():
    calls = []

    dependency = prepare_production_context_dependency(
        tools=make_tools(calls),
        question="why battery 6 had oil production variation in 2026?",
        site_id=4,
        reading_client=FakeReadingClient(),
        well_filter_client=FakeWellFilterClient(),
    )

    assert dependency is not None
    assert calls[0] == (
        "summarize_report_by_month",
        {
            "report_name": "oil_production",
            "year": 2026,
            "battery_name": "Battery 6",
            "value_key": None,
        },
    )
    assert calls[1] == (
        "search_operational_context",
        {
            "query": "Battery 6 oil production variation reason 2026",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "entity_name": "Battery 6",
            "limit": 10,
        },
    )
    assert calls[2][0] == "search_operational_context"
    assert calls[2][1]["entity_name"] == "HARTZOG DRAW UNIT 4048"
    assert calls[3][1]["entity_name"] == "HARTZOG DRAW UNIT 5823"
    assert calls[4][1]["entity_name"] == "5-2 Float Over"
    assert calls[5][1]["entity_name"] == "Battery 6 Pump"
    context = json.loads(dependency["context"])
    assert context["authoritative_data_type"] == "production_variation_context"
    assert context["related_entities"]["wells"] == [
        "HARTZOG DRAW UNIT 4048",
        "HARTZOG DRAW UNIT 5823",
    ]
    assert context["related_entities"]["equipment"]["tank"] == ["5-2 Float Over"]
    assert context["related_entities"]["equipment"]["pump"] == ["Battery 6 Pump"]
    assert [trace["tool"] for trace in dependency["traces"]] == [
        "summarize_report_by_month",
        "search_operational_context",
        "search_operational_context",
        "search_operational_context",
        "search_operational_context",
        "search_operational_context",
    ]
    assert dependency["stats"]["model_calls"] == 0


def test_non_reason_question_does_not_prefetch_production_context():
    dependency = prepare_production_context_dependency(
        tools=make_tools([]),
        question="show battery 6 oil production in 2026",
        site_id=4,
    )

    assert dependency is None

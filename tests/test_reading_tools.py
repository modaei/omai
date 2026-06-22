import json

from omai.tools.reading_tools import build_reading_tools


class FakeReadingClient:
    def __init__(self):
        self.range_calls = []

    def list_reading_types(self):
        return []

    def all_missing_readings_for_date(self, site_id, reading_date):
        return {
            "reading_type": "all_supported_missing_readings",
            "date": reading_date,
            "missing_count": 0,
            "groups": [],
        }

    def all_missing_readings_for_range(self, site_id, start_date, end_date):
        self.range_calls.append(
            {"site_id": site_id, "start_date": start_date, "end_date": end_date}
        )
        return {
            "reading_type": "all_supported_missing_readings",
            "start_date": start_date,
            "end_date": end_date,
            "day_count": 7,
            "missing_count": 0,
            "dates": [],
        }


def tool_by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_find_all_missing_readings_tool_uses_single_client_call():
    tools = build_reading_tools(FakeReadingClient(), 4)
    result = json.loads(
        tool_by_name(tools, "find_all_missing_readings").invoke(
            {"reading_date": "2026-05-20"}
        )
    )

    assert result["ok"] is True
    assert result["reading_type"] == "all_supported_missing_readings"
    assert result["date"] == "2026-05-20"


def test_find_all_missing_readings_for_range_tool_uses_single_client_call():
    client = FakeReadingClient()
    tools = build_reading_tools(client, 4)
    result = json.loads(
        tool_by_name(tools, "find_all_missing_readings_for_range").invoke(
            {"start_date": "2026-06-15", "end_date": "2026-06-21"}
        )
    )

    assert result["ok"] is True
    assert result["reading_type"] == "all_supported_missing_readings"
    assert result["start_date"] == "2026-06-15"
    assert result["end_date"] == "2026-06-21"
    assert client.range_calls == [
        {
            "site_id": 4,
            "start_date": "2026-06-15",
            "end_date": "2026-06-21",
        }
    ]

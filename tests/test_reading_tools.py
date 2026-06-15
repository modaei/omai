import json

from omai.tools.reading_tools import build_reading_tools


class FakeReadingClient:
    def list_reading_types(self):
        return []

    def all_missing_readings_for_date(self, site_id, reading_date):
        return {
            "reading_type": "all_supported_missing_readings",
            "date": reading_date,
            "missing_count": 0,
            "groups": [],
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

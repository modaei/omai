import json

from omai.tools.report_tools import build_report_tools


class FakeReportClient:
    def run_report(self, site_id, report_name, start_date, end_date):
        return [{"date": start_date, "value": 12.5}]


def tool_by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_run_report_result_exposes_site_name_not_site_id():
    tools = build_report_tools(FakeReportClient(), 4, "HARTZOG DRAW")
    result = json.loads(
        tool_by_name(tools, "run_report").invoke(
            {
                "report_name": "water_injection",
                "start_date": "2026-05-01",
                "end_date": "2026-05-31",
            }
        )
    )

    assert result["ok"] is True
    assert result["site_name"] == "HARTZOG DRAW"
    assert "site_id" not in result
    assert result["report_name"] == "water_injection"


def test_list_available_reports_result_exposes_site_name_not_site_id():
    tools = build_report_tools(FakeReportClient(), 4, "HARTZOG DRAW")
    result = json.loads(tool_by_name(tools, "list_available_reports").invoke({}))

    assert result["site_name"] == "HARTZOG DRAW"
    assert "site_id" not in result
    assert "water_injection" in result["reports"]

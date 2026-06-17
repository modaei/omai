import json

from omai.tools.report_tools import build_report_tools


class FakeReportClient:
    def run_report(self, site_id, report_name, start_date, end_date):
        return [{"date": start_date, "value": 12.5}]


class FakeMonthlyReportClient:
    def __init__(self):
        self.calls = []

    def run_report(self, site_id, report_name, start_date, end_date):
        self.calls.append((site_id, report_name, start_date, end_date))
        return {
            "date_battery_values": {
                "Battery 5": [
                    {"date": 1767225600000, "value": 99},
                ],
                "Battery 6": [
                    {"date": 1767225600000, "value": 10},
                    {"date": 1769904000000, "value": 12.345},
                    {"date": 1772323200000, "value": 7},
                ],
            }
        }


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


def test_summarize_report_by_month_uses_one_annual_report_call():
    client = FakeMonthlyReportClient()
    tools = build_report_tools(client, 4, "HARTZOG DRAW")

    result = json.loads(
        tool_by_name(tools, "summarize_report_by_month").invoke(
            {
                "report_name": "oil_production",
                "year": 2026,
                "battery_name": "6",
            }
        )
    )

    assert client.calls == [(4, "oil_production", "2026-01-01", "2026-12-31")]
    assert result["ok"] is True
    assert result["site_name"] == "HARTZOG DRAW"
    assert result["battery_name"] == "Battery 6"
    assert result["value_key"] == "value"
    assert result["monthly_totals"]["2026-01"] == 10
    assert result["monthly_totals"]["2026-02"] == 12.35
    assert result["monthly_totals"]["2026-03"] == 7
    assert result["monthly_totals"]["2026-04"] == 0
    assert result["matched_daily_rows"] == 3

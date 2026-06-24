import json

from omai.tools.shutdown_tools import build_shutdown_tools


class FakeShutdownClient:
    def summarize_shutdown_causes(self, site_id, start_date, end_date, shutdown_type):
        return {
            "site_id": site_id,
            "start_date": start_date,
            "end_date": end_date,
            "shutdown_type": shutdown_type,
            "main_cause": {"downtime_reason": "Paraffin"},
            "causes": [],
        }

    def get_shutdowns(self, site_id, start_date, end_date, shutdown_type):
        return {
            "site_id": site_id,
            "start_date": start_date,
            "end_date": end_date,
            "shutdown_type": shutdown_type,
            "short_shutdowns": [],
            "long_shutdowns": [],
        }

    def get_current_long_shutdowns(self, site_id, as_of_date=None):
        return {
            "site_id": site_id,
            "as_of_date": as_of_date or "2026-06-24",
            "long_shutdowns": [],
        }

    def list_downtime_codes(self):
        return {"downtime_codes": {}}


class FakeOperationalContextStore:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "query": kwargs["query"],
            "count": 1,
            "matches": [{"source_type": "chart_note", "text": "Shutdown context"}],
        }


def tool_by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_summarize_shutdown_causes_tool_adds_operational_context():
    store = FakeOperationalContextStore()
    tools = build_shutdown_tools(
        FakeShutdownClient(),
        site_id=4,
        operational_context_store=store,
    )

    result = json.loads(
        tool_by_name(tools, "summarize_shutdown_causes").invoke(
            {
                "start_date": "2026-05-01",
                "end_date": "2026-05-31",
                "shutdown_type": "all",
            }
        )
    )

    assert result["ok"] is True
    assert result["operational_context"]["matches"][0]["text"] == "Shutdown context"
    assert store.calls[0]["site_id"] == 4
    assert "Paraffin" in store.calls[0]["query"]

import json

from omai.clients.database_schema_client import DatabaseSchemaClient
from omai.tools.database_schema_tools import build_database_schema_tools


def tool_by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_draft_operational_sql_returns_non_executed_draft(tmp_path):
    schema_path = tmp_path / "database_schema.md"
    schema_path.write_text(
        "Allowed Tables\n- `wells`\n- `well_tests`\n\n"
        "Site filter: `wells.site_id = :site_id`\n",
        encoding="utf-8",
    )
    tools = build_database_schema_tools(DatabaseSchemaClient(schema_path), site_id=4)

    result = json.loads(
        tool_by_name(tools, "draft_operational_sql").invoke(
            {
                "question": "Which wells had high oil tests?",
                "sql": (
                    "SELECT wells.name, well_tests.oil "
                    "FROM well_tests "
                    "JOIN wells ON well_tests.well_id = wells.id "
                    "WHERE wells.site_id = :site_id"
                ),
                "notes": "Draft only.",
            }
        )
    )

    assert result["ok"] is True
    assert result["executed"] is False
    assert result["site_id"] == 4
    assert "SELECT wells.name" in result["sql"]
    assert "Allowed Tables" in result["schema"]
    assert "not executed" in result["warning"].lower()


def test_draft_operational_sql_reports_missing_schema(tmp_path):
    tools = build_database_schema_tools(
        DatabaseSchemaClient(tmp_path / "missing.md"),
        site_id=4,
    )

    result = json.loads(
        tool_by_name(tools, "draft_operational_sql").invoke(
            {"question": "Test", "sql": "SELECT 1"}
        )
    )

    assert result["ok"] is False
    assert result["executed"] is False
    assert "Could not load database schema document" in result["error"]

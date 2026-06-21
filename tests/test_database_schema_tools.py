import json

from omai.clients.database_schema_client import DatabaseSchemaClient
from omai.tools.database_schema_tools import build_database_schema_tools
from omai.tools.operational_sql_validator import (
    OperationalSqlValidationError,
    OperationalSqlValidator,
)


class FakeSchemaClient(DatabaseSchemaClient):
    def __init__(self, schema_path, column_metadata):
        super().__init__(schema_path)
        self.column_metadata = column_metadata

    def load_column_metadata(self):
        return self.column_metadata


def tool_by_name(tools, name):
    return next(tool for tool in tools if tool.name == name)


def test_draft_operational_sql_returns_non_executed_draft(tmp_path):
    schema_path = tmp_path / "database_schema.md"
    schema_path.write_text(
        "Allowed Tables\n- `wells`\n- `well_tests`\n\n"
        "Site filter: `wells.site_id = :site_id`\n",
        encoding="utf-8",
    )
    tools = build_database_schema_tools(
        FakeSchemaClient(
            schema_path,
            {
                "wells": {"id", "site_id", "name"},
                "well_tests": {"well_id", "oil"},
            },
        ),
        site_id=4,
    )

    result = json.loads(
        tool_by_name(tools, "draft_operational_sql").invoke(
            {
                "question": "Which wells had high oil tests?",
                "sql": (
                    "SELECT wells.name, well_tests.oil "
                    "FROM well_tests "
                    "JOIN wells ON well_tests.well_id = wells.id "
                    "WHERE wells.site_id = :site_id "
                    "LIMIT 10"
                ),
                "notes": "Draft only.",
            }
        )
    )

    assert result["ok"] is True
    assert result["executed"] is False
    assert result["site_id"] == 4
    assert "SELECT wells.name" in result["sql"]
    assert result["validation"]["valid"] is True
    assert result["validation"]["tables"] == ["well_tests", "wells"]
    assert result["validation"]["referenced_columns"]["wells"] == ["id", "name", "site_id"]
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


def test_draft_operational_sql_returns_validation_error(tmp_path):
    schema_path = tmp_path / "database_schema.md"
    schema_path.write_text("Allowed Tables\n- `wells`\n", encoding="utf-8")
    tools = build_database_schema_tools(
        FakeSchemaClient(schema_path, {"wells": {"id", "site_id", "name"}}),
        site_id=4,
    )

    result = json.loads(
        tool_by_name(tools, "draft_operational_sql").invoke(
            {
                "question": "Find wells.",
                "sql": "SELECT wells.bad_column FROM wells WHERE wells.site_id = :site_id LIMIT 10",
            }
        )
    )

    assert result["ok"] is False
    assert result["executed"] is False
    assert result["validation"]["valid"] is False
    assert "Unknown column reference" in result["validation"]["error"]


def test_operational_sql_validator_rejects_write_statement():
    validator = OperationalSqlValidator({"wells": {"id", "site_id", "name"}})

    try:
        validator.validate("UPDATE wells SET name = 'x' WHERE site_id = :site_id LIMIT 1")
    except OperationalSqlValidationError as exc:
        assert "Only SELECT statements" in str(exc)
    else:
        raise AssertionError("write statement was accepted")


def test_operational_sql_validator_rejects_non_allowlisted_table():
    validator = OperationalSqlValidator({"users": {"id", "site_id"}})

    try:
        validator.validate("SELECT users.id FROM users WHERE users.site_id = :site_id LIMIT 10")
    except OperationalSqlValidationError as exc:
        assert "non-allowlisted" in str(exc)
    else:
        raise AssertionError("non-allowlisted table was accepted")


def test_operational_sql_validator_rejects_missing_site_scope():
    validator = OperationalSqlValidator(
        {
            "wells": {"id", "site_id", "name"},
            "well_tests": {"well_id", "oil"},
        }
    )

    try:
        validator.validate(
            "SELECT w.name, wt.oil FROM well_tests wt "
            "JOIN wells w ON wt.well_id = w.id LIMIT 10"
        )
    except OperationalSqlValidationError as exc:
        assert "selected site" in str(exc)
    else:
        raise AssertionError("query without site scope was accepted")


def test_operational_sql_validator_rejects_child_without_parent_join():
    validator = OperationalSqlValidator({"well_tests": {"well_id", "oil"}})

    try:
        validator.validate("SELECT well_tests.oil FROM well_tests LIMIT 10")
    except OperationalSqlValidationError as exc:
        assert "must join through `wells`" in str(exc)
    else:
        raise AssertionError("child table without site parent join was accepted")


def test_operational_sql_validator_rejects_unknown_column():
    validator = OperationalSqlValidator({"wells": {"id", "site_id", "name"}})

    try:
        validator.validate(
            "SELECT wells.missing FROM wells WHERE wells.site_id = :site_id LIMIT 10"
        )
    except OperationalSqlValidationError as exc:
        assert "Unknown column reference" in str(exc)
    else:
        raise AssertionError("unknown column was accepted")

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.clients.database_schema_client import (
    DatabaseSchemaClient,
    DatabaseSchemaClientError,
    OperationalSqlExecutionError,
)
from omai.tools.operational_sql_validator import (
    OperationalSqlValidationError,
    OperationalSqlValidator,
)


logger = logging.getLogger(__name__)


MYSQL_SQL_DIALECT_GUIDANCE = {
    "dialect": "mysql_mariadb",
    "rules": [
        "Use LOWER(column) LIKE '%text%' for case-insensitive matching; do not use ILIKE.",
        "Use DATE_FORMAT(column, '%Y-%m') for monthly grouping; do not use DATE_TRUNC.",
        "Use plain string date literals like '2026-05-01'; do not use DATE '2026-05-01'.",
        "Do not use PostgreSQL casts such as ::date.",
        "Use DATE(datetime_column) only when matching a datetime to a date column.",
    ],
}


COMMON_AGGREGATE_HINTS = [
    "Shutdown totals/reasons: join well_shutdowns ws to wells w, filter w.site_id = :site_id, use ws.date, SUM(ws.hours), and group by ws.downtime_code or w.name.",
    "Well-test aggregates: join well_tests wt to wells w, filter w.site_id = :site_id, use wt.time and aggregate wt.oil, wt.water, wt.gas, or wt.runtime.",
    "Well-test battery aggregates: left join batteries b on b.id = w.battery_id and group by COALESCE(b.name, 'No Battery').",
    "Mixed-tank oil volume: join mixed_tank_readings m to tanks t, filter t.site_id = :site_id, compute oil height from top level minus water level, multiply by t.bbl_foot.",
    "Flow meter aggregates: join flow_meter_readings r to flow_meters fm, filter fm.site_id = :site_id, use r.time, r.total, r.flow, r.odometer, and fm.type.",
]


class OperationalSqlInput(BaseModel):
    """Arguments shared by the operational SQL draft and execution tools.

    The model supplies the question and SQL, while Omai supplies the selected
    site_id as a protected bind parameter during execution.
    """

    question: str = Field(
        description="The user's operational question that the SQL draft is meant to answer."
    )
    sql: str = Field(
        description=(
            "A proposed read-only SELECT query. It must include `:site_id` for "
            "site scoping and a numeric LIMIT."
        )
    )
    notes: str | None = Field(
        default=None,
        description="Optional explanation of assumptions, joins, or expected output.",
    )


def _json_result(value: Any) -> str:
    """Serialize tool output compactly for the model."""
    return json.dumps(value, default=str, separators=(",", ":"))


def _available_columns(
    table_names: list[str],
    column_metadata: dict[str, set[str]],
) -> dict[str, list[str]]:
    """Return sorted available columns for the tables referenced by SQL.

    The model commonly guesses names like `duration_hours` or `alarm_date`.
    Including table columns in validation feedback lets it repair the query in
    the next round instead of repeatedly guessing and hitting the tool limit.
    """
    return {
        table: sorted(column_metadata.get(table, set()))
        for table in table_names
        if table in column_metadata
    }


def build_database_schema_tools(
    client: DatabaseSchemaClient,
    site_id: int,
    max_rows: int = 100,
) -> list[StructuredTool]:
    """Build curated operational SQL tools for the selected site.

    Existing domain tools should remain the first choice. These tools are the
    flexible second path for operational questions that need a direct database
    query across the curated allowlist.
    """

    def _validate_sql(sql: str):
        """Load metadata and validate the model-supplied SQL once per tool call."""
        # Load live column metadata for the allowlisted tables. This validates
        # columns without trusting the model to know the current schema.
        column_metadata = client.load_column_metadata()
        validator = OperationalSqlValidator(column_metadata)
        validation = validator.validate(sql)
        if validation.limit > max_rows:
            raise OperationalSqlValidationError(
                f"SQL LIMIT cannot be greater than {max_rows}."
            )
        return validation, column_metadata

    def draft_operational_sql(
        question: str,
        sql: str,
        notes: str | None = None,
    ) -> str:
        """Return schema context and validation feedback without executing SQL."""
        logger.info("Drafting non-executed operational SQL site_id=%s", site_id)
        try:
            # Load the allowlisted schema at invocation time so edits to the
            # markdown are picked up without rebuilding the Python package.
            schema = client.load_schema()
            validation, column_metadata = _validate_sql(sql)
            # Echo the drafted SQL with explicit executed=false metadata. This is
            # useful when the model needs schema feedback before running a query.
            return _json_result(
                {
                    "ok": True,
                    "executed": False,
                    "site_id": site_id,
                    "question": question,
                    "sql": validation.sql,
                    "notes": notes,
                    "validation": {
                        "valid": True,
                        "tables": validation.tables,
                        "aliases": validation.aliases,
                        "referenced_columns": validation.referenced_columns,
                        "limit": validation.limit,
                        "warnings": validation.warnings,
                    },
                    "available_columns": _available_columns(
                        validation.tables,
                        column_metadata,
                    ),
                    "sql_dialect": MYSQL_SQL_DIALECT_GUIDANCE,
                    "aggregate_hints": COMMON_AGGREGATE_HINTS,
                    "schema": schema,
                    "warning": (
                        "This SQL was not executed. Use execute_operational_sql "
                        "next with this validated SQL if it is needed to answer "
                        "the user. Do not draft another SQL variant unless this "
                        "validation result shows a specific schema problem."
                    ),
                }
            )
        except OperationalSqlValidationError as exc:
            # Validation failures are expected while the model learns a schema.
            # Return them as data so the model can correct the draft.
            logger.info("Operational SQL draft validation failed: %s", exc)
            try:
                column_metadata = client.load_column_metadata()
                validator = OperationalSqlValidator(column_metadata)
                table_names = validator.referenced_tables(sql)
                available_columns = _available_columns(table_names, column_metadata)
            except DatabaseSchemaClientError:
                available_columns = {}
            return _json_result(
                {
                    "ok": False,
                    "executed": False,
                    "site_id": site_id,
                    "question": question,
                    "sql": sql,
                    "validation": {
                        "valid": False,
                        "error": str(exc),
                    },
                    "available_columns": available_columns,
                    "sql_dialect": MYSQL_SQL_DIALECT_GUIDANCE,
                    "aggregate_hints": COMMON_AGGREGATE_HINTS,
                }
            )
        except DatabaseSchemaClientError as exc:
            # Return a structured tool error so the assistant can explain that
            # schema context is unavailable instead of failing the chat request.
            logger.warning("Database schema lookup failed: %s", exc)
            return _json_result({"ok": False, "executed": False, "error": str(exc)})

    def execute_operational_sql(
        question: str,
        sql: str,
        notes: str | None = None,
    ) -> str:
        """Run a validated SELECT query and return bounded rows to the model."""
        logger.info("Executing operational SQL site_id=%s", site_id)
        try:
            validation, column_metadata = _validate_sql(sql)
            rows = client.execute_readonly_sql(
                validation.sql,
                site_id=site_id,
                max_rows=max_rows,
            )
            # The successful result includes data rows because this tool is used
            # by the model to answer the user. API clients still do not receive
            # tool payloads directly; the public `/chat` response only returns
            # the final assistant answer.
            return _json_result(
                {
                    "ok": True,
                    "executed": True,
                    "site_id": site_id,
                    "question": question,
                    "sql": validation.sql,
                    "notes": notes,
                    "row_count": len(rows),
                    "max_rows": max_rows,
                    "rows": rows,
                    "available_columns": _available_columns(
                        validation.tables,
                        column_metadata,
                    ),
                    "sql_dialect": MYSQL_SQL_DIALECT_GUIDANCE,
                    "aggregate_hints": COMMON_AGGREGATE_HINTS,
                    "validation": {
                        "valid": True,
                        "tables": validation.tables,
                        "aliases": validation.aliases,
                        "referenced_columns": validation.referenced_columns,
                        "limit": validation.limit,
                        "warnings": validation.warnings,
                    },
                }
            )
        except OperationalSqlValidationError as exc:
            logger.info("Operational SQL execution validation failed: %s", exc)
            try:
                column_metadata = client.load_column_metadata()
                validator = OperationalSqlValidator(column_metadata)
                table_names = validator.referenced_tables(sql)
                available_columns = _available_columns(table_names, column_metadata)
            except DatabaseSchemaClientError:
                available_columns = {}
            return _json_result(
                {
                    "ok": False,
                    "executed": False,
                    "site_id": site_id,
                    "question": question,
                    "sql": sql,
                    "validation": {
                        "valid": False,
                        "error": str(exc),
                    },
                    "available_columns": available_columns,
                    "sql_dialect": MYSQL_SQL_DIALECT_GUIDANCE,
                    "aggregate_hints": COMMON_AGGREGATE_HINTS,
                }
            )
        except (DatabaseSchemaClientError, OperationalSqlExecutionError):
            # Do not leak SQLAlchemy, driver, host, username, or schema details
            # into the model context. Full details are kept in application logs.
            logger.exception("Operational SQL execution failed")
            return _json_result(
                {
                    "ok": False,
                    "executed": False,
                    "site_id": site_id,
                    "question": question,
                    "sql": sql,
                    "error": "Operational SQL query failed.",
                }
            )

    return [
        StructuredTool.from_function(
            func=draft_operational_sql,
            name="draft_operational_sql",
            description=(
                "Validate a read-only SQL query and return curated Ometrics schema "
                "context without executing it. Use this when you need schema feedback "
                "before running execute_operational_sql. If the draft result is valid "
                "and the query answers the user, run execute_operational_sql next with "
                "the same SQL instead of drafting alternate valid queries."
            ),
            args_schema=OperationalSqlInput,
        ),
        StructuredTool.from_function(
            func=execute_operational_sql,
            name="execute_operational_sql",
            description=(
                "Execute a validated read-only SELECT query against curated Ometrics "
                "operational tables. Use it after more specific report, reading, "
                "shutdown, timeline, work-order, and capability tools when a direct "
                "database query is the best way to answer. Queries must use `:site_id` "
                "for selected-site scoping and a numeric LIMIT no greater than the "
                f"configured maximum of {max_rows} rows."
            ),
            args_schema=OperationalSqlInput,
        )
    ]

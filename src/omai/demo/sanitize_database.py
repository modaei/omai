"""Build and load a safe, synthetic Ometrics database for the public demo.

This module is deliberately a developer-only utility. It imports a raw backup
into a temporary staging database on the configured MariaDB server, sanitizes
that copy, and streams the verified result into the demo database. It must never
point at a production database or be run by the public API.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import pymysql


# Tables that contain credentials, internal activity, queues, or other data that
# has no value in a public operational demo.  Their schema remains in the dump,
# but all rows are removed.
FORBIDDEN_TABLES = {
    "activity_log",
    "ai_conversations",
    "ai_daily_usage",
    "ai_messages",
    "ai_rag_index_events",
    "ai_reported_messages",
    "alarm_acknowledge_tokens",
    "cache",
    "cache_locks",
    "contacts",
    "contact_site",
    "failed_jobs",
    "jobs",
    "job_batches",
    "monitoring_command_results",
    "on_call_contacts",
    "password_reset_tokens",
    "personal_access_tokens",
    "roles",
    "role_user",
    "settings",
    "site_user",
    "users",
    "well_documents",
}

# The public demo deliberately contains only the operational model Omai and
# Omreports need.  Unknown/new application tables are emptied by default rather
# than accidentally becoming a path for unsanitized production data.
RETAINED_TABLES = {
    "migrations",
    "sites",
    "batteries",
    "wells",
    "onrr_codes",
    "lacts",
    "tanks",
    "treaters",
    "pumps",
    "water_plants",
    "flares",
    "flow_meters",
    "knock_outs",
    "data_points",
    "data_point_data",
    "lact_readings",
    "flare_readings",
    "flow_meter_readings",
    "knock_out_readings",
    "linear_tank_readings",
    "mixed_tank_readings",
    "non_linear_tank_readings",
    "non_linear_tank_volume_mappings",
    "non_linear_tank_volume_mapping_details",
    "pump_readings",
    "treater_readings",
    "water_plant_readings",
    "water_draws",
    "run_tickets",
    "well_fluids",
    "well_injections",
    "well_shutdowns",
    "well_tests",
    "well_histories",
    "general_notes",
    "chart_notes",
    "work_orders",
    "work_order_notes",
    "alarm_events",
    "alarm_logs",
    "rod_pump_cards",
    "rod_pump_card_archive",
    "rod_pump_monitoring_data",
    "rod_pump_monitoring_data_cards",
    "rod_pump_notes",
    "report_configurations",
    "silent_profiles",
}

# A table may have several timestamps, but only its operational event timestamp
# controls the 12-month retention window.  Creation/update timestamps are moved
# later with all other date columns after the retained rows have been selected.
RETENTION_DATES = {
    "general_notes": ("date", "created_at"),
    "chart_notes": ("x_axis_value", "created_at"),
    "work_orders": ("date", "created_at"),
    "work_order_notes": ("created_at",),
    "well_shutdowns": ("start", "shutdown_start", "created_at"),
    "well_tests": ("time", "created_at"),
    "well_fluids": ("time", "created_at"),
    "well_injections": ("time", "created_at"),
    "well_histories": ("changed_at", "created_at"),
    "lact_readings": ("time", "created_at"),
    "flare_readings": ("time", "created_at"),
    "flow_meter_readings": ("time", "created_at"),
    "knock_out_readings": ("time", "created_at"),
    "linear_tank_readings": ("time", "created_at"),
    "mixed_tank_readings": ("time", "created_at"),
    "non_linear_tank_readings": ("time", "created_at"),
    "pump_readings": ("time", "created_at"),
    "treater_readings": ("time", "created_at"),
    "water_plant_readings": ("time", "created_at"),
    "water_draws": ("time", "created_at"),
    "run_tickets": ("time", "created_at"),
    "rod_pump_notes": ("updated_at", "created_at"),
}

UNIX_TIMESTAMP_COLUMNS = {
    "alarm_events": ("unix_timestamp",),
    "alarm_logs": ("unix_timestamp",),
    "rod_pump_cards": ("pull_timestamp", "timestamp"),
    "rod_pump_card_archive": ("pull_timestamp", "timestamp"),
    "rod_pump_monitoring_data": ("timestamp",),
    "rod_pump_monitoring_data_cards": ("timestamp",),
}

ENTITY_TABLES = {
    "sites": "Site",
    "batteries": "Battery",
    "wells": "Well",
    "lacts": "LACT",
    "tanks": "Tank",
    "treaters": "Treater",
    "pumps": "Pump",
    "water_plants": "Water Plant",
    "flares": "Flare",
    "flow_meters": "Flow Meter",
    "knock_outs": "Knockout",
}

LOCATION_COLUMNS = {
    "sites": ("location",),
    "batteries": ("location",),
    "wells": ("location",),
    "lacts": ("location",),
    "flares": ("location",),
    "knock_outs": ("location",),
    "tanks": ("location",),
}

STRUCTURAL_STRING_COLUMNS = {
    "id", "uuid", "key", "tag", "tags", "type", "contents", "status",
    "active", "event", "object_type", "entity_type", "pump_type", "code",
    "downtime_code", "data_point_name", "data_point_type", "chart_name",
    "property", "unit", "uom", "color", "role", "guard_name", "name",
    "facility_name", "device_name", "table_name", "connection", "driver",
}

TEXT_COLUMN_MARKERS = (
    "comment", "note", "description", "message", "subject", "body", "reason",
    "detail", "summary", "old_value", "new_value", "value", "title", "object_name",
    "well_name", "operator", "address", "email", "phone", "filename", "file_path",
    "url", "number",
)


class SanitizationError(RuntimeError):
    """Raised when a dump cannot be sanitized or safely loaded."""


@dataclass(frozen=True)
class SanitizationOptions:
    """All explicit, non-production inputs needed for one sanitization run."""

    input_path: Path
    site_id: int
    seed: bytes
    staging_database: str
    database_host: str
    database_port: int
    database_user: str
    database_password: str
    target_database: str
    rollback_directory: Path
    keep_days: int = 365
    dry_run: bool = False


def main(argv: list[str] | None = None) -> None:
    """Sanitize a backup and replace the configured demo database."""
    options = _options_from_args(argv)
    sanitizer = DemoDumpSanitizer(options)
    manifest = sanitizer.run()
    print(json.dumps(manifest, indent=2, sort_keys=True, default=str))


class DemoDumpSanitizer:
    """Coordinate staging import, sanitization, and direct demo replacement."""

    def __init__(self, options: SanitizationOptions):
        self.options = options

    def run(self) -> dict[str, Any]:
        """Run the complete process and always drop the temporary staging database."""
        self._validate_options()
        self._validate_dump_format()
        self._create_staging_database()
        try:
            self._import_source_dump()
            with closing(self._connection(self.options.staging_database)) as connection:
                sanitizer = DatabaseSanitizer(
                    connection=connection,
                    site_id=self.options.site_id,
                    seed=self.options.seed,
                    keep_days=self.options.keep_days,
                    target_date=date.today(),
                )
                manifest = sanitizer.sanitize_and_validate()
            manifest.update(
                source_sha256=_file_sha256(self.options.input_path),
                sanitized_on=date.today().isoformat(),
                target_database=self.options.target_database,
            )
            if not self.options.dry_run:
                rollback = self._replace_demo_database()
                if rollback is not None:
                    manifest["rollback_dump"] = str(rollback)
            return manifest
        finally:
            self._drop_staging_database()

    def _validate_options(self) -> None:
        if not self.options.input_path.is_file():
            raise SanitizationError(f"Input dump does not exist: {self.options.input_path}")
        if self.options.site_id <= 0 or self.options.keep_days <= 0:
            raise SanitizationError("--site-id and --keep-days must be positive.")
        if self.options.database_port <= 0:
            raise SanitizationError("--database-port must be positive.")
        if not self.options.database_host or not self.options.database_user:
            raise SanitizationError("--database-host and --database-user are required.")
        if not self.options.staging_database.startswith("omai_demo_staging"):
            raise SanitizationError("Staging database must start with 'omai_demo_staging'.")
        if self.options.staging_database == self.options.target_database:
            raise SanitizationError("Staging and target databases must be different.")
        for value, label in (
            (self.options.staging_database, "staging database"),
            (self.options.target_database, "target database"),
        ):
            if not _safe_identifier(value):
                raise SanitizationError(f"Invalid {label} identifier: {value!r}")
        _require_command("mariadb")
        _require_command("mariadb-dump")

    def _validate_dump_format(self) -> None:
        """Reject database-selecting dumps before they can affect another schema."""
        forbidden = re.compile(rb"^\s*(?:CREATE|DROP)\s+DATABASE\b|^\s*USE\s+`?", re.IGNORECASE | re.MULTILINE)
        with _open_dump(self.options.input_path) as source:
            for line in source:
                if forbidden.match(line):
                    raise SanitizationError(
                        "Source dump must be a single-database dump without --databases, CREATE DATABASE, DROP DATABASE, or USE statements."
                    )

    def _create_staging_database(self) -> None:
        self._run_sql(f"DROP DATABASE IF EXISTS `{self.options.staging_database}`")
        self._run_sql(
            f"CREATE DATABASE `{self.options.staging_database}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )

    def _drop_staging_database(self) -> None:
        try:
            self._run_sql(f"DROP DATABASE IF EXISTS `{self.options.staging_database}`")
        except SanitizationError:
            # Do not hide the original import/sanitization error because staging
            # cleanup can be retried manually by an administrator.
            pass

    def _import_source_dump(self) -> None:
        with _open_dump(self.options.input_path) as source:
            process = subprocess.run(
                self._mariadb_command("--binary-mode=1", self.options.staging_database),
                stdin=source,
                stderr=subprocess.PIPE,
                env=self._client_environment(),
            )
        if process.returncode:
            raise SanitizationError(
                "Could not import source dump: " + process.stderr.decode("utf-8", errors="replace").strip()
            )

    def _connection(self, database: str) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self.options.database_host,
            port=self.options.database_port,
            user=self.options.database_user,
            password=self.options.database_password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
        )

    def _replace_demo_database(self) -> Path | None:
        """Back up, replace, and restore the direct database target on failure."""
        self.options.rollback_directory.mkdir(parents=True, exist_ok=True)
        rollback = self.options.rollback_directory / (
            f"{self.options.target_database}-{datetime.now():%Y%m%d-%H%M%S}.sql.gz"
        )
        has_existing_target = self._database_exists(self.options.target_database)
        if has_existing_target:
            self._backup_database(self.options.target_database, rollback)
        try:
            self._run_sql(f"DROP DATABASE IF EXISTS `{self.options.target_database}`")
            self._run_sql(
                f"CREATE DATABASE `{self.options.target_database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            self._stream_database(self.options.staging_database, self.options.target_database)
        except Exception as exc:
            if has_existing_target:
                self._restore_database(rollback)
                raise SanitizationError("Demo import failed; the previous demo database was restored.") from exc
            self._run_sql(f"DROP DATABASE IF EXISTS `{self.options.target_database}`")
            raise SanitizationError("Initial demo import failed; no database was retained.") from exc
        return rollback if has_existing_target else None

    def _database_exists(self, database: str) -> bool:
        result = _run(
            self._mariadb_command("-N", "-e", f"SHOW DATABASES LIKE '{database}'"),
            env=self._client_environment(),
        )
        return bool(result.stdout.strip())

    def _backup_database(self, database: str, output_path: Path) -> None:
        with gzip.open(output_path, "wb") as output:
            process = subprocess.run(
                self._mariadb_dump_command(database),
                stdout=output,
                stderr=subprocess.PIPE,
                env=self._client_environment(),
            )
        if process.returncode:
            output_path.unlink(missing_ok=True)
            raise SanitizationError(
                "Could not create demo database rollback: "
                + process.stderr.decode("utf-8", errors="replace").strip()
            )

    def _stream_database(self, source_database: str, target_database: str) -> None:
        dump = subprocess.Popen(
            self._mariadb_dump_command(source_database),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._client_environment(),
        )
        target = subprocess.run(
            self._mariadb_command("--binary-mode=1", target_database),
            stdin=dump.stdout,
            stderr=subprocess.PIPE,
            env=self._client_environment(),
        )
        assert dump.stdout is not None
        dump.stdout.close()
        dump_stderr = dump.communicate()[1]
        if dump.returncode or target.returncode:
            raise SanitizationError(
                "Could not load sanitized demo database: "
                + (target.stderr or dump_stderr).decode("utf-8", errors="replace").strip()
            )

    def _restore_database(self, rollback: Path) -> None:
        self._run_sql(f"DROP DATABASE IF EXISTS `{self.options.target_database}`")
        self._run_sql(
            f"CREATE DATABASE `{self.options.target_database}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        with gzip.open(rollback, "rb") as source:
            process = subprocess.run(
                self._mariadb_command("--binary-mode=1", self.options.target_database),
                stdin=source,
                stderr=subprocess.PIPE,
                env=self._client_environment(),
            )
        if process.returncode:
            raise SanitizationError(process.stderr.decode("utf-8", errors="replace"))

    def _run_sql(self, statement: str) -> None:
        _run(self._mariadb_command("-e", statement), env=self._client_environment())

    def _mariadb_command(self, *args: str) -> list[str]:
        return [
            "mariadb", "--host", self.options.database_host, "--port", str(self.options.database_port),
            "--user", self.options.database_user, *args,
        ]

    def _mariadb_dump_command(self, database: str) -> list[str]:
        return [
            "mariadb-dump", "--host", self.options.database_host, "--port", str(self.options.database_port),
            "--user", self.options.database_user, "--single-transaction", "--routines", "--events",
            "--no-tablespaces", database,
        ]

    def _client_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["MYSQL_PWD"] = self.options.database_password
        return environment


class DatabaseSanitizer:
    """Apply deterministic, schema-aware transformations to the temporary copy."""

    def __init__(
        self,
        *,
        connection: pymysql.connections.Connection,
        site_id: int,
        seed: bytes,
        keep_days: int,
        target_date: date,
    ):
        self.connection = connection
        self.site_id = site_id
        self.seed = seed
        self.keep_days = keep_days
        self.target_date = target_date
        self.columns = self._columns_by_table()
        self.date_shift_days = 0

    def sanitize_and_validate(self) -> dict[str, Any]:
        """Reduce the dataset, replace sensitive fields, then prove basic invariants."""
        self._require_site()
        with self.connection.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            self._clear_non_operational_tables(cursor)
            self._keep_selected_site(cursor)
            self._remove_orphans(cursor)
            self._retain_recent_events(cursor)
            self._remove_orphans(cursor)
            self.date_shift_days = self._calculate_date_shift(cursor)
            self._shift_dates(cursor)
            self._replace_entity_labels(cursor)
            self._sanitize_free_text_and_json(cursor)
            self._clear_locations(cursor)
            self._transform_measurements(cursor)
            self._synthesize_current_data_point_values(cursor)
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        self.connection.commit()
        return self._validate()

    def _columns_by_table(self) -> dict[str, dict[str, str]]:
        query = """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
        """
        with self.connection.cursor() as cursor:
            cursor.execute(query)
            rows = cursor.fetchall()
        result: dict[str, dict[str, str]] = {}
        for table_name, column_name, data_type in rows:
            result.setdefault(str(table_name), {})[str(column_name)] = str(data_type).lower()
        return result

    def _require_site(self) -> None:
        if "sites" not in self.columns:
            raise SanitizationError("Source database has no sites table.")
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM sites WHERE id = %s", (self.site_id,))
            if not cursor.fetchone():
                raise SanitizationError(f"Site {self.site_id} does not exist in the source dump.")

    def _clear_non_operational_tables(self, cursor: pymysql.cursors.Cursor) -> None:
        for table in self.columns:
            if table in FORBIDDEN_TABLES or table not in RETAINED_TABLES:
                cursor.execute(f"DELETE FROM `{table}`")

    def _keep_selected_site(self, cursor: pymysql.cursors.Cursor) -> None:
        for table, columns in self.columns.items():
            if "site_id" in columns:
                cursor.execute(f"DELETE FROM `{table}` WHERE site_id <> %s", (self.site_id,))
        cursor.execute("DELETE FROM sites WHERE id <> %s", (self.site_id,))

    def _remove_orphans(self, cursor: pymysql.cursors.Cursor) -> None:
        """Delete rows whose non-null foreign key now targets a removed row."""
        foreign_keys = self._foreign_keys()
        for _ in range(len(foreign_keys) + 1):
            deleted = 0
            for table, column, parent_table, parent_column in foreign_keys:
                if table not in self.columns or parent_table not in self.columns:
                    continue
                cursor.execute(
                    f"DELETE child FROM `{table}` child "
                    f"LEFT JOIN `{parent_table}` parent ON child.`{column}` = parent.`{parent_column}` "
                    f"WHERE child.`{column}` IS NOT NULL AND parent.`{parent_column}` IS NULL"
                )
                deleted += cursor.rowcount
            if not deleted:
                return

    def _foreign_keys(self) -> list[tuple[str, str, str, str]]:
        query = """
            SELECT table_name, column_name, referenced_table_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = DATABASE() AND referenced_table_name IS NOT NULL
        """
        with self.connection.cursor() as cursor:
            cursor.execute(query)
            return [tuple(map(str, row)) for row in cursor.fetchall()]

    def _retain_recent_events(self, cursor: pymysql.cursors.Cursor) -> None:
        cutoff = self.target_date - timedelta(days=self.keep_days - 1)
        for table, candidate_columns in RETENTION_DATES.items():
            column = self._first_existing_column(table, candidate_columns)
            if column:
                cursor.execute(f"DELETE FROM `{table}` WHERE DATE(`{column}`) < %s", (cutoff,))
        cutoff_epoch = int(datetime.combine(cutoff, datetime.min.time()).timestamp())
        for table, candidates in UNIX_TIMESTAMP_COLUMNS.items():
            for column in candidates:
                if column in self.columns.get(table, {}):
                    cursor.execute(f"DELETE FROM `{table}` WHERE `{column}` < %s", (cutoff_epoch,))
                    break

    def _calculate_date_shift(self, cursor: pymysql.cursors.Cursor) -> int:
        latest: date | None = None
        for table, candidates in RETENTION_DATES.items():
            column = self._first_existing_column(table, candidates)
            if not column:
                continue
            cursor.execute(f"SELECT MAX(DATE(`{column}`)) FROM `{table}`")
            value = cursor.fetchone()[0]
            if value and (latest is None or value > latest):
                latest = value
        for table, candidates in UNIX_TIMESTAMP_COLUMNS.items():
            column = self._first_existing_column(table, candidates)
            if not column:
                continue
            cursor.execute(f"SELECT MAX(DATE(FROM_UNIXTIME(`{column}`))) FROM `{table}`")
            value = cursor.fetchone()[0]
            if value and (latest is None or value > latest):
                latest = value
        if latest is None:
            return 0
        return (self.target_date - latest).days

    def _shift_dates(self, cursor: pymysql.cursors.Cursor) -> None:
        if not self.date_shift_days:
            return
        for table, columns in self.columns.items():
            for column, data_type in columns.items():
                if data_type in {"date", "datetime", "timestamp"}:
                    cursor.execute(
                        f"UPDATE `{table}` SET `{column}` = DATE_ADD(`{column}`, INTERVAL %s DAY) "
                        f"WHERE `{column}` IS NOT NULL",
                        (self.date_shift_days,),
                    )
        seconds = self.date_shift_days * 86400
        for table, candidates in UNIX_TIMESTAMP_COLUMNS.items():
            for column in candidates:
                if column in self.columns.get(table, {}):
                    cursor.execute(
                        f"UPDATE `{table}` SET `{column}` = `{column}` + %s WHERE `{column}` IS NOT NULL",
                        (seconds,),
                    )

    def _replace_entity_labels(self, cursor: pymysql.cursors.Cursor) -> None:
        aliases: dict[str, str] = {}
        for table, label in ENTITY_TABLES.items():
            if table not in self.columns or "id" not in self.columns[table]:
                continue
            name_column = "name" if "name" in self.columns[table] else None
            key_column = "key" if "key" in self.columns[table] else None
            if not name_column and not key_column:
                continue
            cursor.execute(
                f"SELECT id{', `name`' if name_column else ''}{', `key`' if key_column else ''} FROM `{table}`"
            )
            rows = cursor.fetchall()
            entity_aliases = self._unique_entity_aliases(
                label,
                [int(row[0]) for row in rows],
            )
            for row in rows:
                identifier = row[0]
                alias = entity_aliases[int(identifier)]
                old_values = row[1:]
                for old in old_values:
                    if old:
                        aliases[str(old)] = alias
                updates: list[str] = []
                params: list[Any] = []
                if name_column:
                    updates.append("`name` = %s")
                    params.append(alias)
                if key_column:
                    updates.append("`key` = %s")
                    params.append(_slug(alias))
                params.append(identifier)
                cursor.execute(f"UPDATE `{table}` SET {', '.join(updates)} WHERE id = %s", params)

        if "data_points" in self.columns:
            cursor.execute("SELECT id, facility_name, device_name, data_point_name FROM data_points")
            for identifier, facility, device, point_name in cursor.fetchall():
                safe_point_name = _safe_data_point_name(point_name, int(identifier))
                facility_alias = aliases.get(str(facility), self._entity_alias("Facility", int(identifier)))
                device_alias = aliases.get(str(device), facility_alias) if device else facility_alias
                tag = f"{_slug(device_alias)}_{_slug(safe_point_name)}_{identifier}"
                assignments = ["facility_name = %s", "device_name = %s", "data_point_name = %s"]
                params: list[Any] = [facility_alias, device_alias, safe_point_name]
                tag_column = "tag" if "tag" in self.columns["data_points"] else "tags" if "tags" in self.columns["data_points"] else None
                if tag_column:
                    assignments.append(f"`{tag_column}` = %s")
                    params.append(tag)
                params.append(identifier)
                cursor.execute(
                    f"UPDATE data_points SET {', '.join(assignments)} WHERE id = %s", params
                )

    def _sanitize_free_text_and_json(self, cursor: pymysql.cursors.Cursor) -> None:
        for table, columns in self.columns.items():
            if table not in RETAINED_TABLES:
                continue
            identifier = "id" if "id" in columns else None
            if not identifier:
                continue
            for column, data_type in columns.items():
                lowered = column.lower()
                if lowered in STRUCTURAL_STRING_COLUMNS:
                    continue
                if data_type == "json":
                    # Missing-reading configuration contains only entity IDs and
                    # remains useful after the selected-site reduction.  Other
                    # JSON fields can contain card arrays or raw telemetry, so
                    # preserve their shape while replacing every scalar value.
                    if table != "report_configurations":
                        self._sanitize_json_column(cursor, table, column, identifier)
                elif data_type in {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"} and any(
                    marker in lowered for marker in TEXT_COLUMN_MARKERS
                ):
                    cursor.execute(f"SELECT `{identifier}` FROM `{table}` WHERE `{column}` IS NOT NULL")
                    for row in cursor.fetchall():
                        record_id = int(row[0])
                        cursor.execute(
                            f"UPDATE `{table}` SET `{column}` = %s WHERE `{identifier}` = %s",
                            (self._synthetic_text(table, record_id), record_id),
                        )

    def _sanitize_json_column(
        self,
        cursor: pymysql.cursors.Cursor,
        table: str,
        column: str,
        identifier: str,
    ) -> None:
        """Keep JSON object/array layouts but remove source values recursively."""
        cursor.execute(
            f"SELECT `{identifier}`, `{column}` FROM `{table}` WHERE `{column}` IS NOT NULL"
        )
        for record_id, raw_payload in cursor.fetchall():
            try:
                payload = json.loads(raw_payload)
            except (TypeError, ValueError):
                payload = {}
            replacement = self._synthetic_json_value(payload, table, column, int(record_id))
            cursor.execute(
                f"UPDATE `{table}` SET `{column}` = %s WHERE `{identifier}` = %s",
                (json.dumps(replacement, separators=(",", ":")), record_id),
            )

    def _synthetic_json_value(
        self,
        value: Any,
        table: str,
        column: str,
        record_id: int,
        path: str = "",
    ) -> Any:
        """Replace JSON scalars deterministically while retaining API/card shapes."""
        if isinstance(value, dict):
            return {
                str(key): self._synthetic_json_value(
                    child, table, column, record_id, f"{path}.{key}"
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                self._synthetic_json_value(child, table, column, record_id, f"{path}[{index}]")
                for index, child in enumerate(value)
            ]
        if isinstance(value, bool) or value is None:
            return value
        if isinstance(value, (int, float)):
            factor = self._measurement_factor(f"{column}_{path}")
            return round(float(value) * factor, 6)
        return f"Synthetic demo {table.replace('_', ' ')} value"

    def _clear_locations(self, cursor: pymysql.cursors.Cursor) -> None:
        for table, candidates in LOCATION_COLUMNS.items():
            for column in candidates:
                if column in self.columns.get(table, {}):
                    cursor.execute(f"UPDATE `{table}` SET `{column}` = NULL")

    def _transform_measurements(self, cursor: pymysql.cursors.Cursor) -> None:
        """Scale measured values without touching IDs, flags, counters, or dates."""
        for table, columns in self.columns.items():
            if table not in RETAINED_TABLES:
                continue
            for column, data_type in columns.items():
                if data_type not in {"decimal", "double", "float", "int", "bigint", "mediumint", "smallint"}:
                    continue
                if not _is_measurement_column(column):
                    continue
                factor = self._measurement_factor(column)
                cursor.execute(
                    f"UPDATE `{table}` SET `{column}` = ROUND(`{column}` * %s, 6) "
                    f"WHERE `{column}` IS NOT NULL",
                    (factor,),
                )

    def _synthesize_current_data_point_values(self, cursor: pymysql.cursors.Cursor) -> None:
        if "data_point_data" not in self.columns or "data_points" not in self.columns:
            return
        columns = self.columns["data_point_data"]
        value_column = "data" if "data" in columns else "value" if "value" in columns else None
        if not value_column or "data_point_id" not in columns:
            return
        now_epoch = int(datetime.now().timestamp())
        cursor.execute("SELECT id, data_point_name FROM data_points")
        for point_id, point_name in cursor.fetchall():
            value = round(10 + self._fraction(f"current:{point_id}:{point_name}") * 90, 3)
            payload = json.dumps({"value": value, "timestamp": now_epoch, "type": "number"})
            cursor.execute(
                f"UPDATE data_point_data SET `{value_column}` = %s WHERE data_point_id = %s",
                (payload, point_id),
            )
        if "last_update" in columns:
            cursor.execute("UPDATE data_point_data SET last_update = %s", (now_epoch,))

    def _validate(self) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM sites")
            site_count = int(cursor.fetchone()[0])
            if site_count != 1:
                raise SanitizationError(f"Expected exactly one retained site, found {site_count}.")
            cursor.execute("SELECT id FROM sites")
            retained_site_id = int(cursor.fetchone()[0])
            if retained_site_id != self.site_id:
                raise SanitizationError("The retained site does not match --site-id.")
            forbidden_counts = {}
            for table in FORBIDDEN_TABLES.intersection(self.columns):
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                count = int(cursor.fetchone()[0])
                if count:
                    forbidden_counts[table] = count
            if forbidden_counts:
                raise SanitizationError(f"Forbidden tables contain rows: {forbidden_counts}")
            counts = {}
            for table in sorted(RETAINED_TABLES.intersection(self.columns)):
                cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                counts[table] = int(cursor.fetchone()[0])
        return {
            "site_id": self.site_id,
            "keep_days": self.keep_days,
            "date_shift_days": self.date_shift_days,
            "table_counts": counts,
        }

    def _first_existing_column(self, table: str, candidates: Iterable[str]) -> str | None:
        available = self.columns.get(table, {})
        return next((column for column in candidates if column in available), None)

    def _entity_alias(self, label: str, identifier: int) -> str:
        return f"{label} {100 + int(self._fraction(f'{label}:{identifier}') * 899)}"

    def _unique_entity_aliases(self, label: str, identifiers: list[int]) -> dict[int, str]:
        """Return stable, non-colliding aliases for every row in one entity table.

        Hash ordering keeps the output unrelated to source row IDs, while the
        sequential suffix guarantees unique values for columns such as
        ``wells.key`` that have a database unique constraint.
        """
        ordered_identifiers = sorted(
            set(identifiers),
            key=lambda identifier: (self._fraction(f"{label}:{identifier}"), identifier),
        )

        return {
            identifier: f"{label} {100 + position}"
            for position, identifier in enumerate(ordered_identifiers)
        }

    def _synthetic_text(self, table: str, identifier: int) -> str:
        terms = ("routine inspection", "chemical treatment", "paraffin review", "maintenance follow-up", "flow verification")
        term = terms[int(self._fraction(f"text:{table}:{identifier}") * len(terms)) % len(terms)]
        return f"Synthetic demo {table.replace('_', ' ')} record: {term}."

    def _measurement_factor(self, column: str) -> float:
        domain = _measurement_domain(column)
        return 0.8 + self._fraction(f"measurement:{domain}") * 0.4

    def _fraction(self, value: str) -> float:
        digest = hashlib.blake2b(value.encode("utf-8"), key=self.seed, digest_size=8).digest()
        return int.from_bytes(digest, "big") / float(2**64 - 1)


def _options_from_args(argv: list[str] | None) -> SanitizationOptions:
    parser = argparse.ArgumentParser(
        description="Sanitize an Ometrics dump and replace the isolated demo MariaDB database."
    )
    parser.add_argument("--input", type=Path, required=True, help="Private .sql or .sql.gz source dump.")
    parser.add_argument("--site-id", type=int, required=True)
    parser.add_argument("--seed-file", type=Path, required=True, help="Private deterministic sanitization seed.")
    parser.add_argument("--staging-database", default="omai_demo_staging")
    parser.add_argument("--database-host", default="127.0.0.1")
    parser.add_argument("--database-port", type=int, default=3306)
    parser.add_argument("--database-user", default="root")
    parser.add_argument("--target-database", default="ometrics_demo")
    parser.add_argument("--database-password-env", default="DEMO_MYSQL_ROOT_PASSWORD")
    parser.add_argument("--rollback-directory", type=Path, default=Path("data/demo-rollbacks"))
    parser.add_argument("--keep-days", type=int, default=365)
    parser.add_argument("--dry-run", action="store_true", help="Sanitize and validate without replacing the demo database.")
    args = parser.parse_args(argv)
    if not args.seed_file.is_file():
        raise SystemExit(f"Seed file does not exist: {args.seed_file}")
    password = os.getenv(args.database_password_env, "")
    if not password:
        raise SystemExit(f"Environment variable {args.database_password_env} is required.")
    return SanitizationOptions(
        input_path=args.input,
        site_id=args.site_id,
        seed=args.seed_file.read_bytes().strip(),
        staging_database=args.staging_database,
        database_host=args.database_host,
        database_port=args.database_port,
        database_user=args.database_user,
        database_password=password,
        target_database=args.target_database,
        rollback_directory=args.rollback_directory,
        keep_days=args.keep_days,
        dry_run=args.dry_run,
    )


def _open_dump(path: Path):
    return gzip.open(path, "rb") if path.suffix == ".gz" else path.open("rb")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True, env=env)
    if result.returncode:
        raise SanitizationError(result.stderr.strip() or "Command failed: " + " ".join(command))
    return result


def _require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise SanitizationError(f"Required command is not installed: {command}")


def _safe_identifier(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]+", value))


def _slug(value: Any) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value).lower())).strip("_") or "item"


def _safe_data_point_name(value: Any, identifier: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    return normalized if normalized else f"Measurement {identifier}"


def _measurement_domain(column: str) -> str:
    lowered = column.lower()
    for domain in ("oil", "water", "gas", "sale", "cost", "volume", "bbl", "rate", "pressure", "temperature", "load", "level", "stroke", "fillage", "current"):
        if domain in lowered:
            return domain
    return "measurement"


def _is_measurement_column(column: str) -> bool:
    lowered = column.lower()
    if lowered == "id" or lowered.endswith("_id") or "timestamp" in lowered:
        return False
    if lowered in {"active", "enabled", "hours", "duration", "sort_order", "priority", "level"}:
        return False
    markers = ("oil", "water", "gas", "sale", "cost", "volume", "bbl", "rate", "pressure", "temperature", "load", "stroke", "fillage", "current", "value", "foot", "height", "diameter")
    return any(marker in lowered for marker in markers)


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()

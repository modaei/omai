from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import bindparam, create_engine, inspect, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from omai.config.settings import Settings


class ReadingClientError(RuntimeError):
    """Raised when a reading query is invalid or cannot be completed."""


@dataclass(frozen=True)
class ReadingDefinition:
    label: str
    table: str
    base_table: str
    foreign_key: str
    base_key: str
    fields: tuple[str, ...]
    base_filter: str = ""
    supports_missing: bool = True


READING_DEFINITIONS: dict[str, ReadingDefinition] = {
    "lact": ReadingDefinition(
        "LACT readings",
        "lact_readings",
        "lacts",
        "lact_id",
        "id",
        ("reading", "temperature", "bs_w", "comments"),
    ),
    "flare": ReadingDefinition(
        "Flare readings",
        "flare_readings",
        "flares",
        "flare_id",
        "id",
        ("pressure", "volume", "comments"),
    ),
    "linear_tank": ReadingDefinition(
        "Linear tank readings",
        "linear_tank_readings",
        "tanks",
        "tank_id",
        "id",
        ("level", "feet", "inches", "percentage", "pressure", "temperature", "comments"),
        "AND b.type = 'linear-volume'",
    ),
    "mixed_tank": ReadingDefinition(
        "Mixed tank readings",
        "mixed_tank_readings",
        "tanks",
        "tank_id",
        "id",
        (
            "top_level_feet",
            "top_level_inches",
            "water_level_feet",
            "water_level_inches",
            "comments",
        ),
        "AND b.type = 'mixed-water-oil'",
    ),
    "non_linear_tank": ReadingDefinition(
        "Non-linear tank readings",
        "non_linear_tank_readings",
        "tanks",
        "tank_id",
        "id",
        ("initial_feet", "initial_inches", "final_feet", "final_inches", "comments"),
        "AND b.type = 'non-linear-volume'",
    ),
    "water_plant": ReadingDefinition(
        "Water plant readings",
        "water_plant_readings",
        "water_plants",
        "water_plant_id",
        "id",
        (
            "meter_reading",
            "flow_rate",
            "suction_pressure",
            "discharge_pressure",
            "comments",
        ),
    ),
    "flow_meter": ReadingDefinition(
        "Flow meter readings",
        "flow_meter_readings",
        "flow_meters",
        "flow_meter_id",
        "id",
        ("total", "flow", "odometer", "comments"),
    ),
    "well_test": ReadingDefinition(
        "Well tests",
        "well_tests",
        "wells",
        "well_id",
        "id",
        (
            "oil",
            "water",
            "gas",
            "pip",
            "m_temp",
            "amps",
            "tbgp",
            "csgp",
            "runtime",
            "comments",
        ),
        supports_missing=False,
    ),
    "well_fluid": ReadingDefinition(
        "Well fluid readings",
        "well_fluids",
        "wells",
        "well_id",
        "id",
        ("level", "comments"),
        supports_missing=False,
    ),
    "well_injection": ReadingDefinition(
        "Well injection readings",
        "well_injections",
        "wells",
        "well_id",
        "id",
        ("flow_rate", "total", "type", "tbg", "csg", "comments"),
        supports_missing=False,
    ),
    "run_ticket": ReadingDefinition(
        "Run tickets",
        "run_tickets",
        "tanks",
        "tank_id",
        "id",
        (
            "number",
            "measurement_method",
            "obs_grav",
            "obs_temp",
            "b_s_w",
            "corr_grav",
            "gov",
            "nsv",
            "initial_feet",
            "initial_inches",
            "initial_qtr",
            "final_feet",
            "final_inches",
            "final_qtr",
            "initial_volume",
            "final_volume",
            "initial_meter_reading",
            "final_meter_reading",
            "comments",
        ),
        supports_missing=False,
    ),
    "water_draw": ReadingDefinition(
        "Water draws",
        "water_draws",
        "tanks",
        "tank_id",
        "id",
        ("initial_feet", "initial_inches", "final_feet", "final_inches", "comments"),
        supports_missing=False,
    ),
    "treater": ReadingDefinition(
        "Treater readings",
        "treater_readings",
        "treaters",
        "treater_id",
        "id",
        ("oil_intake", "pressure", "temperature", "comments"),
    ),
    "knock_out": ReadingDefinition(
        "Knock-out readings",
        "knock_out_readings",
        "knock_outs",
        "knock_out_id",
        "id",
        ("inlet", "oil_off", "comments"),
    ),
    "pump": ReadingDefinition(
        "Pump readings",
        "pump_readings",
        "pumps",
        "pump_id",
        "id",
        ("suction_pressure", "discharge_pressure", "comments"),
    ),
}

MISSING_READINGS_REPORT_FUNCTION_NAME = "hartzog_daily_missing"


BASE_ENTITY_LABELS = {
    "flares": "Flare",
    "flow_meters": "Flow Meter",
    "knock_outs": "Knock Out",
    "lacts": "LACT",
    "pumps": "Pump",
    "tanks": "Tank",
    "treaters": "Treater",
    "water_plants": "Water Plant",
    "wells": "Well",
}

TANK_VOLUME_FIELDS = (
    "bbl_foot",
    "volume",
    "oil_volume",
    "water_volume",
    "total_volume",
    "volume_status",
)


def list_supported_reading_types() -> list[dict[str, Any]]:
    return [
        {
            "reading_type": "tank",
            "label": "All tank readings",
            "supports_missing": True,
        },
        *[
            {
                "reading_type": key,
                "label": definition.label,
                "supports_missing": definition.supports_missing,
            }
            for key, definition in READING_DEFINITIONS.items()
        ],
    ]


class ReadingClient:
    def __init__(self, engine: Engine, max_rows: int = 500):
        self.engine = engine
        self.max_rows = max_rows
        self._missing_reading_exclusion_cache: dict[int, dict[str, tuple[int, ...]]] = {}
        self._table_columns_cache: dict[str, set[str]] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> "ReadingClient":
        settings.validate_database()
        url = URL.create(
            drivername="mysql+pymysql",
            username=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
        engine = create_engine(
            url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
        )
        return cls(engine)

    def list_reading_types(self) -> list[dict[str, Any]]:
        return list_supported_reading_types()

    def get_readings_for_date(
        self, site_id: int, reading_type: str, reading_date: str
    ) -> dict[str, Any]:
        day = self._parse_date(reading_date)
        if reading_type == "tank":
            return self._get_all_tank_readings(site_id, day)
        definition = self._definition(reading_type)
        rows = self._readings_for_day(site_id, definition, day)
        return {
            "reading_type": reading_type,
            "label": definition.label,
            "site_id": site_id,
            "date": day.isoformat(),
            "count": len(rows),
            "readings": self._compact_reading_rows(definition, rows),
        }

    def compare_readings_between_dates(
        self,
        site_id: int,
        reading_type: str,
        first_date: str,
        second_date: str,
    ) -> dict[str, Any]:
        first_day = self._parse_date(first_date)
        second_day = self._parse_date(second_date)
        if reading_type == "tank":
            return {
                "reading_type": "tank",
                "label": "All tank readings",
                "site_id": site_id,
                "first_date": first_day.isoformat(),
                "second_date": second_day.isoformat(),
                "groups": [
                    self.compare_readings_between_dates(
                        site_id, tank_type, first_date, second_date
                    )
                    for tank_type in (
                        "linear_tank",
                        "mixed_tank",
                        "non_linear_tank",
                    )
                ],
            }
        definition = self._definition(reading_type)
        first_rows = self._readings_for_day(site_id, definition, first_day)
        second_rows = self._readings_for_day(site_id, definition, second_day)
        return {
            "reading_type": reading_type,
            "label": definition.label,
            "site_id": site_id,
            "first_date": first_day.isoformat(),
            "second_date": second_day.isoformat(),
            "first_count": len(first_rows),
            "second_count": len(second_rows),
            "comparison_rows": self._comparison_rows(
                reading_type, definition, first_rows, second_rows
            ),
            "entity_summary": self._entity_summary(first_rows, second_rows),
        }

    def missing_readings_for_date(
        self, site_id: int, reading_type: str, reading_date: str
    ) -> dict[str, Any]:
        day = self._parse_date(reading_date)
        if reading_type == "tank":
            groups = [
                self.missing_readings_for_date(site_id, tank_type, reading_date)
                for tank_type in ("linear_tank", "mixed_tank", "non_linear_tank")
            ]
            return {
                "reading_type": "tank",
                "label": "All tank readings",
                "site_id": site_id,
                "date": day.isoformat(),
                "missing_count": sum(group["missing_count"] for group in groups),
                "groups": groups,
            }
        definition = self._definition(reading_type)
        if not definition.supports_missing:
            raise ReadingClientError(
                f"Missing-reading checks are not supported for {reading_type}."
            )

        start, end = self._day_bounds(day)
        ignored_ids = self._missing_reading_ignored_ids(site_id, reading_type)
        query = text(
            f"""
            SELECT b.name AS entity_name
            FROM {definition.base_table} b
            LEFT JOIN {definition.table} r
                ON b.{definition.base_key} = r.{definition.foreign_key}
                AND r.time >= :start_time
                AND r.time < :end_time
            WHERE r.{definition.foreign_key} IS NULL
                AND b.site_id = :site_id
                AND COALESCE(b.disable_reading, 0) = 0
                {("AND b.id NOT IN :ignored_ids" if ignored_ids else "")}
                {definition.base_filter}
            ORDER BY b.name
            LIMIT :limit
            """
        )
        params: dict[str, Any] = {
            "site_id": site_id,
            "start_time": start,
            "end_time": end,
        }
        if ignored_ids:
            params["ignored_ids"] = list(ignored_ids)
            query = query.bindparams(bindparam("ignored_ids", expanding=True))
        rows = self._execute(query, **params)
        return {
            "reading_type": reading_type,
            "label": definition.label,
            "site_id": site_id,
            "date": day.isoformat(),
            "missing_count": len(rows),
            "missing_entities": self._with_entity_display(definition, rows),
        }

    def all_missing_readings_for_date(
        self, site_id: int, reading_date: str
    ) -> dict[str, Any]:
        day = self._parse_date(reading_date)
        groups = []
        skipped = []
        for reading_type, definition in READING_DEFINITIONS.items():
            if not definition.supports_missing:
                continue
            try:
                groups.append(
                    self.missing_readings_for_date(site_id, reading_type, reading_date)
                )
            except ReadingClientError as exc:
                skipped.append({"reading_type": reading_type, "error": str(exc)})
        groups = [group for group in groups if group["missing_count"] > 0]
        return {
            "reading_type": "all_supported_missing_readings",
            "label": "All supported missing readings",
            "site_id": site_id,
            "date": day.isoformat(),
            "missing_count": sum(group["missing_count"] for group in groups),
            "groups": groups,
            "skipped": skipped,
        }

    def all_missing_readings_for_range(
        self, site_id: int, start_date: str, end_date: str
    ) -> dict[str, Any]:
        start_day = self._parse_date(start_date)
        end_day = self._parse_date(end_date)
        if end_day < start_day:
            raise ReadingClientError("end_date must be on or after start_date.")
        if (end_day - start_day).days > 31:
            raise ReadingClientError(
                "Missing-reading range checks are limited to 32 days."
            )

        dates = []
        total_missing_count = 0
        current_day = start_day
        while current_day <= end_day:
            day_result = self.all_missing_readings_for_date(
                site_id, current_day.isoformat()
            )
            total_missing_count += int(day_result["missing_count"])
            dates.append(
                {
                    "date": current_day.isoformat(),
                    "missing_count": day_result["missing_count"],
                    "groups": day_result["groups"],
                    "skipped": day_result["skipped"],
                }
            )
            current_day += timedelta(days=1)

        return {
            "reading_type": "all_supported_missing_readings",
            "label": "All supported missing readings",
            "site_id": site_id,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "day_count": len(dates),
            "missing_count": total_missing_count,
            "dates": dates,
        }

    def search_well_tests(
        self,
        site_id: int,
        start_date: str,
        end_date: str,
        well_name: str | None = None,
        min_oil: float | None = None,
        max_oil: float | None = None,
        min_water: float | None = None,
        max_water: float | None = None,
        min_gas: float | None = None,
        max_gas: float | None = None,
        min_runtime: float | None = None,
        max_runtime: float | None = None,
    ) -> dict[str, Any]:
        start_day = self._parse_date(start_date)
        end_day = self._parse_date(end_date)
        if end_day < start_day:
            raise ReadingClientError("end_date must be on or after start_date.")

        start_time = datetime.combine(start_day, time.min)
        end_time = datetime.combine(end_day + timedelta(days=1), time.min)
        conditions = [
            "w.site_id = :site_id",
            "wt.time >= :start_time",
            "wt.time < :end_time",
        ]
        params: dict[str, Any] = {
            "site_id": site_id,
            "start_time": start_time,
            "end_time": end_time,
        }

        if well_name:
            conditions.append("w.name LIKE :well_name")
            params["well_name"] = f"%{well_name}%"

        numeric_filters = {
            "min_oil": ("wt.oil", ">=", min_oil),
            "max_oil": ("wt.oil", "<=", max_oil),
            "min_water": ("wt.water", ">=", min_water),
            "max_water": ("wt.water", "<=", max_water),
            "min_gas": ("wt.gas", ">=", min_gas),
            "max_gas": ("wt.gas", "<=", max_gas),
            "min_runtime": ("wt.runtime", ">=", min_runtime),
            "max_runtime": ("wt.runtime", "<=", max_runtime),
        }
        applied_filters: dict[str, Any] = {}
        for name, (column, operator, value) in numeric_filters.items():
            if value is None:
                continue
            conditions.append(f"{column} {operator} :{name}")
            params[name] = value
            applied_filters[name] = value

        where_sql = "\n                AND ".join(conditions)
        query = text(
            f"""
            SELECT wt.time,
                w.name AS entity_name,
                wt.oil,
                wt.water,
                wt.gas,
                wt.pip,
                wt.m_temp,
                wt.amps,
                wt.tbgp,
                wt.csgp,
                wt.runtime,
                wt.comments
            FROM well_tests wt
            JOIN wells w
                ON wt.well_id = w.id
            WHERE {where_sql}
            ORDER BY wt.time, w.name
            LIMIT :limit
            """
        )
        rows = self._execute(query, **params)
        definition = self._definition("well_test")
        return {
            "reading_type": "well_test",
            "label": "Well tests",
            "site_id": site_id,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "filters": {
                **({"well_name": well_name} if well_name else {}),
                **applied_filters,
            },
            "count": len(rows),
            "well_tests": self._compact_range_reading_rows(
                definition, self._with_entity_display(definition, rows)
            ),
        }

    def search_readings(
        self,
        site_id: int,
        reading_type: str,
        start_date: str,
        end_date: str,
        entity_name: str | None = None,
        filters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        start_day = self._parse_date(start_date)
        end_day = self._parse_date(end_date)
        if end_day < start_day:
            raise ReadingClientError("end_date must be on or after start_date.")
        if reading_type == "tank":
            groups = [
                self.search_readings(
                    site_id, tank_type, start_date, end_date, entity_name, filters
                )
                for tank_type in ("linear_tank", "mixed_tank", "non_linear_tank")
            ]
            return {
                "reading_type": "tank",
                "label": "All tank readings",
                "site_id": site_id,
                "start_date": start_day.isoformat(),
                "end_date": end_day.isoformat(),
                "count": sum(group["count"] for group in groups),
                "groups": groups,
            }

        definition = self._definition(reading_type)
        start_time = datetime.combine(start_day, time.min)
        end_time = datetime.combine(end_day + timedelta(days=1), time.min)
        conditions = [
            "b.site_id = :site_id",
            "r.time >= :start_time",
            "r.time < :end_time",
        ]
        if definition.base_filter:
            conditions.append(definition.base_filter.removeprefix("AND "))

        params: dict[str, Any] = {
            "site_id": site_id,
            "start_time": start_time,
            "end_time": end_time,
        }
        if entity_name:
            conditions.append("b.name LIKE :entity_name")
            params["entity_name"] = f"%{entity_name}%"

        applied_filters = self._filter_conditions(definition, filters or [], conditions, params)
        where_sql = "\n                AND ".join(conditions)
        field_sql = _reading_select_fields(definition)
        query = text(
            f"""
            SELECT r.time,
                b.name AS entity_name,
                {field_sql}
            FROM {definition.table} r
            JOIN {definition.base_table} b
                ON r.{definition.foreign_key} = b.{definition.base_key}
            WHERE {where_sql}
            ORDER BY r.time, b.name
            LIMIT :limit
            """
        )
        rows = self._execute(query, **params)
        rows = self._with_tank_volumes(reading_type, rows)
        return {
            "reading_type": reading_type,
            "label": definition.label,
            "site_id": site_id,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "filters": {
                **({"entity_name": entity_name} if entity_name else {}),
                **({"field_filters": applied_filters} if applied_filters else {}),
            },
            "count": len(rows),
            "readings": self._compact_range_reading_rows(
                definition, self._with_entity_display(definition, rows)
            ),
        }

    def resolve_reading_entity(
        self,
        site_id: int,
        entity_name: str,
        reading_date: str | None = None,
        reading_type: str | None = None,
    ) -> dict[str, Any]:
        """Find reading-capable entities matching a user-supplied object name.

        This method exists because an object name alone is not always enough to
        select the correct reading table. For example, a phrase such as
        "Battery 2 Vent" may be guessed as a flare by the model even when the
        actual reading is stored on a flow meter. The resolver searches the
        configured reading base tables first and reports ambiguity explicitly.
        """
        normalized_name = entity_name.strip()
        if not normalized_name:
            raise ReadingClientError("entity_name is required.")

        day = self._parse_date(reading_date) if reading_date else None
        reading_types = self._resolution_reading_types(reading_type)
        candidates: list[dict[str, Any]] = []
        for candidate_reading_type in reading_types:
            definition = self._definition(candidate_reading_type)
            for row in self._matching_entities(
                site_id, definition, normalized_name
            ):
                candidate = self._entity_candidate(
                    candidate_reading_type, definition, row
                )
                if day is not None:
                    candidate["has_reading"] = self._entity_has_reading(
                        site_id, definition, int(row["entity_id"]), day
                    )
                candidates.append(candidate)

        candidates = self._deduplicate_candidates(candidates)
        candidates.sort(
            key=lambda candidate: (
                candidate["match_rank"],
                candidate["reading_type"],
                candidate["entity_display_name"],
            )
        )
        return {
            "entity_name": normalized_name,
            "reading_type": reading_type,
            "site_id": site_id,
            "date": day.isoformat() if day else None,
            "count": len(candidates),
            "candidates": candidates[: self.max_rows],
        }

    def get_reading_for_entity(
        self,
        site_id: int,
        entity_name: str,
        reading_date: str,
        reading_type: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an entity name, then return that entity's reading for one day."""
        day = self._parse_date(reading_date)
        resolution = self.resolve_reading_entity(
            site_id, entity_name, day.isoformat(), reading_type
        )
        candidates = resolution["candidates"]
        if len(candidates) != 1:
            return {
                "reading_type": reading_type,
                "label": "Resolved entity reading",
                "site_id": site_id,
                "date": day.isoformat(),
                "entity_name": entity_name,
                "needs_clarification": len(candidates) > 1,
                "count": 0,
                "candidates": [
                    {
                        "reading_type": candidate["reading_type"],
                        "entity_type": candidate["entity_type"],
                        "entity_display_name": candidate["entity_display_name"],
                        **(
                            {"has_reading": candidate["has_reading"]}
                            if "has_reading" in candidate
                            else {}
                        ),
                    }
                    for candidate in candidates
                ],
                "readings": [],
            }

        candidate = candidates[0]
        definition = self._definition(candidate["reading_type"])
        rows = self._readings_for_day_entity(
            site_id, definition, int(candidate["entity_id"]), day
        )
        return {
            "reading_type": candidate["reading_type"],
            "label": definition.label,
            "site_id": site_id,
            "date": day.isoformat(),
            "entity": {
                "reading_type": candidate["reading_type"],
                "entity_type": candidate["entity_type"],
                "entity_display_name": candidate["entity_display_name"],
            },
            "count": len(rows),
            "readings": self._compact_reading_rows(definition, rows),
        }

    def _readings_for_day(
        self, site_id: int, definition: ReadingDefinition, day: date
    ) -> list[dict[str, Any]]:
        start, end = self._day_bounds(day)
        field_sql = _reading_select_fields(definition)
        query = text(
            f"""
            SELECT r.id AS reading_id,
                r.time,
                b.name AS entity_name,
                {field_sql}
            FROM {definition.table} r
            JOIN {definition.base_table} b
                ON r.{definition.foreign_key} = b.{definition.base_key}
            WHERE b.site_id = :site_id
                AND r.time >= :start_time
                AND r.time < :end_time
                {definition.base_filter}
            ORDER BY r.time, b.name
            LIMIT :limit
            """
        )
        rows = self._execute(query, site_id=site_id, start_time=start, end_time=end)
        return self._with_entity_display(
            definition,
            self._with_tank_volumes(_reading_type_for_definition(definition), rows),
        )

    def _readings_for_day_entity(
        self,
        site_id: int,
        definition: ReadingDefinition,
        entity_id: int,
        day: date,
    ) -> list[dict[str, Any]]:
        start, end = self._day_bounds(day)
        field_sql = _reading_select_fields(definition)
        query = text(
            f"""
            SELECT r.id AS reading_id,
                r.time,
                b.name AS entity_name,
                {field_sql}
            FROM {definition.table} r
            JOIN {definition.base_table} b
                ON r.{definition.foreign_key} = b.{definition.base_key}
            WHERE b.site_id = :site_id
                AND b.{definition.base_key} = :entity_id
                AND r.time >= :start_time
                AND r.time < :end_time
                {definition.base_filter}
            ORDER BY r.time, b.name
            LIMIT :limit
            """
        )
        rows = self._execute(
            query,
            site_id=site_id,
            entity_id=entity_id,
            start_time=start,
            end_time=end,
        )
        return self._with_entity_display(
            definition,
            self._with_tank_volumes(_reading_type_for_definition(definition), rows),
        )

    def _matching_entities(
        self, site_id: int, definition: ReadingDefinition, entity_name: str
    ) -> list[dict[str, Any]]:
        has_key = self._table_has_column(definition.base_table, "key")
        key_select = ", b.`key` AS entity_key" if has_key else ""
        key_condition = " OR LOWER(b.`key`) LIKE LOWER(:entity_name)" if has_key else ""
        query = text(
            f"""
            SELECT b.{definition.base_key} AS entity_id,
                b.name AS entity_name
                {key_select}
            FROM {definition.base_table} b
            WHERE b.site_id = :site_id
                AND (
                    LOWER(b.name) LIKE LOWER(:entity_name)
                    {key_condition}
                )
                {definition.base_filter}
            ORDER BY b.name
            LIMIT :limit
            """
        )
        rows = self._execute(
            query,
            site_id=site_id,
            entity_name=f"%{entity_name}%",
        )
        normalized_query = _normalize_entity_match_value(entity_name)
        for row in rows:
            row["match_rank"] = _entity_match_rank(row, normalized_query)
        return rows

    def _entity_has_reading(
        self, site_id: int, definition: ReadingDefinition, entity_id: int, day: date
    ) -> bool:
        start, end = self._day_bounds(day)
        query = text(
            f"""
            SELECT 1
            FROM {definition.table} r
            JOIN {definition.base_table} b
                ON r.{definition.foreign_key} = b.{definition.base_key}
            WHERE b.site_id = :site_id
                AND b.{definition.base_key} = :entity_id
                AND r.time >= :start_time
                AND r.time < :end_time
                {definition.base_filter}
            LIMIT 1
            """
        )
        return bool(
            self._execute(
                query,
                site_id=site_id,
                entity_id=entity_id,
                start_time=start,
                end_time=end,
            )
        )

    @staticmethod
    def _entity_candidate(
        reading_type: str, definition: ReadingDefinition, row: dict[str, Any]
    ) -> dict[str, Any]:
        entity_type = BASE_ENTITY_LABELS.get(definition.base_table, "Entity")
        return {
            "reading_type": reading_type,
            "entity_id": row["entity_id"],
            "entity_type": entity_type,
            "entity_name": row["entity_name"],
            "entity_display_name": f"{entity_type} - {row['entity_name']}",
            "match_rank": row["match_rank"],
        }

    @staticmethod
    def _deduplicate_candidates(
        candidates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for candidate in candidates:
            key = (
                candidate["reading_type"],
                candidate["entity_type"],
                str(candidate["entity_id"]),
            )
            existing = deduplicated.get(key)
            if existing is None or candidate["match_rank"] < existing["match_rank"]:
                deduplicated[key] = candidate
        return list(deduplicated.values())

    def _resolution_reading_types(self, reading_type: str | None) -> list[str]:
        if reading_type is None:
            return list(READING_DEFINITIONS)
        if reading_type == "tank":
            return ["linear_tank", "mixed_tank", "non_linear_tank"]
        self._definition(reading_type)
        return [reading_type]

    def _table_has_column(self, table_name: str, column_name: str) -> bool:
        columns = self._table_columns_cache.get(table_name)
        if columns is None:
            try:
                columns = {
                    str(column["name"])
                    for column in inspect(self.engine).get_columns(table_name)
                }
            except SQLAlchemyError:
                columns = set()
            self._table_columns_cache[table_name] = columns
        return column_name in columns

    def _get_all_tank_readings(self, site_id: int, day: date) -> dict[str, Any]:
        groups = [
            self.get_readings_for_date(site_id, tank_type, day.isoformat())
            for tank_type in ("linear_tank", "mixed_tank", "non_linear_tank")
        ]
        return {
            "reading_type": "tank",
            "label": "All tank readings",
            "site_id": site_id,
            "date": day.isoformat(),
            "count": sum(group["count"] for group in groups),
            "groups": groups,
        }

    def _execute(self, query, **params: Any) -> list[dict[str, Any]]:
        params.setdefault("limit", self.max_rows)
        try:
            with self.engine.connect() as connection:
                result = connection.execute(query, params)
                return [self._serialize_row(dict(row._mapping)) for row in result]
        except SQLAlchemyError as exc:
            raise ReadingClientError(f"Database query failed: {exc}") from exc

    @staticmethod
    def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
        serialized = {}
        for key, value in row.items():
            if isinstance(value, (datetime, date)):
                serialized[key] = value.isoformat(sep=" ")
            else:
                serialized[key] = value
        return serialized

    @staticmethod
    def _entity_summary(
        first_rows: list[dict[str, Any]], second_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        entity_names = {
            row["entity_name"] for row in first_rows
        } | {row["entity_name"] for row in second_rows}
        summary = []
        for entity_name in sorted(entity_names):
            first = [row for row in first_rows if row["entity_name"] == entity_name]
            second = [row for row in second_rows if row["entity_name"] == entity_name]
            display_name = (first or second)[0]["entity_display_name"]
            summary.append(
                {
                    "entity_display_name": display_name,
                    "first_count": len(first),
                    "second_count": len(second),
                    "status": ReadingClient._comparison_status(len(first), len(second)),
                }
            )
        return summary

    @staticmethod
    def _comparison_status(first_count: int, second_count: int) -> str:
        if first_count and second_count:
            return "present_on_both_dates"
        if first_count:
            return "present_only_on_first_date"
        return "present_only_on_second_date"

    @staticmethod
    def _comparison_rows(
        reading_type: str,
        definition: ReadingDefinition,
        first_rows: list[dict[str, Any]],
        second_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        first_by_entity = ReadingClient._latest_by_entity(first_rows)
        second_by_entity = ReadingClient._latest_by_entity(second_rows)
        entity_names = set(first_by_entity) | set(second_by_entity)
        rows = []

        for entity_name in sorted(entity_names):
            first = first_by_entity.get(entity_name)
            second = second_by_entity.get(entity_name)
            display_name = (first or second)["entity_display_name"]

            if first is None or second is None:
                rows.append(
                    {
                        "entity_display_name": display_name,
                        "status": "missing_on_first_date"
                        if first is None
                        else "missing_on_second_date",
                    }
                )
                continue

            changes = ReadingClient._reading_changes(reading_type, definition, first, second)
            rows.append(
                {
                    "entity_display_name": display_name,
                    "status": "compared",
                    "changes": changes,
                }
            )

        return rows

    @staticmethod
    def _latest_by_entity(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        latest = {}
        for row in rows:
            current = latest.get(row["entity_name"])
            if current is None or str(row.get("time", "")) > str(current.get("time", "")):
                latest[row["entity_name"]] = row
        return latest

    @staticmethod
    def _reading_changes(
        reading_type: str,
        definition: ReadingDefinition,
        first: dict[str, Any],
        second: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if reading_type == "mixed_tank":
            changes = [
                ReadingClient._feet_inches_change(
                    "top_level",
                    "Top level",
                    first.get("top_level_feet"),
                    first.get("top_level_inches"),
                    second.get("top_level_feet"),
                    second.get("top_level_inches"),
                ),
                ReadingClient._feet_inches_change(
                    "water_level",
                    "Water level",
                    first.get("water_level_feet"),
                    first.get("water_level_inches"),
                    second.get("water_level_feet"),
                    second.get("water_level_inches"),
                ),
            ]
            return [
                change
                for change in changes
                if change["direction"] != "unchanged"
            ]

        changes = []
        for field in definition.fields:
            if field == "comments":
                continue
            first_value = first.get(field)
            second_value = second.get(field)
            if first_value == second_value:
                direction = "unchanged"
                delta = None
            elif _is_number(first_value) and _is_number(second_value):
                numeric_delta = float(second_value) - float(first_value)
                direction = _direction(numeric_delta)
                delta = round(numeric_delta, 3)
            else:
                direction = "changed"
                delta = None

            if direction != "unchanged":
                changes.append(
                    {
                        "field": field,
                        "first_value": first_value,
                        "second_value": second_value,
                        "direction": direction,
                        "delta": delta,
                    }
                )
        return changes

    @staticmethod
    def _feet_inches_change(
        field: str,
        label: str,
        first_feet: Any,
        first_inches: Any,
        second_feet: Any,
        second_inches: Any,
    ) -> dict[str, Any]:
        first_total = _total_inches(first_feet, first_inches)
        second_total = _total_inches(second_feet, second_inches)
        if first_total is None or second_total is None:
            return {
                "field": field,
                "label": label,
                "first_value": _format_feet_inches(first_feet, first_inches),
                "second_value": _format_feet_inches(second_feet, second_inches),
                "direction": "unknown",
                "delta": None,
            }

        delta_inches = second_total - first_total
        return {
            "field": field,
            "label": label,
            "first_value": _format_feet_inches(first_feet, first_inches),
            "second_value": _format_feet_inches(second_feet, second_inches),
            "direction": _direction(delta_inches),
            "delta_inches": delta_inches,
            "delta": _format_inches_delta(delta_inches),
        }

    @staticmethod
    def _with_entity_display(
        definition: ReadingDefinition, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        entity_type = BASE_ENTITY_LABELS.get(definition.base_table, "Entity")
        enriched = []
        for row in rows:
            row = dict(row)
            row["entity_type"] = entity_type
            row["entity_display_name"] = f"{entity_type} - {row['entity_name']}"
            enriched.append(row)
        return enriched

    @staticmethod
    def _with_tank_volumes(
        reading_type: str,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if reading_type not in {"linear_tank", "mixed_tank"}:
            return rows

        enriched = []
        for row in rows:
            row = dict(row)
            if reading_type == "linear_tank":
                row.update(_linear_tank_volume(row))
            elif reading_type == "mixed_tank":
                row.update(_mixed_tank_volumes(row))
            enriched.append(row)
        return enriched

    @staticmethod
    def _compact_reading_rows(
        definition: ReadingDefinition, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            ReadingClient._compact_reading_row(definition, row)
            for row in rows
        ]

    @staticmethod
    def _compact_reading_row(
        definition: ReadingDefinition, row: dict[str, Any]
    ) -> dict[str, Any]:
        compact = {
            "entity_display_name": row["entity_display_name"],
            "entity_type": row["entity_type"],
        }
        for field in definition.fields:
            value = row.get(field)
            if value is not None:
                compact[field] = value
        for field in TANK_VOLUME_FIELDS:
            value = row.get(field)
            if value is not None:
                compact[field] = value
        return compact

    @staticmethod
    def _compact_range_reading_rows(
        definition: ReadingDefinition, rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            {
                "date": str(row.get("time", ""))[:10],
                **ReadingClient._compact_reading_row(definition, row),
            }
            for row in rows
        ]

    @staticmethod
    def _filter_conditions(
        definition: ReadingDefinition,
        filters: list[dict[str, Any]],
        conditions: list[str],
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        operators = {
            ">": ">",
            ">=": ">=",
            "<": "<",
            "<=": "<=",
            "=": "=",
            "==": "=",
        }
        applied = []
        numeric_fields = {
            field for field in definition.fields if field not in {"comments", "type"}
        }
        for index, filter_item in enumerate(filters):
            field = str(filter_item.get("field", "")).strip()
            operator = operators.get(str(filter_item.get("operator", "")).strip())
            value = filter_item.get("value")
            if field not in numeric_fields:
                raise ReadingClientError(
                    f"Cannot filter {definition.label} by unsupported field: {field}."
                )
            if operator is None:
                raise ReadingClientError(
                    "Filter operator must be one of >, >=, <, <=, =, ==."
                )
            if not _is_number(value):
                raise ReadingClientError("Filter values must be numbers.")

            param_name = f"filter_{index}"
            conditions.append(f"r.{field} {operator} :{param_name}")
            params[param_name] = value
            applied.append({"field": field, "operator": operator, "value": value})
        return applied

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ReadingClientError("Dates must use YYYY-MM-DD format.") from exc

    @staticmethod
    def _day_bounds(day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time.min)
        return start, start + timedelta(days=1)

    @staticmethod
    def _definition(reading_type: str) -> ReadingDefinition:
        try:
            return READING_DEFINITIONS[reading_type]
        except KeyError as exc:
            raise ReadingClientError(f"Unsupported reading type: {reading_type}") from exc

    def _missing_reading_ignored_ids(
        self, site_id: int, reading_type: str
    ) -> tuple[int, ...]:
        site_cache = self._missing_reading_exclusion_cache.get(site_id)
        if site_cache is None:
            site_cache = self._load_missing_reading_exclusions(site_id)
            self._missing_reading_exclusion_cache[site_id] = site_cache
        return site_cache.get(reading_type, ())

    def _load_missing_reading_exclusions(
        self, site_id: int
    ) -> dict[str, tuple[int, ...]]:
        query = text(
            """
            SELECT related_entities
            FROM report_configurations
            WHERE site_id = :site_id
                AND function_name = :function_name
            ORDER BY id DESC
            LIMIT 1
            """
        )
        try:
            with self.engine.connect() as connection:
                row = (
                    connection.execute(
                        query,
                        {
                            "site_id": site_id,
                            "function_name": MISSING_READINGS_REPORT_FUNCTION_NAME,
                        },
                    )
                    .mappings()
                    .first()
                )
        except SQLAlchemyError:
            return {}

        if row is None:
            return {}

        related_entities = row.get("related_entities")
        if isinstance(related_entities, str):
            try:
                related_entities = json.loads(related_entities)
            except json.JSONDecodeError:
                return {}
        if not isinstance(related_entities, dict):
            return {}

        exclusions: dict[str, tuple[int, ...]] = {}
        if lact_ids := _coerce_id_list(related_entities.get("lact_ids")):
            exclusions["lact"] = lact_ids
        if flare_ids := _coerce_id_list(related_entities.get("flare_ids")):
            exclusions["flare"] = flare_ids
        if knock_out_ids := _coerce_id_list(related_entities.get("knock_out_ids")):
            exclusions["knock_out"] = knock_out_ids
        if tank_ids := related_entities.get("tank_ids"):
            if isinstance(tank_ids, dict):
                linear_ids = _coerce_id_list(tank_ids.get("linear"))
                mixed_ids = _coerce_id_list(tank_ids.get("mixed"))
                non_linear_ids = _coerce_id_list(tank_ids.get("nonLinear"))
                if linear_ids:
                    exclusions["linear_tank"] = linear_ids
                if mixed_ids:
                    exclusions["mixed_tank"] = mixed_ids
                if non_linear_ids:
                    exclusions["non_linear_tank"] = non_linear_ids
        if treater_ids := _coerce_id_list(related_entities.get("treater_ids")):
            exclusions["treater"] = treater_ids
        if water_plant_ids := _coerce_id_list(related_entities.get("water_plant_ids")):
            exclusions["water_plant"] = water_plant_ids
        if pump_ids := _coerce_id_list(related_entities.get("pump_ids")):
            exclusions["pump"] = pump_ids
        if flow_meter_ids := related_entities.get("flow_meter_ids"):
            if isinstance(flow_meter_ids, dict):
                combined = (
                    _coerce_id_list(flow_meter_ids.get("water"))
                    + _coerce_id_list(flow_meter_ids.get("gas"))
                    + _coerce_id_list(flow_meter_ids.get("oil"))
                )
                if combined:
                    exclusions["flow_meter"] = tuple(dict.fromkeys(combined))
        return exclusions


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _normalize_entity_match_value(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _entity_match_rank(row: dict[str, Any], normalized_query: str) -> int:
    """Rank exact entity-name/key matches ahead of broader partial matches."""
    values = [
        _normalize_entity_match_value(row.get("entity_name")),
        _normalize_entity_match_value(row.get("entity_key")),
    ]
    if normalized_query in values:
        return 0
    if any(value.endswith(normalized_query) for value in values if value):
        return 1
    return 2


def _reading_select_fields(definition: ReadingDefinition) -> str:
    fields = [f"r.{field}" for field in definition.fields]
    if definition.base_table == "tanks" and definition.table in {
        "linear_tank_readings",
        "mixed_tank_readings",
    }:
        fields.append("b.bbl_foot")
    return ",\n                ".join(fields)


def _reading_type_for_definition(definition: ReadingDefinition) -> str:
    for reading_type, candidate in READING_DEFINITIONS.items():
        if candidate is definition:
            return reading_type
    return ""


def _linear_tank_volume(row: dict[str, Any]) -> dict[str, Any]:
    bbl_foot = row.get("bbl_foot")
    if not _is_number(bbl_foot):
        return {"volume_status": "missing_bbl_foot"}

    if _is_number(row.get("level")):
        level = float(row["level"])
    elif _is_number(row.get("feet")) and _is_number(row.get("inches")):
        level = float(row["feet"]) + float(row["inches"]) / 12
    else:
        return {"bbl_foot": float(bbl_foot), "volume_status": "missing_level"}

    return {
        "bbl_foot": float(bbl_foot),
        "volume": round(level * float(bbl_foot), 2),
    }


def _mixed_tank_volumes(row: dict[str, Any]) -> dict[str, Any]:
    bbl_foot = row.get("bbl_foot")
    if not _is_number(bbl_foot):
        return {"volume_status": "missing_bbl_foot"}

    required = (
        row.get("top_level_feet"),
        row.get("top_level_inches"),
        row.get("water_level_feet"),
        row.get("water_level_inches"),
    )
    if not all(_is_number(value) for value in required):
        return {"bbl_foot": float(bbl_foot), "volume_status": "missing_levels"}

    top_level = float(row["top_level_feet"]) + float(row["top_level_inches"]) / 12
    water_level = float(row["water_level_feet"]) + float(row["water_level_inches"]) / 12
    total_volume = top_level * float(bbl_foot)
    water_volume = water_level * float(bbl_foot)
    oil_volume = total_volume - water_volume
    return {
        "bbl_foot": float(bbl_foot),
        "oil_volume": round(oil_volume, 2),
        "water_volume": round(water_volume, 2),
        "total_volume": round(total_volume, 2),
    }


def _direction(delta: float) -> str:
    if delta > 0:
        return "increase"
    if delta < 0:
        return "decrease"
    return "unchanged"


def _total_inches(feet: Any, inches: Any) -> float | None:
    if not _is_number(feet) or not _is_number(inches):
        return None
    return float(feet) * 12 + float(inches)


def _format_feet_inches(feet: Any, inches: Any) -> str | None:
    if not _is_number(feet) or not _is_number(inches):
        return None
    inches_value = float(inches)
    if inches_value.is_integer():
        inches_text = str(int(inches_value))
    else:
        inches_text = str(round(inches_value, 2)).rstrip("0").rstrip(".")
    return f"{int(feet)}'{inches_text}\""


def _format_inches_delta(delta_inches: float) -> str:
    signless = abs(delta_inches)
    feet = int(signless // 12)
    inches = signless - feet * 12
    if inches.is_integer():
        inches_text = str(int(inches))
    else:
        inches_text = str(round(inches, 2)).rstrip("0").rstrip(".")

    if feet:
        return f"{feet}'{inches_text}\""
    return f"{inches_text}\""


def _coerce_id_list(value: Any) -> tuple[int, ...]:
    if not value:
        return ()
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        value = [value]

    ids = []
    for item in value:
        try:
            ids.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(ids))


class UnavailableReadingClient:
    def __init__(self, reason: str):
        self.reason = reason

    def list_reading_types(self) -> list[dict[str, Any]]:
        return list_supported_reading_types()

    def get_readings_for_date(
        self, site_id: int, reading_type: str, reading_date: str
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def compare_readings_between_dates(
        self,
        site_id: int,
        reading_type: str,
        first_date: str,
        second_date: str,
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def missing_readings_for_date(
        self, site_id: int, reading_type: str, reading_date: str
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def all_missing_readings_for_date(
        self, site_id: int, reading_date: str
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def all_missing_readings_for_range(
        self, site_id: int, start_date: str, end_date: str
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def search_well_tests(
        self,
        site_id: int,
        start_date: str,
        end_date: str,
        well_name: str | None = None,
        min_oil: float | None = None,
        max_oil: float | None = None,
        min_water: float | None = None,
        max_water: float | None = None,
        min_gas: float | None = None,
        max_gas: float | None = None,
        min_runtime: float | None = None,
        max_runtime: float | None = None,
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def search_readings(
        self,
        site_id: int,
        reading_type: str,
        start_date: str,
        end_date: str,
        entity_name: str | None = None,
        filters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def resolve_reading_entity(
        self,
        site_id: int,
        entity_name: str,
        reading_date: str | None = None,
        reading_type: str | None = None,
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

    def get_reading_for_entity(
        self,
        site_id: int,
        entity_name: str,
        reading_date: str,
        reading_type: str | None = None,
    ) -> dict[str, Any]:
        raise ReadingClientError(self.reason)

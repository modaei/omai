from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from omai.config.settings import Settings


class ShutdownClientError(RuntimeError):
    """Raised when a well shutdown query is invalid or cannot be completed."""


DOWNTIME_CODES = {
    "PRF": "Paraffin",
    "BT": "Batch/Downhole Treating",
    "CL": "Casing Leak",
    "CR": "Compressor Problems",
    "DH": "Downhole Problems",
    "EL": "Electrical Problems",
    "ES": "Emergency Shut Down",
    "ESP": "ESP Downhole Problems",
    "FBTU": "Facility/Battery/TS Upset",
    "FPI": "Flowline/Pipeline Problems or Maintenance",
    "FSD": "Facility Shut Down",
    "NRW": "Non Rig Wellwork",
    "PR": "Pumping Rod Problems",
    "PU": "Artificial Lift Surface Failure",
    "RES": "Reservoir Management/Optimization",
    "RIG": "Rig Nearby (Sim Ops)",
    "SEI": "Surface Equipment Problems or Integrity",
    "SIB": "Shut In For Buildup",
    "TL": "Tubing Leak",
    "TPD": "Third Party Distruption",
    "TW": "Testing Another Well In Field",
    "UTP": "Uneconomical To Produce/Repair",
    "WEA": "Inclement Weather",
    "WHS": "Water Handling System Capacity",
    "WO": "Proactive/Planned Rigwork",
}


class ShutdownClient:
    def __init__(self, engine: Engine, max_rows: int = 500):
        self.engine = engine
        self.max_rows = max_rows

    @classmethod
    def from_settings(cls, settings: Settings) -> "ShutdownClient":
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

    def list_downtime_codes(self) -> dict[str, Any]:
        return {"downtime_codes": DOWNTIME_CODES}

    def get_shutdowns(
        self,
        site_id: int,
        start_date: str,
        end_date: str,
        shutdown_type: str = "all",
    ) -> dict[str, Any]:
        start_day = self._parse_date(start_date)
        end_day = self._parse_date(end_date)
        if start_day > end_day:
            raise ShutdownClientError("start_date cannot be after end_date.")
        if shutdown_type not in {"all", "short", "long"}:
            raise ShutdownClientError("shutdown_type must be all, short, or long.")

        short_shutdowns = []
        long_shutdowns = []
        if shutdown_type in {"all", "short"}:
            short_shutdowns = self._short_shutdowns(site_id, start_day, end_day)
        if shutdown_type in {"all", "long"}:
            long_shutdowns = self._long_shutdowns(site_id, start_day, end_day)

        total_hours = round(
            sum(row.get("hours") or 0 for row in short_shutdowns),
            3,
        )
        return {
            "site_id": site_id,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "shutdown_type": shutdown_type,
            "short_count": len(short_shutdowns),
            "long_count": len(long_shutdowns),
            "short_total_hours": total_hours,
            "short_shutdowns": short_shutdowns,
            "long_shutdowns": long_shutdowns,
        }

    def get_current_long_shutdowns(
        self, site_id: int, as_of_date: str | None = None
    ) -> dict[str, Any]:
        as_of = self._parse_date(as_of_date) if as_of_date else date.today()
        start, end = self._day_bounds(as_of)
        query = text(
            """
            SELECT wells.name AS well_name,
                well_shutdowns.long_shutdown_start,
                well_shutdowns.long_shutdown_end,
                well_shutdowns.downtime_code,
                well_shutdowns.comments
            FROM well_shutdowns
            JOIN wells ON well_shutdowns.well_id = wells.id
            WHERE wells.site_id = :site_id
                AND well_shutdowns.long_shutdown = 1
                AND well_shutdowns.long_shutdown_start < :end_time
                AND (
                    well_shutdowns.long_shutdown_end IS NULL
                    OR well_shutdowns.long_shutdown_end >= :start_time
                )
            ORDER BY well_shutdowns.long_shutdown_start, wells.name
            LIMIT :limit
            """
        )
        rows = self._execute(
            query,
            site_id=site_id,
            start_time=start,
            end_time=end,
        )
        shutdowns = [self._compact_shutdown_row(row, "long") for row in rows]
        return {
            "site_id": site_id,
            "as_of_date": as_of.isoformat(),
            "count": len(shutdowns),
            "long_shutdowns": shutdowns,
        }

    def summarize_shutdown_causes(
        self,
        site_id: int,
        start_date: str,
        end_date: str,
        shutdown_type: str = "all",
    ) -> dict[str, Any]:
        start_day = self._parse_date(start_date)
        end_day = self._parse_date(end_date)
        if start_day > end_day:
            raise ShutdownClientError("start_date cannot be after end_date.")
        if shutdown_type not in {"all", "short", "long"}:
            raise ShutdownClientError("shutdown_type must be all, short, or long.")

        short_shutdowns = []
        long_shutdowns = []
        if shutdown_type in {"all", "short"}:
            short_shutdowns = self._short_shutdowns(site_id, start_day, end_day)
        if shutdown_type in {"all", "long"}:
            long_shutdowns = self._long_shutdowns(site_id, start_day, end_day)

        cause_map: dict[str, dict[str, Any]] = {}
        for shutdown in short_shutdowns:
            cause = self._cause_bucket(cause_map, shutdown)
            cause["short_count"] += 1
            cause["event_count"] += 1
            cause["short_hours"] = round(
                cause["short_hours"] + float(shutdown.get("hours") or 0),
                3,
            )
            cause["total_hours"] = round(
                cause["short_hours"] + cause["long_overlap_hours"],
                3,
            )

        range_start = datetime.combine(start_day, time.min)
        range_end = datetime.combine(end_day, time.min) + timedelta(days=1)
        for shutdown in long_shutdowns:
            cause = self._cause_bucket(cause_map, shutdown)
            cause["long_count"] += 1
            cause["event_count"] += 1
            cause["long_overlap_hours"] = round(
                cause["long_overlap_hours"]
                + self._long_shutdown_overlap_hours(shutdown, range_start, range_end),
                3,
            )
            cause["total_hours"] = round(
                cause["short_hours"] + cause["long_overlap_hours"],
                3,
            )

        causes = sorted(
            cause_map.values(),
            key=lambda item: (
                item["total_hours"],
                item["event_count"],
                item["short_count"],
                item["long_count"],
                item["downtime_reason"],
            ),
            reverse=True,
        )
        return {
            "site_id": site_id,
            "start_date": start_day.isoformat(),
            "end_date": end_day.isoformat(),
            "shutdown_type": shutdown_type,
            "cause_count": len(causes),
            "main_cause": causes[0] if causes else None,
            "causes": causes,
        }

    def _short_shutdowns(
        self, site_id: int, start_day: date, end_day: date
    ) -> list[dict[str, Any]]:
        query = text(
            """
            SELECT wells.name AS well_name,
                well_shutdowns.date,
                well_shutdowns.hours,
                well_shutdowns.downtime_code,
                well_shutdowns.comments
            FROM well_shutdowns
            JOIN wells ON well_shutdowns.well_id = wells.id
            WHERE wells.site_id = :site_id
                AND well_shutdowns.long_shutdown = 0
                AND well_shutdowns.date >= :start_date
                AND well_shutdowns.date <= :end_date
            ORDER BY well_shutdowns.date, wells.name
            LIMIT :limit
            """
        )
        rows = self._execute(
            query,
            site_id=site_id,
            start_date=start_day,
            end_date=end_day,
        )
        return [self._compact_shutdown_row(row, "short") for row in rows]

    def _long_shutdowns(
        self, site_id: int, start_day: date, end_day: date
    ) -> list[dict[str, Any]]:
        start_time = datetime.combine(start_day, time.min)
        end_time = datetime.combine(end_day, time.min) + timedelta(days=1)
        query = text(
            """
            SELECT wells.name AS well_name,
                well_shutdowns.long_shutdown_start,
                well_shutdowns.long_shutdown_end,
                well_shutdowns.downtime_code,
                well_shutdowns.comments
            FROM well_shutdowns
            JOIN wells ON well_shutdowns.well_id = wells.id
            WHERE wells.site_id = :site_id
                AND well_shutdowns.long_shutdown = 1
                AND well_shutdowns.long_shutdown_start < :end_time
                AND (
                    well_shutdowns.long_shutdown_end IS NULL
                    OR well_shutdowns.long_shutdown_end >= :start_time
                )
            ORDER BY well_shutdowns.long_shutdown_start, wells.name
            LIMIT :limit
            """
        )
        rows = self._execute(
            query,
            site_id=site_id,
            start_time=start_time,
            end_time=end_time,
        )
        return [self._compact_shutdown_row(row, "long") for row in rows]

    def _execute(self, query, **params: Any) -> list[dict[str, Any]]:
        params.setdefault("limit", self.max_rows)
        try:
            with self.engine.connect() as connection:
                result = connection.execute(query, params)
                return [self._serialize_row(dict(row._mapping)) for row in result]
        except SQLAlchemyError as exc:
            raise ShutdownClientError(f"Database query failed: {exc}") from exc

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
    def _compact_shutdown_row(row: dict[str, Any], shutdown_type: str) -> dict[str, Any]:
        compact = {
            "well": f"Well - {row['well_name']}",
            "type": shutdown_type,
        }
        if shutdown_type == "short":
            compact["date"] = row.get("date")
            compact["hours"] = row.get("hours")
        else:
            compact["start"] = row.get("long_shutdown_start")
            compact["end"] = row.get("long_shutdown_end")

        code = row.get("downtime_code")
        if code:
            compact["downtime_code"] = code
            compact["downtime_reason"] = DOWNTIME_CODES.get(code, code)
        if row.get("comments"):
            compact["comments"] = row["comments"]
        return compact

    @staticmethod
    def _long_shutdown_overlap_hours(
        row: dict[str, Any],
        range_start: datetime,
        range_end: datetime,
    ) -> float:
        start = _parse_datetime_value(row.get("start"))
        end = _parse_datetime_value(row.get("end")) or range_end
        if start is None:
            return 0.0

        overlap_start = max(start, range_start)
        overlap_end = min(end, range_end)
        if overlap_end <= overlap_start:
            return 0.0
        return round((overlap_end - overlap_start).total_seconds() / 3600, 3)

    @staticmethod
    def _cause_bucket(
        cause_map: dict[str, dict[str, Any]],
        row: dict[str, Any],
    ) -> dict[str, Any]:
        code = row.get("downtime_code") or "UNKNOWN"
        if code not in cause_map:
            cause_map[code] = {
                "downtime_code": code,
                "downtime_reason": DOWNTIME_CODES.get(
                    code, "Unknown" if code == "UNKNOWN" else code
                ),
                "event_count": 0,
                "short_count": 0,
                "long_count": 0,
                "short_hours": 0.0,
                "long_overlap_hours": 0.0,
                "total_hours": 0.0,
            }
        return cause_map[code]

    @staticmethod
    def _parse_date(value: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ShutdownClientError("Dates must use YYYY-MM-DD format.") from exc

    @staticmethod
    def _day_bounds(day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, time.min)
        return start, start + timedelta(days=1)


class UnavailableShutdownClient:
    def __init__(self, reason: str):
        self.reason = reason

    def list_downtime_codes(self) -> dict[str, Any]:
        return {"downtime_codes": DOWNTIME_CODES}

    def get_shutdowns(
        self,
        site_id: int,
        start_date: str,
        end_date: str,
        shutdown_type: str = "all",
    ) -> dict[str, Any]:
        raise ShutdownClientError(self.reason)

    def get_current_long_shutdowns(
        self, site_id: int, as_of_date: str | None = None
    ) -> dict[str, Any]:
        raise ShutdownClientError(self.reason)

    def summarize_shutdown_causes(
        self,
        site_id: int,
        start_date: str,
        end_date: str,
        shutdown_type: str = "all",
    ) -> dict[str, Any]:
        raise ShutdownClientError(self.reason)


def _parse_datetime_value(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        return None

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from omai.config.settings import Settings


class DataPointClientError(RuntimeError):
    """Raised when current telemetry values cannot be resolved or queried."""


class DataPointClient:
    """Read current site-scoped values from `data_point_data`."""

    def __init__(self, engine: Engine, timezone_name: str = "UTC"):
        self.engine = engine
        try:
            self.timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise DataPointClientError(f"Invalid timezone: {timezone_name}") from exc

    @classmethod
    def from_settings(cls, settings: Settings) -> "DataPointClient":
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
        return cls(engine, settings.timezone)

    def get_values(
        self,
        site_id: int,
        facility_name: str,
        data_point_name: str,
        device_name: str | None = None,
    ) -> dict[str, Any]:
        """Resolve selectors and return every closest matching current value."""
        facility_query = _normalize_name(facility_name)
        data_point_query = _normalize_name(data_point_name)
        if not facility_query:
            raise DataPointClientError("facility_name is required.")
        if not data_point_query:
            raise DataPointClientError("data_point_name is required.")

        rows = self._site_data_points(site_id)
        facility_matches = _ranked_matches(
            facility_query,
            {str(row["effective_facility_name"]) for row in rows},
            mode="suffix",
        )
        if not facility_matches:
            return _not_found("facility", facility_name)
        if len(facility_matches) > 1:
            return _ambiguous("facility", facility_name, facility_matches)
        resolved_facility = facility_matches[0]
        facility_rows = [
            row
            for row in rows
            if _normalize_name(row["effective_facility_name"])
            == _normalize_name(resolved_facility)
        ]

        device_defaulted = device_name is None or not str(device_name).strip()
        requested_device = resolved_facility if device_defaulted else str(device_name)
        device_query = _normalize_name(requested_device)
        device_matches = _ranked_matches(
            device_query,
            {str(row["device_name"]) for row in facility_rows if row.get("device_name")},
            mode="exact",
        )
        if not device_matches:
            return {
                **_not_found("device", requested_device),
                "facility_name": resolved_facility,
                "device_defaulted": device_defaulted,
            }
        if len(device_matches) > 1:
            return {
                **_ambiguous("device", requested_device, device_matches),
                "facility_name": resolved_facility,
                "device_defaulted": device_defaulted,
            }
        resolved_device = device_matches[0]
        device_rows = [
            row
            for row in facility_rows
            if _normalize_name(row.get("device_name")) == _normalize_name(resolved_device)
        ]

        point_matches = _ranked_matches(
            data_point_query,
            {str(row["data_point_name"]) for row in device_rows if row.get("data_point_name")},
            mode="prefix",
        )
        if not point_matches:
            return {
                **_not_found("data_point", data_point_name),
                "facility_name": resolved_facility,
                "device_name": resolved_device,
                "device_defaulted": device_defaulted,
            }
        matched_names = {_normalize_name(name) for name in point_matches}
        values = [
            self._compact_value(row)
            for row in device_rows
            if _normalize_name(row.get("data_point_name")) in matched_names
        ]
        values.sort(key=lambda item: item["data_point_name"])
        return {
            "status": "found",
            "facility_name": resolved_facility,
            "device_name": resolved_device,
            "device_defaulted": device_defaulted,
            "requested_data_point_name": data_point_name,
            "match_count": len(values),
            "values": values,
        }

    def _site_data_points(self, site_id: int) -> list[dict[str, Any]]:
        query = text(
            """
            SELECT COALESCE(parent.facility_name, dp.facility_name) AS effective_facility_name,
                dp.device_name,
                dp.data_point_name,
                dp.type AS data_point_type,
                dp.active,
                current_value.data,
                current_value.last_update
            FROM data_points dp
            LEFT JOIN data_points parent ON parent.id = dp.facility_id
            LEFT JOIN data_point_data current_value ON current_value.data_point_id = dp.id
            WHERE dp.site_id = :site_id
              AND dp.data_point_name IS NOT NULL
            ORDER BY effective_facility_name, dp.device_name, dp.data_point_name
            """
        )
        try:
            with self.engine.connect() as connection:
                result = connection.execute(query, {"site_id": site_id})
                return [dict(row._mapping) for row in result]
        except SQLAlchemyError as exc:
            raise DataPointClientError(f"Database query failed: {exc}") from exc

    def _compact_value(self, row: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        data_error = None
        if row.get("data"):
            try:
                decoded = json.loads(row["data"])
                if isinstance(decoded, dict):
                    payload = decoded
                else:
                    data_error = "Current value payload is not a JSON object."
            except (TypeError, ValueError):
                data_error = "Current value payload is invalid JSON."
        timestamp = payload.get("timestamp") or row.get("last_update")
        value = {
            "data_point_name": row["data_point_name"],
            "value": payload.get("value"),
            "value_available": "value" in payload,
            "value_type": payload.get("type") or row.get("data_point_type"),
            "received_at": self._format_timestamp(timestamp),
            "active": bool(row.get("active")),
        }
        if data_error:
            value["data_error"] = data_error
        return value

    def _format_timestamp(self, value: Any) -> str | None:
        if value is None:
            return None
        try:
            timestamp = float(value)
            if timestamp > 1_000_000_000_000:
                timestamp /= 1000
            received = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            return None
        return received.astimezone(self.timezone).strftime("%m/%d/%Y %H:%M:%S %Z")


class UnavailableDataPointClient:
    """Data-point client used when database settings are unavailable."""

    def __init__(self, reason: str):
        self.reason = reason

    def get_values(
        self,
        site_id: int,
        facility_name: str,
        data_point_name: str,
        device_name: str | None = None,
    ) -> dict[str, Any]:
        raise DataPointClientError(self.reason)


def _normalize_name(value: Any) -> str:
    """Normalize human labels and telemetry selectors to underscore tokens."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(value or "").lower())).strip("_")


def _ranked_matches(query: str, names: set[str], *, mode: str) -> list[str]:
    """Return exact matches or the closest safe prefix/suffix matches."""
    normalized = [(name, _normalize_name(name)) for name in names if _normalize_name(name)]
    exact = sorted(name for name, candidate in normalized if candidate == query)
    if exact or mode == "exact":
        return exact
    if mode == "suffix":
        candidates = [
            (name, len(candidate.split("_")) - len(query.split("_")))
            for name, candidate in normalized
            if candidate.endswith(f"_{query}")
        ]
    else:
        candidates = [
            (name, len(candidate.split("_")) - len(query.split("_")))
            for name, candidate in normalized
            if candidate.startswith(f"{query}_")
        ]
    if not candidates:
        return []
    closest = min(score for _, score in candidates)
    return sorted(name for name, score in candidates if score == closest)


def _not_found(selector: str, requested: str) -> dict[str, Any]:
    return {
        "status": "not_found",
        "selector": selector,
        "requested": requested,
        "candidates": [],
    }


def _ambiguous(selector: str, requested: str, candidates: list[str]) -> dict[str, Any]:
    return {
        "status": "ambiguous",
        "selector": selector,
        "requested": requested,
        "candidates": candidates,
    }

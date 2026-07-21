from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, URL
from sqlalchemy.exc import SQLAlchemyError

from omai.config.settings import Settings


class AnomalyRepositoryError(RuntimeError):
    """Raised when anomaly tables cannot be queried or updated."""


@dataclass(frozen=True)
class DataPointTarget:
    id: int
    site_id: int
    site_key: str
    facility_id: int | None
    facility_name: str
    device_name: str
    data_point_name: str
    tags: str
    active: bool


@dataclass(frozen=True)
class AnomalyRule:
    id: int
    site_id: int
    source_type: str
    scope_type: str
    scope_id: int | None
    scope_key: str | None
    tag_pattern: str | None
    enabled: bool
    check_interval_minutes: int | None
    lookback_days: int | None
    comparison_mode: str | None
    max_percent_change: float | None
    max_z_score: float | None
    flatline_minutes: int | None
    stale_minutes: int | None


class AnomalyRepository:
    """Read rules/targets and persist compact state plus anomaly events in MySQL."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @classmethod
    def from_settings(cls, settings: Settings) -> "AnomalyRepository":
        settings.validate_database()
        engine = create_engine(
            URL.create(
                "mysql+pymysql",
                username=settings.db_user,
                password=settings.db_password,
                host=settings.db_host,
                port=settings.db_port,
                database=settings.db_name,
            ),
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            pool_recycle=settings.db_pool_recycle,
            pool_pre_ping=True,
        )
        return cls(engine)

    def data_point_targets(self, site_id: int) -> list[DataPointTarget]:
        query = text(
            """
            SELECT dp.id, dp.site_id, s.`key` AS site_key, dp.facility_id,
                COALESCE(parent.facility_name, dp.facility_name) AS facility_name,
                dp.device_name, dp.data_point_name, COALESCE(dp.tags, dp.data_point_name) AS tags,
                dp.active
            FROM data_points dp
            JOIN sites s ON s.id=dp.site_id
            LEFT JOIN data_points parent ON parent.id=dp.facility_id
            WHERE dp.site_id=:site_id
              AND dp.data_point_name IS NOT NULL
              AND dp.type='number'
            ORDER BY facility_name, dp.device_name, dp.data_point_name
            """
        )
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(query, {"site_id": site_id}).mappings().all()
        except SQLAlchemyError as exc:
            raise AnomalyRepositoryError(f"Could not load data points: {exc}") from exc
        return [
            DataPointTarget(
                id=int(row["id"]),
                site_id=int(row["site_id"]),
                site_key=str(row["site_key"]),
                facility_id=int(row["facility_id"]) if row["facility_id"] is not None else None,
                facility_name=str(row["facility_name"] or ""),
                device_name=str(row["device_name"] or ""),
                data_point_name=str(row["data_point_name"] or ""),
                tags=str(row["tags"] or ""),
                active=bool(row["active"]),
            )
            for row in rows
        ]

    def rules(self, site_id: int, source_type: str = "data_point") -> list[AnomalyRule]:
        query = text(
            """
            SELECT *
            FROM ai_anomaly_rules
            WHERE site_id=:site_id AND source_type=:source_type
            ORDER BY LENGTH(COALESCE(tag_pattern, scope_key, '')) DESC, updated_at DESC
            """
        )
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    query, {"site_id": site_id, "source_type": source_type}
                ).mappings().all()
        except SQLAlchemyError as exc:
            raise AnomalyRepositoryError(f"Could not load anomaly rules: {exc}") from exc
        return [_rule(row) for row in rows]

    def metric_state(self, site_id: int, source_key: str) -> dict[str, Any] | None:
        query = text(
            """
            SELECT *
            FROM ai_anomaly_metric_states
            WHERE site_id=:site_id AND source_key=:source_key
            LIMIT 1
            """
        )
        try:
            with self.engine.connect() as connection:
                row = connection.execute(query, {"site_id": site_id, "source_key": source_key}).mappings().first()
        except SQLAlchemyError as exc:
            raise AnomalyRepositoryError(f"Could not load anomaly state: {exc}") from exc
        return dict(row) if row else None

    def upsert_metric_state(self, state: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        payload = {**state, "now": now}
        query = text(
            """
            INSERT INTO ai_anomaly_metric_states
                (site_id, source_type, source_key, scope_type, scope_id, metric_key,
                 comparison_mode, last_checked_at, last_observed_at, last_value, last_delta,
                 rolling_count, rolling_avg, rolling_stddev, rolling_min, rolling_max,
                 rolling_median, rolling_mad, flatline_since, stale_since, last_anomaly_at,
                 baseline_status, baseline_days_available, baseline_built_at, skip_reason,
                 created_at, updated_at)
            VALUES
                (:site_id, :source_type, :source_key, :scope_type, :scope_id, :metric_key,
                 :comparison_mode, :last_checked_at, :last_observed_at, :last_value, :last_delta,
                 :rolling_count, :rolling_avg, :rolling_stddev, :rolling_min, :rolling_max,
                 :rolling_median, :rolling_mad, :flatline_since, :stale_since, :last_anomaly_at,
                 :baseline_status, :baseline_days_available, :baseline_built_at, :skip_reason,
                 :now, :now)
            ON DUPLICATE KEY UPDATE
                comparison_mode=VALUES(comparison_mode),
                last_checked_at=VALUES(last_checked_at),
                last_observed_at=VALUES(last_observed_at),
                last_value=VALUES(last_value),
                last_delta=VALUES(last_delta),
                rolling_count=VALUES(rolling_count),
                rolling_avg=VALUES(rolling_avg),
                rolling_stddev=VALUES(rolling_stddev),
                rolling_min=VALUES(rolling_min),
                rolling_max=VALUES(rolling_max),
                rolling_median=VALUES(rolling_median),
                rolling_mad=VALUES(rolling_mad),
                flatline_since=VALUES(flatline_since),
                stale_since=VALUES(stale_since),
                last_anomaly_at=VALUES(last_anomaly_at),
                baseline_status=VALUES(baseline_status),
                baseline_days_available=VALUES(baseline_days_available),
                baseline_built_at=VALUES(baseline_built_at),
                skip_reason=VALUES(skip_reason),
                updated_at=VALUES(updated_at)
            """
        )
        try:
            with self.engine.begin() as connection:
                connection.execute(query, _nullable_state(payload))
        except SQLAlchemyError as exc:
            raise AnomalyRepositoryError(f"Could not save anomaly state: {exc}") from exc

    def insert_event(self, event: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        payload = {
            **event,
            "details_json": json.dumps(event.get("details_json") or {}, separators=(",", ":")),
            "now": now,
        }
        query = text(
            """
            INSERT INTO ai_anomaly_events
                (site_id, source_type, source_key, source_id, entity_type, entity_id, entity_name,
                 metric_key, severity, status, detected_at, window_start, window_end, value,
                 expected_min, expected_max, baseline_value, deviation_percent, reason,
                 details_json, created_at, updated_at)
            VALUES
                (:site_id, :source_type, :source_key, :source_id, :entity_type, :entity_id, :entity_name,
                 :metric_key, :severity, :status, :detected_at, :window_start, :window_end, :value,
                 :expected_min, :expected_max, :baseline_value, :deviation_percent, :reason,
                 :details_json, :now, :now)
            """
        )
        try:
            with self.engine.begin() as connection:
                connection.execute(query, payload)
        except SQLAlchemyError as exc:
            raise AnomalyRepositoryError(f"Could not save anomaly event: {exc}") from exc

    def search_events(
        self,
        site_id: int,
        start: datetime | None = None,
        end: datetime | None = None,
        source_type: str | None = None,
        status: str | None = "open",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses = ["site_id=:site_id"]
        params: dict[str, Any] = {"site_id": site_id, "limit": limit}
        if start:
            clauses.append("detected_at>=:start")
            params["start"] = start
        if end:
            clauses.append("detected_at<=:end")
            params["end"] = end
        if source_type:
            clauses.append("source_type=:source_type")
            params["source_type"] = source_type
        if status:
            clauses.append("status=:status")
            params["status"] = status
        query = text(
            f"""
            SELECT id, source_type, entity_name, metric_key, severity, status,
                detected_at, value, baseline_value, deviation_percent, reason
            FROM ai_anomaly_events
            WHERE {' AND '.join(clauses)}
            ORDER BY detected_at DESC, id DESC
            LIMIT :limit
            """
        )
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(query, params).mappings().all()
        except SQLAlchemyError as exc:
            raise AnomalyRepositoryError(f"Could not search anomaly events: {exc}") from exc
        return [dict(row) for row in rows]


def _rule(row: Any) -> AnomalyRule:
    return AnomalyRule(
        id=int(row["id"]),
        site_id=int(row["site_id"]),
        source_type=str(row["source_type"]),
        scope_type=str(row["scope_type"]),
        scope_id=int(row["scope_id"]) if row["scope_id"] is not None else None,
        scope_key=str(row["scope_key"]) if row["scope_key"] is not None else None,
        tag_pattern=str(row["tag_pattern"]) if row["tag_pattern"] is not None else None,
        enabled=bool(row["enabled"]),
        check_interval_minutes=int(row["check_interval_minutes"]) if row["check_interval_minutes"] is not None else None,
        lookback_days=int(row["lookback_days"]) if row["lookback_days"] is not None else None,
        comparison_mode=str(row["comparison_mode"]) if row["comparison_mode"] is not None else None,
        max_percent_change=float(row["max_percent_change"]) if row["max_percent_change"] is not None else None,
        max_z_score=float(row["max_z_score"]) if row["max_z_score"] is not None else None,
        flatline_minutes=int(row["flatline_minutes"]) if row["flatline_minutes"] is not None else None,
        stale_minutes=int(row["stale_minutes"]) if row["stale_minutes"] is not None else None,
    )


def _nullable_state(state: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "site_id", "source_type", "source_key", "scope_type", "scope_id", "metric_key",
        "comparison_mode", "last_checked_at", "last_observed_at", "last_value", "last_delta",
        "rolling_count", "rolling_avg", "rolling_stddev", "rolling_min", "rolling_max",
        "rolling_median", "rolling_mad", "flatline_since", "stale_since", "last_anomaly_at",
        "baseline_status", "baseline_days_available", "baseline_built_at", "skip_reason", "now",
    ]
    return {key: state.get(key) for key in keys}

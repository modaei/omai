from __future__ import annotations

import fnmatch
import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from typing import Any

from omai.clients.graphite_trend_client import GraphiteTarget, GraphiteTrendClient
from omai.repositories.anomaly_repository import (
    AnomalyRepository,
    AnomalyRule,
    DataPointTarget,
)
from omai.services.anomaly_metric_catalog import MetricCatalog


MIN_BASELINE_DAYS = 10


class AnomalyDetectionService:
    """Build baselines, evaluate recent trends, and record deterministic anomalies."""

    def __init__(
        self,
        repository: AnomalyRepository,
        graphite: GraphiteTrendClient,
        catalog: MetricCatalog | None = None,
    ):
        self.repository = repository
        self.graphite = graphite
        self.catalog = catalog or MetricCatalog()

    def build_data_point_baseline(self, site_id: int, days: int = 30) -> dict[str, int]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        targets = self.repository.data_point_targets(site_id)
        series = self.graphite.fetch(
            [_graphite_target(target) for target in targets],
            start,
            end,
            max_data_points=days * 24,
        )
        summary = {"ready": 0, "insufficient_history": 0, "empty": 0}
        for target in targets:
            points = series.get(target.id, [])
            state = self._baseline_state(target, points)
            self.repository.upsert_metric_state(state)
            summary[state["baseline_status"]] = summary.get(state["baseline_status"], 0) + 1
        return summary

    def check_data_point_anomalies(
        self,
        site_id: int,
        window: timedelta = timedelta(hours=1),
    ) -> dict[str, int]:
        end = datetime.now(timezone.utc)
        start = end - window
        targets = self.repository.data_point_targets(site_id)
        rules = self.repository.rules(site_id, "data_point")
        target_rules = {target.id: _resolve_data_point_rule(target, rules) for target in targets}
        claimable = [
            target for target in targets
            if target.active and (target_rules[target.id] is None or target_rules[target.id].enabled)
        ]
        series = self.graphite.fetch(
            [_graphite_target(target) for target in claimable],
            start,
            end,
            max_data_points=120,
        )
        summary = {"checked": 0, "skipped": 0, "anomalies": 0}
        for target in claimable:
            state = self.repository.metric_state(site_id, _source_key(target))
            if not state or state.get("baseline_status") != "ready":
                summary["skipped"] += 1
                continue
            rule = target_rules[target.id]
            event = self._evaluate_target(target, series.get(target.id, []), state, rule, start, end)
            updated = self._updated_state(target, series.get(target.id, []), state, event is not None)
            self.repository.upsert_metric_state(updated)
            summary["checked"] += 1
            if event:
                self.repository.insert_event(event)
                summary["anomalies"] += 1
        return summary

    def _baseline_state(self, target: DataPointTarget, points: list[dict[str, Any]]) -> dict[str, Any]:
        values = [point["value"] for point in points]
        observed_days = _observed_days(points)
        status = "ready" if observed_days >= MIN_BASELINE_DAYS and values else "insufficient_history"
        if not values:
            status = "empty"
        stats = _stats(values)
        latest = points[-1] if points else {}
        return {
            "site_id": target.site_id,
            "source_type": "data_point",
            "source_key": _source_key(target),
            "scope_type": "data_point",
            "scope_id": target.id,
            "metric_key": target.tags,
            "comparison_mode": "point_value",
            "last_checked_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "last_observed_at": _naive(latest.get("time")),
            "last_value": latest.get("value"),
            "last_delta": None,
            "rolling_count": len(values),
            **stats,
            "flatline_since": None,
            "stale_since": None,
            "last_anomaly_at": None,
            "baseline_status": status,
            "baseline_days_available": observed_days,
            "baseline_built_at": datetime.now(timezone.utc).replace(tzinfo=None),
            "skip_reason": None if status == "ready" else "Less than 10 days of usable Graphite history.",
        }

    def _evaluate_target(
        self,
        target: DataPointTarget,
        points: list[dict[str, Any]],
        state: dict[str, Any],
        rule: AnomalyRule | None,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any] | None:
        if not points:
            stale_minutes = _rule_value(rule, "stale_minutes", 120)
            last_observed = state.get("last_observed_at")
            if last_observed and end.replace(tzinfo=None) - last_observed > timedelta(minutes=stale_minutes):
                return _event(target, start, end, None, state, "high", "Data point is stale.")
            return None
        latest = points[-1]["value"]
        avg = float(state.get("rolling_avg") or 0)
        stddev = float(state.get("rolling_stddev") or 0)
        max_percent_change = _rule_value(rule, "max_percent_change", 50.0)
        max_z_score = _rule_value(rule, "max_z_score", 4.0)
        percent_change = abs((latest - avg) / avg * 100) if avg else 0
        z_score = abs((latest - avg) / stddev) if stddev else 0
        if percent_change > max_percent_change and z_score > max_z_score:
            return _event(
                target,
                start,
                end,
                latest,
                state,
                "high" if z_score >= max_z_score * 1.5 else "medium",
                f"Value deviated {percent_change:.1f}% from baseline and z-score is {z_score:.2f}.",
                percent_change,
            )
        return None

    def _updated_state(
        self,
        target: DataPointTarget,
        points: list[dict[str, Any]],
        old: dict[str, Any],
        anomaly: bool,
    ) -> dict[str, Any]:
        values = [float(old.get("rolling_avg") or 0)] * max(0, int(old.get("rolling_count") or 0))
        values.extend(point["value"] for point in points)
        # Keep the state compact by recalculating from a bounded pseudo-history.
        values = values[-720:]
        stats = _stats(values)
        latest = points[-1] if points else {}
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return {
            **old,
            "site_id": target.site_id,
            "source_type": "data_point",
            "source_key": _source_key(target),
            "scope_type": "data_point",
            "scope_id": target.id,
            "metric_key": target.tags,
            "comparison_mode": old.get("comparison_mode") or "point_value",
            "last_checked_at": now,
            "last_observed_at": _naive(latest.get("time")) or old.get("last_observed_at"),
            "last_value": latest.get("value", old.get("last_value")),
            "rolling_count": len(values),
            **stats,
            "last_anomaly_at": now if anomaly else old.get("last_anomaly_at"),
        }


def _graphite_target(target: DataPointTarget) -> GraphiteTarget:
    facility = target.facility_name.replace(" ", "_")
    device = target.device_name.replace(" ", "_")
    metric = target.tags or target.data_point_name
    if device == facility or facility.upper().startswith("HDU_"):
        graphite = f"MI3.{target.site_key}.POC.{facility}.{metric}"
    else:
        graphite = f"MI3.{target.site_key}.{device}.{metric}"
    return GraphiteTarget(data_point_id=target.id, target=graphite)


def _source_key(target: DataPointTarget) -> str:
    return f"data_point:{target.id}"


def _resolve_data_point_rule(target: DataPointTarget, rules: list[AnomalyRule]) -> AnomalyRule | None:
    precedence = {"data_point": 0, "device": 1, "facility": 2, "tag_pattern": 3, "site": 4}
    matches = [rule for rule in rules if _matches_rule(target, rule)]
    matches.sort(key=lambda rule: (precedence.get(rule.scope_type, 99), -(len(rule.tag_pattern or rule.scope_key or ""))))
    return matches[0] if matches else None


def _matches_rule(target: DataPointTarget, rule: AnomalyRule) -> bool:
    if rule.scope_type == "site":
        return True
    if rule.scope_type == "data_point":
        return rule.scope_id == target.id
    if rule.scope_type == "facility":
        return rule.scope_id == target.facility_id
    if rule.scope_type == "device":
        return rule.scope_id == target.facility_id and (rule.scope_key or "").casefold() == target.device_name.casefold()
    if rule.scope_type == "tag_pattern" and rule.tag_pattern:
        return fnmatch.fnmatchcase(target.tags.casefold(), rule.tag_pattern.replace("%", "*").replace("_", "?").casefold())
    return False


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "rolling_avg": None,
            "rolling_stddev": None,
            "rolling_min": None,
            "rolling_max": None,
            "rolling_median": None,
            "rolling_mad": None,
        }
    med = median(values)
    return {
        "rolling_avg": mean(values),
        "rolling_stddev": pstdev(values) if len(values) > 1 else 0.0,
        "rolling_min": min(values),
        "rolling_max": max(values),
        "rolling_median": med,
        "rolling_mad": median([abs(value - med) for value in values]),
    }


def _observed_days(points: list[dict[str, Any]]) -> int:
    return len({point["time"].date() for point in points if point.get("time")})


def _rule_value(rule: AnomalyRule | None, field: str, default: Any) -> Any:
    if rule is None:
        return default
    value = getattr(rule, field)
    return default if value is None else value


def _event(
    target: DataPointTarget,
    start: datetime,
    end: datetime,
    value: float | None,
    state: dict[str, Any],
    severity: str,
    reason: str,
    percent_change: float | None = None,
) -> dict[str, Any]:
    stddev = float(state.get("rolling_stddev") or 0)
    avg = float(state.get("rolling_avg") or 0)
    return {
        "site_id": target.site_id,
        "source_type": "data_point",
        "source_key": _source_key(target),
        "source_id": target.id,
        "entity_type": "data_point",
        "entity_id": target.id,
        "entity_name": f"{target.facility_name} / {target.device_name} / {target.data_point_name}",
        "metric_key": target.tags,
        "severity": severity,
        "status": "open",
        "detected_at": end.replace(tzinfo=None),
        "window_start": start.replace(tzinfo=None),
        "window_end": end.replace(tzinfo=None),
        "value": value,
        "expected_min": avg - (4 * stddev) if stddev else None,
        "expected_max": avg + (4 * stddev) if stddev else None,
        "baseline_value": avg or None,
        "deviation_percent": percent_change,
        "reason": reason,
        "details_json": {"baseline": {key: str(value) for key, value in state.items() if key.startswith("rolling_")}},
    }


def _naive(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    return None

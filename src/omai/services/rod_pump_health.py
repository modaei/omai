from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Engine, text


METRICS = {
    "runtime": "Yesterday Percent",
    "cycles": "Yesterday Cycles",
    "peak_load": "Yesterday Peak Load",
    "min_load": "Yesterday Min Load",
    "peak_limit": "Peak Load Setpoint",
    "min_limit": "Min Load Setpoint",
    "spm": "Stroke Min",
    "stroke_length": "Stroke Length",
    "fillage": "Pump Fillage",
    "fillage_setpoint": "Fillage Setpoint",
    "well_state_code": "Well State Code",
    "well_state": "Well State",
    "state_code": "State Code",
    "control_mode": "Control Mode Setpoint",
    "operation_mode": "Operation Mode Setpoint",
    "host_switch": "Host Switch Setpoint",
}
TAG_ALIASES = {
    "peak_load_sp": "max_load",
    "min_load_sp": "min_load",
    "peak_load_ls": "peak_load_last_stroke",
    "min_load_ls": "min_load_last_stroke",
}

MALFUNCTION_CODES = {32, 33, 34, 40, 46, 47, 48, 49, 50, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, *range(83, 91)}
PUMP_OFF_CODES = {9, 31, 35, 36, 37, 41, 42, 44, *range(46, 66)}
OPTIONAL_METRICS = {"well_state", "state_code"}
CORE_SAM1_METRICS = {"runtime", "cycles", "peak_load", "min_load", "well_state_code"}


class RodPumpHealthEvaluator:
    """Derive and persist Phase 1 health episodes from Graphite SAM1 telemetry."""

    def __init__(self, engine: Engine, monitoring_url: str, timezone_name: str, http_get: Callable[..., Any] = httpx.get, reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None):
        self.engine = engine
        self.monitoring_url = monitoring_url.rstrip("/")
        self.timezone = ZoneInfo(timezone_name)
        self.http_get = http_get
        self.reviewer = reviewer

    def evaluate(self, site_id: int, well_id: int, now: datetime | None = None) -> dict[str, Any]:
        """Evaluate current conditions using the previously refreshed baseline."""
        end = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        # A short overlap preserves state transitions at the operational-day
        # boundary without re-reading the historical baseline window.
        start = end - timedelta(days=2)
        well = self._well(site_id, well_id)
        if self._is_currently_shutdown(well_id, end):
            self._suppress_shutdown_episodes(well_id, end)
            return {"well": well, "status": "skipped_shutdown", "daily": {}, "baseline": self._stored_baseline(well_id, end), "coverage": {}, "episodes": []}
        catalog = self._catalog(site_id, well["well_key"])
        series, coverage = self._series(well, catalog, start, end)
        daily_by_date = self._daily_by_date(series, start, end)
        for metric_date, metric in daily_by_date.items():
            day_start = datetime.combine(metric_date, datetime.min.time(), self.timezone)
            self._upsert_daily(well_id, metric, coverage, day_start, day_start + timedelta(days=1))
        daily = daily_by_date.get(end.date(), self._daily({}, end))
        baseline_configuration = self._current_baseline_configuration(well_id)
        baseline = self._stored_baseline(well_id, end)
        history = self._recent_days(well_id, end)
        current_configuration = self._configuration_from_daily(daily)
        episodes = self._rules(well_id, daily, baseline, coverage, history, baseline_configuration, current_configuration, end)
        for episode in episodes:
            self._upsert_episode(well_id, episode, end)
        self._clear_unseen_episodes(well_id, {item["code"] for item in episodes}, end)
        return {"well": well, "daily": daily, "baseline": baseline, "coverage": coverage, "episodes": episodes}

    def refresh_history(self, site_id: int, well_id: int, now: datetime | None = None) -> dict[str, Any]:
        """Nightly maintenance: refresh 45 days of daily data and derive one baseline."""
        end = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        start = end - timedelta(days=45)
        well = self._well(site_id, well_id)
        if self._is_currently_shutdown(well_id, end):
            self._suppress_shutdown_episodes(well_id, end)
            return {"well": well, "status": "skipped_shutdown", "baseline": self._stored_baseline(well_id, end), "coverage": {}}
        catalog = self._catalog(site_id, well["well_key"])
        series, coverage = self._series(well, catalog, start, end)
        daily_by_date = self._daily_by_date(series, start, end)
        for metric_date, metric in daily_by_date.items():
            day_start = datetime.combine(metric_date, datetime.min.time(), self.timezone)
            self._upsert_daily(well_id, metric, coverage, day_start, day_start + timedelta(days=1))
        current = daily_by_date.get(end.date(), self._daily({}, end))
        baseline = self._baseline_from_days(well_id, end, current)
        self._upsert_baseline(well_id, baseline, catalog, end)
        return {"well": well, "baseline": baseline, "coverage": coverage}

    def _well(self, site_id: int, well_id: int) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(text("""SELECT w.id, w.name, w.`key` well_key, s.`key` site_key
                FROM wells w JOIN sites s ON s.id=w.site_id
                WHERE w.id=:well_id AND w.site_id=:site_id AND LOWER(w.pump_type)='rod'"""), {"well_id": well_id, "site_id": site_id}).mappings().first()
        if not row:
            raise ValueError("Rod-pump well not found in the selected site.")
        return dict(row)

    def _is_currently_shutdown(self, well_id: int, now: datetime) -> bool:
        with self.engine.connect() as connection:
            return connection.execute(text("""SELECT EXISTS(
                SELECT 1 FROM well_shutdowns
                WHERE well_id=:well_id AND `start`<=:now
                  AND (`end` IS NULL OR `end`>:now)
            )"""), {"well_id": well_id, "now": now}).scalar() == 1

    def _catalog(self, site_id: int, well_key: str) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(text("""SELECT dp.data_point_name, dp.tag
                FROM data_points dp LEFT JOIN data_points parent ON parent.id=dp.facility_id
                WHERE dp.site_id=:site_id
                  AND COALESCE(parent.facility_name, dp.facility_name)=:well_key"""), {"site_id": site_id, "well_key": well_key}).mappings().all()
        by_name = {
            str(row["data_point_name"]).casefold(): {**dict(row), "tag": TAG_ALIASES.get(str(row["tag"]), str(row["tag"]))}
            for row in rows if row["data_point_name"] and row["tag"]
        }
        return {key: by_name[name.casefold()] for key, name in METRICS.items() if name.casefold() in by_name}

    def _series(self, well: dict[str, Any], catalog: dict[str, dict[str, Any]], start: datetime, end: datetime) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
        values: dict[str, list[dict[str, Any]]] = {}
        coverage: dict[str, Any] = {}
        target_keys: dict[str, list[str]] = defaultdict(list)
        for key in METRICS:
            point = catalog.get(key)
            if not point:
                values[key] = []; coverage[key] = {"status": "unavailable", "reason": "No tagged data point is assigned to this well."}; continue
            target = f"MI3.{well['site_key']}.{well['well_key'].replace(' ', '_')}.{point['tag']}"
            target_keys[target].append(key)
        if not target_keys:
            return values, coverage
        # Graphite's Render API accepts repeated target parameters.  Asking for
        # all well metrics together changes 16 round trips into one.
        params: list[tuple[str, str]] = [("target", target) for target in target_keys]
        params.extend([("from", start.strftime("%H:%M_%Y%m%d")), ("until", end.strftime("%H:%M_%Y%m%d")), ("format", "json"), ("noNullPoints", "true"), ("tz", str(self.timezone))])
        try:
            response = self.http_get(self.monitoring_url, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            returned = {str(item.get("target")): item for item in payload if item.get("target")}
            for target, keys in target_keys.items():
                item = returned.get(target, {})
                points = [{"time": datetime.fromtimestamp(point[1], self.timezone), "value": point[0]} for point in item.get("datapoints", []) if point[0] is not None]
                status = "available" if points else "empty"
                for key in keys:
                    values[key] = points
                    coverage[key] = {"status": status, "target": target, "count": len(points), "latest": points[-1]["time"].isoformat() if points else None}
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            for target, keys in target_keys.items():
                for key in keys:
                    values[key] = []
                    coverage[key] = {"status": "error", "target": target, "reason": str(exc)}
        return values, coverage

    def _daily_by_date(self, series: dict[str, list[dict[str, Any]]], start: datetime, end: datetime) -> dict[Any, dict[str, Any]]:
        """Aggregate each local operational day before deriving any baseline or event count."""
        dates = {row["time"].astimezone(self.timezone).date() for rows in series.values() for row in rows}
        dates.update({start.date(), end.date()})
        return {
            metric_date: self._daily({key: [row for row in rows if row["time"].astimezone(self.timezone).date() == metric_date] for key, rows in series.items()}, datetime.combine(metric_date, datetime.min.time(), self.timezone))
            for metric_date in dates
        }

    def _daily(self, series: dict[str, list[dict[str, Any]]], end: datetime) -> dict[str, Any]:
        latest = lambda key: series[key][-1]["value"] if series.get(key) else None
        numeric = lambda key: [float(row["value"]) for row in series.get(key, []) if _number(row["value"])]
        peak, minimum = latest("peak_load"), latest("min_load")
        states = [{"code": int(float(row["value"])), "observed_at": row["time"].isoformat()} for row in series.get("well_state_code", []) if _number(row["value"])]
        transitions = [
            {"from_state_code": left["code"], "to_state_code": right["code"], "observed_at": right["observed_at"]}
            for left, right in zip(states, states[1:]) if left["code"] != right["code"]
        ]
        return {"metric_date": end.date().isoformat(), "runtime_percent": latest("runtime"), "cycle_count": latest("cycles"), "peak_load": peak, "min_load": minimum, "load_range": float(peak) - float(minimum) if _number(peak) and _number(minimum) else None, "median_spm": _median(numeric("spm")), "stroke_length": _median(numeric("stroke_length")), "median_fillage": _median(numeric("fillage")), "fillage_setpoint": latest("fillage_setpoint"), "peak_load_setpoint": latest("peak_limit"), "min_load_setpoint": latest("min_limit"), "control_mode": latest("control_mode"), "operation_mode": latest("operation_mode"), "host_switch": latest("host_switch"), "pump_off_event_count": sum(event["to_state_code"] in PUMP_OFF_CODES for event in transitions), "malfunction_event_count": sum(event["to_state_code"] in MALFUNCTION_CODES for event in transitions), "state_events": transitions}

    def _baseline_from_days(self, well_id: int, end: datetime, current: dict[str, Any]) -> dict[str, Any]:
        """Build the historical baseline from daily values, never repeated 15-minute samples."""
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(text("SELECT * FROM rod_pump_health_daily_metrics WHERE well_id=:well_id AND metric_date>=:start AND metric_date<:end ORDER BY metric_date"), {"well_id": well_id, "start": (end-timedelta(days=30)).date(), "end": end.date()}).mappings().all()]
        summary = {}
        columns = {"runtime": "runtime_percent", "cycles": "cycle_count", "peak_load": "peak_load", "min_load": "min_load", "spm": "median_spm", "stroke_length": "stroke_length", "fillage": "median_fillage"}
        for key, column in columns.items():
            values = [float(row[column]) for row in rows if _number(row.get(column))]
            if len(values) >= 8: summary[key] = {"median": median(values), "count": len(values)}
        configuration = {key: current.get(column) for key, column in {"control_mode": "control_mode", "operation_mode": "operation_mode", "host_switch": "host_switch", "peak_limit": "peak_load_setpoint", "min_limit": "min_load_setpoint", "fillage_setpoint": "fillage_setpoint"}.items() if current.get(column) is not None}
        return {"status": "stable" if len(summary) >= 3 else "insufficient_data", "start": end-timedelta(days=30), "end": end, "summary": summary, "configuration": configuration}

    def _stored_baseline(self, well_id: int, now: datetime) -> dict[str, Any]:
        with self.engine.connect() as connection:
            row = connection.execute(text("SELECT * FROM rod_pump_health_historical_baselines WHERE well_id=:well_id"), {"well_id": well_id}).mappings().first()
        if not row:
            return {"status": "insufficient_data", "start": None, "end": None, "summary": {}, "configuration": {}}
        row = dict(row)
        try:
            summary = json.loads(row["baseline_summary_json"])
            configuration = json.loads(row["observed_configuration_json"])
        except (TypeError, json.JSONDecodeError):
            summary, configuration = {}, {}
        return {"status": row["baseline_status"], "start": row["baseline_start_at"], "end": row["baseline_end_at"], "summary": summary, "configuration": configuration}

    def _configuration_from_daily(self, daily: dict[str, Any]) -> dict[str, Any]:
        mapping = {"control_mode": "control_mode", "operation_mode": "operation_mode", "host_switch": "host_switch", "peak_limit": "peak_load_setpoint", "min_limit": "min_load_setpoint", "fillage_setpoint": "fillage_setpoint"}
        return {key: daily[column] for key, column in mapping.items() if daily.get(column) is not None}

    def _rules(self, well_id: int, daily: dict[str, Any], baseline: dict[str, Any], coverage: dict[str, Any], history: list[dict[str, Any]], baseline_configuration: dict[str, Any], current_configuration: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        episodes = []
        # Only telemetry required by the implemented Phase 1 rules can open a
        # data-quality episode.  Optional SAM1 capabilities are reported in
        # coverage but must not make a well look unhealthy.
        missing = [key for key in CORE_SAM1_METRICS if coverage.get(key, {}).get("status") != "available"]
        if missing: episodes.append(_episode("diagnostic_data_quality", "advisory", "Required telemetry is unavailable or empty.", {"missing_metrics": missing}))
        if daily["malfunction_event_count"] >= 2: episodes.append(_episode("recurring_malfunction_pattern", "high", "Repeated visible SAM1 malfunction transitions were observed.", {"event_count": daily["malfunction_event_count"], "events": daily["state_events"]}))
        runtimes = [float(row["runtime_percent"]) for row in history if _number(row.get("runtime_percent"))]
        cycles = [float(row["cycle_count"]) for row in history if _number(row.get("cycle_count"))]
        base_runtime = baseline["summary"].get("runtime", {}).get("median")
        base_cycles = baseline["summary"].get("cycles", {}).get("median")
        if len(runtimes) >= 5 and len(cycles) >= 5 and base_runtime and base_cycles and median(runtimes) < float(base_runtime) - max(10, float(base_runtime) * .15) and median(cycles) > float(base_cycles) * 1.25:
            episodes.append(_episode("increasing_cycling_declining_runtime", "advisory", "Runtime is below the derived baseline while daily cycling is elevated.", {"runtime_7d": median(runtimes), "runtime_baseline": base_runtime, "cycles_7d": median(cycles), "cycles_baseline": base_cycles}))
        peak = daily["peak_load"]; limit = daily["peak_load_setpoint"]; base = baseline["summary"].get("peak_load", {}).get("median")
        if _number(peak) and _number(limit) and base and float(peak) > base * 1.08 and (float(limit)-float(peak))/float(limit) < .2:
            episodes.append(_episode("structural_load_creep", "advisory", "Peak polished-rod load is elevated relative to the derived baseline and near its current controller limit.", {"peak_load": peak, "baseline_peak": base, "peak_limit": limit}))
        if baseline_configuration and current_configuration and baseline_configuration != current_configuration:
            episodes.append(_episode("configuration_or_override_change", "advisory", "Observed controller configuration differs from the historical baseline.", {"baseline": baseline_configuration, "current": current_configuration}))
        return episodes

    def _recent_days(self, well_id: int, end: datetime) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text("SELECT * FROM rod_pump_health_daily_metrics WHERE well_id=:well_id AND metric_date>=:start AND metric_date<=:end ORDER BY metric_date"), {"well_id": well_id, "start": (end-timedelta(days=6)).date(), "end": end.date()}).mappings().all()]

    def _current_baseline_configuration(self, well_id: int) -> dict[str, Any]:
        with self.engine.connect() as connection:
            value = connection.execute(text("SELECT observed_configuration_json FROM rod_pump_health_historical_baselines WHERE well_id=:well_id"), {"well_id": well_id}).scalar()
        try: return json.loads(value) if value else {}
        except (TypeError, json.JSONDecodeError): return {}

    def _upsert_daily(self, well_id: int, daily: dict[str, Any], coverage: dict[str, Any], start: datetime, end: datetime) -> None:
        columns = {**daily, "well_id": well_id, "metric_coverage_json": json.dumps(coverage), "source_window_start": start, "source_window_end": end}
        columns.pop("state_events")
        _upsert(self.engine, "rod_pump_health_daily_metrics", columns, ["well_id", "metric_date"])

    def _upsert_baseline(self, well_id: int, baseline: dict[str, Any], catalog: dict[str, Any], now: datetime) -> None:
        _upsert(self.engine, "rod_pump_health_historical_baselines", {"well_id": well_id, "last_refreshed_at": now, "observed_configuration_json": json.dumps(baseline["configuration"]), "expected_metric_catalog_json": json.dumps(catalog), "baseline_start_at": baseline["start"], "baseline_end_at": baseline["end"], "baseline_status": baseline["status"], "baseline_summary_json": json.dumps(baseline["summary"])}, ["well_id"])

    def _upsert_episode(self, well_id: int, episode: dict[str, Any], now: datetime) -> None:
        with self.engine.begin() as connection:
            row = connection.execute(text("SELECT id, evidence_json FROM rod_pump_health_episodes WHERE well_id=:well_id AND diagnosis_code=:code AND state IN ('candidate','active') ORDER BY id DESC LIMIT 1"), {"well_id": well_id, "code": episode["code"]}).mappings().first()
            evidence = [{"observed_at": now.isoformat(), **episode["evidence"]}]
            review = self._review({"diagnosis_code": episode["code"], "severity": episode["severity"], "summary": episode["summary"], "evidence": episode["evidence"]})
            evidence.append({"observed_at": now.isoformat(), "llm_review": review})
            if row:
                connection.execute(text("UPDATE rod_pump_health_episodes SET severity=:severity, state='active', last_seen_at=:now, summary=:summary, evidence_json=:evidence, updated_at=:now WHERE id=:id"), {"id": row["id"], "severity": episode["severity"], "now": now, "summary": episode["summary"], "evidence": json.dumps(evidence)})
            else:
                connection.execute(text("INSERT INTO rod_pump_health_episodes (well_id,diagnosis_code,severity,state,opened_at,last_seen_at,summary,recommended_action,evidence_json,created_at,updated_at) VALUES (:well_id,:code,:severity,'active',:now,:now,:summary,'Review persisted evidence.',:evidence,:now,:now)"), {"well_id": well_id, "code": episode["code"], "severity": episode["severity"], "now": now, "summary": episode["summary"], "evidence": json.dumps(evidence)})

    def _clear_unseen_episodes(self, well_id: int, seen: set[str], now: datetime) -> None:
        with self.engine.begin() as connection:
            rows = connection.execute(text("SELECT id, diagnosis_code FROM rod_pump_health_episodes WHERE well_id=:well_id AND state='active'"), {"well_id": well_id}).mappings().all()
            for row in rows:
                if row["diagnosis_code"] not in seen:
                    connection.execute(text("UPDATE rod_pump_health_episodes SET state='cleared', cleared_at=:now, updated_at=:now WHERE id=:id"), {"id": row["id"], "now": now})

    def suppress_out_of_scope_data_quality_episode(self, well_id: int, now: datetime | None = None) -> None:
        """Close an old data-quality episode when the well leaves the configured scope."""
        now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        with self.engine.begin() as connection:
            connection.execute(text("""UPDATE rod_pump_health_episodes
                SET state='cleared', cleared_at=:now,
                    suppression_reason='Well is outside the rod_pump_monitoring report configuration scope.',
                    updated_at=:now
                WHERE well_id=:well_id AND diagnosis_code='diagnostic_data_quality' AND state IN ('candidate','active')"""), {"well_id": well_id, "now": now})

    def _suppress_shutdown_episodes(self, well_id: int, now: datetime) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("""UPDATE rod_pump_health_episodes
                SET state='cleared', cleared_at=:now,
                    suppression_reason='Well is currently shut down (well_shutdowns).', updated_at=:now
                WHERE well_id=:well_id AND state IN ('candidate','active')"""), {"well_id": well_id, "now": now})

    def _review(self, packet: dict[str, Any]) -> dict[str, Any]:
        if self.reviewer is None:
            return {"status": "template", "alert_summary": packet["summary"], "data_limitations": ["LLM reviewer is unavailable."]}
        try: return {"status": "ok", **self.reviewer(packet)}
        except Exception as exc: return {"status": "fallback", "alert_summary": packet["summary"], "data_limitations": [f"LLM reviewer failed: {exc}"]}


def _upsert(engine: Engine, table: str, values: dict[str, Any], keys: list[str]) -> None:
    names = list(values); updates = ", ".join(f"{name}=VALUES({name})" for name in names if name not in keys)
    with engine.begin() as connection: connection.execute(text(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join(':'+name for name in names)}) ON DUPLICATE KEY UPDATE {updates}"), values)

def _number(value: Any) -> bool:
    try: float(value); return True
    except (TypeError, ValueError): return False

def _median(values: list[float]) -> float | None: return median(values) if values else None
def _episode(code: str, severity: str, summary: str, evidence: dict[str, Any]) -> dict[str, Any]: return {"code": code, "severity": severity, "summary": summary, "evidence": evidence}


def main() -> None:
    """Run current-condition Phase 1 evaluation for all rod wells or one well."""
    _run_cli("evaluate")


def refresh_history_main() -> None:
    """Run the nightly 45-day baseline refresh for all rod wells or one well."""
    _run_cli("refresh_history")


def _run_cli(operation: str) -> None:
    import argparse
    from omai.clients.rod_pump_analysis_client import RodPumpAnalysisClient
    from omai.config.settings import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--site-id", type=int, required=True)
    parser.add_argument("--well-id", type=int, action="append")
    args = parser.parse_args()
    client = RodPumpAnalysisClient.from_settings(Settings.from_env())
    with client.engine.connect() as connection:
        related_entities = connection.execute(text("SELECT related_entities FROM report_configurations WHERE site_id=:site_id AND function_name='rod_pump_monitoring'"), {"site_id": args.site_id}).scalar()
        try:
            configured_well_ids = {int(well_id) for well_id in json.loads(related_entities or "{}").get("well_ids", [])}
        except (TypeError, ValueError, json.JSONDecodeError):
            configured_well_ids = set()
        rows = [dict(row) for row in connection.execute(text("SELECT id, `key` well_key FROM wells WHERE site_id=:site_id"), {"site_id": args.site_id}).mappings().all()]
    # The report configuration is authoritative.  Do not fall back to all rod
    # wells when it is absent or empty: those wells are outside this pipeline.
    wanted = configured_well_ids.intersection(args.well_id or configured_well_ids)
    evaluator = RodPumpHealthEvaluator(client.engine, client.monitoring_url, str(client.timezone), client.http_get, client.health_reviewer)
    for row in rows:
        well_id = int(row["id"])
        if well_id not in configured_well_ids:
            evaluator.suppress_out_of_scope_data_quality_episode(well_id)
            continue
        if well_id not in wanted:
            continue
        getattr(evaluator, operation)(args.site_id, well_id)


if __name__ == "__main__":
    main()

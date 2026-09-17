from __future__ import annotations

import json
import math
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
# SAM1 states 7–10 are all pumping states. In particular, state 9 is
# Pumping Timed Mode, not a pump-off state.
PUMPING_STATE_CODES = {7, 8, 9, 10}
PUMP_OFF_CODES = {31, 35, 36, 37, 41, 42, 44, *range(46, 66)}
# A zero-width transition is commonly a card-resolution/extraction floor, not
# a usable normal reference. Valve-pattern rules require at least this clean
# reference width before comparing relative changes.
MIN_MEANINGFUL_TRANSITION_BASELINE_PERCENT = 0.25
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
        # Current-card extraction is deliberately part of the existing
        # evaluator.  It never creates a separate polling job.
        card_features = self._refresh_card_features(well, catalog, end - timedelta(days=2), end)
        card_episodes = self._card_rules(well_id, card_features, daily, end)
        episodes.extend(card_episodes)
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
        # The nightly history refresh is also the bounded card backfill and
        # clean-card baseline refresh.  Archive rows cover cards pruned from
        # the live tables.
        self._refresh_card_features(well, catalog, end - timedelta(days=90), end)
        self._refresh_card_baselines(well_id, end)
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

    def _refresh_card_features(self, well: dict[str, Any], catalog: dict[str, dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Persist one selected *current* dynograph per pull and side.

        Archive data is used for retained history; a live pull takes precedence
        because it can contain a newly arrived card that has not been archived
        yet.  Where five cards are present, the same load-curve medoid rule as
        the archive service is used; no curves are averaged.
        """
        well_id = int(well["id"])
        sources: dict[tuple[int, str], dict[str, Any]] = {}
        with self.engine.connect() as connection:
            archived = connection.execute(text("""SELECT pull_timestamp, down_hole, card_type, source_card_id,
                data_points, info, selection_metadata FROM rod_pump_card_archive
                WHERE well_id=:well_id AND pull_timestamp>=:start AND pull_timestamp<=:end
                  AND LOWER(card_type)='current'"""), {"well_id": well_id, "start": int(start.timestamp()), "end": int(end.timestamp())}).mappings().all()
            for row in archived:
                item = dict(row); side = "downhole" if item["down_hole"] else "surface"
                sources[(int(item["pull_timestamp"]), side)] = item
            live = connection.execute(text("""SELECT d.pull_timestamp, c.id source_card_id, c.down_hole, c.card_type,
                c.data_points, c.info FROM rod_pump_monitoring_data d
                JOIN rod_pump_monitoring_data_cards dc ON dc.rod_pump_monitoring_data_id=d.id
                JOIN rod_pump_cards c ON c.id=dc.rod_pump_card_id
                WHERE d.well_id=:well_id AND d.pull_timestamp>=:start AND d.pull_timestamp<=:end
                  AND LOWER(c.card_type)='current' ORDER BY d.pull_timestamp,c.down_hole,c.id"""), {"well_id": well_id, "start": int(start.timestamp()), "end": int(end.timestamp())}).mappings().all()
        grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for row in live:
            item = dict(row); grouped[(int(item["pull_timestamp"]), "downhole" if item["down_hole"] else "surface")].append(item)
        for key, candidates in grouped.items():
            selected = _select_current_card_medoid(candidates)
            if selected is not None:
                sources[key] = selected
        state_samples = self._well_state_code_samples(well, catalog, start - timedelta(minutes=30), end + timedelta(minutes=15))
        result = []
        for (pull_timestamp, side), source in sources.items():
            feature = _card_feature_row(source, well_id, pull_timestamp, side, self.timezone)
            feature = _with_card_state_context(feature, state_samples)
            _upsert(self.engine, "rod_pump_health_card_features", feature, ["well_id", "pull_timestamp", "card_side", "card_type"])
            result.append(feature)
        return result

    def _well_state_code_samples(self, well: dict[str, Any], catalog: dict[str, dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Fetch one Well State Code series for the whole card window per well."""
        point = catalog.get("well_state_code")
        if not point:
            return []
        target = f"MI3.{well['site_key']}.{well['well_key'].replace(' ', '_')}.{point['tag']}"
        params = [("target", target), ("from", start.strftime("%H:%M_%Y%m%d")), ("until", end.strftime("%H:%M_%Y%m%d")), ("format", "json"), ("noNullPoints", "true"), ("tz", str(self.timezone))]
        try:
            response = self.http_get(self.monitoring_url, params=params, timeout=20)
            response.raise_for_status()
            payload = response.json()
            item = next((row for row in payload if str(row.get("target")) == target), None)
            if not item:
                return []
            return [{"time": datetime.fromtimestamp(point[1], self.timezone), "code": int(float(point[0]))}
                    for point in item.get("datapoints", []) if len(point) >= 2 and _number(point[0]) and point[1] is not None]
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return []

    def _refresh_card_baselines(self, well_id: int, end: datetime) -> None:
        """Store newest robust per-side clean-card reference, never an average card."""
        start = end - timedelta(days=30)
        with self.engine.connect() as connection:
            cards = [dict(row) for row in connection.execute(text("""SELECT * FROM rod_pump_health_card_features
                WHERE well_id=:well_id AND observed_at>=:start AND observed_at<:end
                  AND card_type='current' ORDER BY observed_at"""), {"well_id": well_id, "start": start, "end": end}).mappings().all()]
            # A paraffin chart note records observed paraffin, not necessarily
            # treatment. Cards within ±3 days can reflect that condition and
            # must not establish the well's clean mechanical baseline. LOWER
            # makes the required "paraffin" match case-insensitive in MariaDB.
            paraffin_rows = connection.execute(text("""SELECT id,x_axis_value FROM chart_notes
                WHERE object_id=:well_id AND object_type='RodPumpOilWell'
                  AND LOWER(COALESCE(note,'')) LIKE '%paraffin%'
                  AND x_axis_value>=:event_start AND x_axis_value<=:event_end
                ORDER BY x_axis_value"""), {"well_id": well_id, "event_start": start - timedelta(days=3), "event_end": end + timedelta(days=3)}).mappings().all()
        paraffin_events = [_as_timezone_datetime(row["x_axis_value"], self.timezone) for row in paraffin_rows]
        for side in ("surface", "downhole"):
            rejected: defaultdict[str, int] = defaultdict(int); eligible = []
            for card in (item for item in cards if item["card_side"] == side):
                if card["quality_status"] != "valid" or not card["orientation"]:
                    rejected["invalid_or_unknown_orientation"] += 1; continue
                state_context = _json_value(card["feature_json"], {}).get("state_context", {})
                if not state_context.get("eligible"):
                    rejected[state_context.get("reason") or "state_context_unavailable"] += 1; continue
                if any(_within_paraffin_event_window(_as_timezone_datetime(card["observed_at"], self.timezone), event) for event in paraffin_events):
                    rejected["paraffin_event_window"] += 1; continue
                eligible.append(card)
            feature_sets = [_json_value(card["feature_json"], {}) for card in eligible]
            summary = _feature_baseline(feature_sets)
            status = "stable" if len(eligible) >= 8 else "insufficient_data"
            _upsert(self.engine, "rod_pump_health_card_baselines", {
                "well_id": well_id, "card_side": side, "card_type": "current", "last_refreshed_at": end,
                "baseline_start_at": start, "baseline_end_at": end, "baseline_status": status,
                "eligible_card_count": len(eligible), "feature_summary_json": json.dumps(summary),
                "exclusion_summary_json": json.dumps(dict(rejected)),
            }, ["well_id", "card_side", "card_type"])

    def _card_rules(self, well_id: int, features: list[dict[str, Any]], daily: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
        """Phase 2 stays review-only; quality/orientation is a hard gate."""
        # A card's own operational date, not the evaluator's current date,
        # determines whether it is comparable. This matters when the latest
        # retained card was captured yesterday but evaluation runs today.
        normal_features = self._normal_state_card_rows(well_id, features)
        valid_downhole = [row for row in normal_features if row["card_side"] == "downhole" and row["quality_status"] == "valid" and row["orientation"]]
        valid_surface = [row for row in normal_features if row["card_side"] == "surface" and row["quality_status"] == "valid" and row["orientation"]]
        if not valid_downhole and not valid_surface:
            return []
        baselines = self._card_baselines(well_id)
        result = []
        down = [_json_value(row["feature_json"], {}) for row in valid_downhole]
        surface = [_json_value(row["feature_json"], {}) for row in valid_surface]
        dbase = baselines.get("downhole", {}); sbase = baselines.get("surface", {})
        if _qualifying_count(down, dbase, "pickup_position_percent", 0.15) >= 4:
            result.append(_episode("gas_interference", "advisory", "Delayed downhole pickup pattern requires chart review.", {"review_only": True, "feature": "pickup_position_percent", "qualifying_valid_cards": 4, **_card_metric_evidence(valid_downhole, dbase, "pickup_position_percent")}, state="candidate"))
        traveling_count = _qualifying_count(down, dbase, "upstroke_transition_width", 0.20)
        if _meaningful_transition_baseline(dbase, "upstroke_transition_width") and traveling_count >= 3:
            result.append(_episode("traveling_valve_leak_or_delayed_closure", "advisory", "Downhole upstroke transition differs from the clean-card reference.", {"review_only": True, "feature": "upstroke_transition_width", "qualifying_cards_of_latest_five": traveling_count, **_card_metric_evidence(valid_downhole, dbase, "upstroke_transition_width")}, state="candidate"))
        standing_count = _qualifying_count(down, dbase, "bottom_transition_width", 0.20)
        if _meaningful_transition_baseline(dbase, "bottom_transition_width") and standing_count >= 3:
            result.append(_episode("standing_valve_leak_or_delayed_opening", "advisory", "Downhole bottom transition differs from the clean-card reference.", {"review_only": True, "feature": "bottom_transition_width", "qualifying_cards_of_latest_five": standing_count, **_card_metric_evidence(valid_downhole, dbase, "bottom_transition_width")}, state="candidate"))
        if _qualifying_count(surface, sbase, "load_spread", 0.15) >= 5:
            result.append(_episode("progressive_mechanical_friction", "advisory", "Surface-card loading spread is elevated relative to the clean-card reference.", {"review_only": True, "feature": "load_spread", "daily_peak_load": daily.get("peak_load"), "daily_min_load": daily.get("min_load"), **_card_metric_evidence(valid_surface, sbase, "load_spread")}, state="candidate"))
        # These two trends need more than the evaluator's two-day pull window;
        # use the persisted, quality-gated feature history rather than fetching
        # a second card source.
        history = self._normal_state_card_rows(well_id, self._recent_valid_card_features(well_id, now - timedelta(days=14), now))
        surface_history = [_json_value(row["feature_json"], {}) for row in history if row["card_side"] == "surface"]
        down_history = [_json_value(row["feature_json"], {}) for row in history if row["card_side"] == "downhole"]
        if len(surface_history) >= 8 and _qualifying_count(surface_history, sbase, "loading_asymmetry", .20) >= 5:
            result.append(_episode("loading_balance_change_suspected", "advisory", "Surface-card loading asymmetry differs from the clean-card reference.", {"review_only": True, "feature": "loading_asymmetry", "comparison_window_days": 14, "qualifying_valid_cards": _qualifying_count(surface_history, sbase, "loading_asymmetry", .20), **_card_metric_evidence([row for row in history if row["card_side"] == "surface"], sbase, "loading_asymmetry")}, state="candidate"))
        fillage_history = self._daily_history(well_id, now - timedelta(days=10), now)
        base_fillage = self._stored_baseline(well_id, now).get("summary", {}).get("fillage", {}).get("median")
        recent_fillage = [float(row["median_fillage"]) for row in fillage_history if _number(row.get("median_fillage"))]
        if len(recent_fillage) >= 10 and _number(base_fillage) and median(recent_fillage[-7:]) < float(base_fillage) * .9 and _low_qualifying_count(down_history, dbase, "normalized_area", .10) >= 3:
            result.append(_episode("declining_pump_performance", "advisory", "Pump Fillage and downhole-card area are below their clean reference patterns.", {"review_only": True, "feature": "normalized_area", "fillage_7_day_median": median(recent_fillage[-7:]), "fillage_30_day_median": base_fillage, "qualifying_valid_cards": _low_qualifying_count(down_history, dbase, "normalized_area", .10), **_card_metric_evidence([row for row in history if row["card_side"] == "downhole"], dbase, "normalized_area")}, state="candidate"))
        return result

    def _card_baselines(self, well_id: int) -> dict[str, dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(text("SELECT card_side,baseline_status,feature_summary_json FROM rod_pump_health_card_baselines WHERE well_id=:well_id AND card_type='current'"), {"well_id": well_id}).mappings().all()
        return {row["card_side"]: _json_value(row["feature_summary_json"], {}) if row["baseline_status"] == "stable" else {} for row in rows}

    def _recent_valid_card_features(self, well_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text("""SELECT * FROM rod_pump_health_card_features
                WHERE well_id=:well_id AND observed_at>=:start AND observed_at<=:end
                  AND card_type='current' AND quality_status='valid' AND orientation IS NOT NULL
                ORDER BY observed_at"""), {"well_id": well_id, "start": start, "end": end}).mappings().all()]

    def _normal_state_card_rows(self, well_id: int, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Retain only cards with a persisted stable-pumping state window."""
        return [row for row in rows if _json_value(row.get("feature_json"), {}).get("state_context", {}).get("eligible") is True]

    def _daily_history(self, well_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text("""SELECT * FROM rod_pump_health_daily_metrics
                WHERE well_id=:well_id AND metric_date>=:start AND metric_date<=:end ORDER BY metric_date"""), {"well_id": well_id, "start": start.date(), "end": end.date()}).mappings().all()]

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
            episode_state = episode.get("state", "active")
            evidence = [{"observed_at": now.isoformat(), **episode["evidence"]}]
            review = self._review({"diagnosis_code": episode["code"], "severity": episode["severity"], "summary": episode["summary"], "evidence": episode["evidence"]})
            evidence.append({"observed_at": now.isoformat(), "llm_review": review})
            if row:
                connection.execute(text("UPDATE rod_pump_health_episodes SET severity=:severity, state=:state, last_seen_at=:now, summary=:summary, evidence_json=:evidence, updated_at=:now WHERE id=:id"), {"id": row["id"], "severity": episode["severity"], "state": episode_state, "now": now, "summary": episode["summary"], "evidence": json.dumps(evidence)})
            else:
                connection.execute(text("INSERT INTO rod_pump_health_episodes (well_id,diagnosis_code,severity,state,opened_at,last_seen_at,summary,recommended_action,evidence_json,created_at,updated_at) VALUES (:well_id,:code,:severity,:state,:now,:now,:summary,'Review persisted evidence.',:evidence,:now,:now)"), {"well_id": well_id, "code": episode["code"], "severity": episode["severity"], "state": episode_state, "now": now, "summary": episode["summary"], "evidence": json.dumps(evidence)})

    def _clear_unseen_episodes(self, well_id: int, seen: set[str], now: datetime) -> None:
        with self.engine.begin() as connection:
            rows = connection.execute(text("SELECT id, diagnosis_code FROM rod_pump_health_episodes WHERE well_id=:well_id AND state IN ('active','candidate')"), {"well_id": well_id}).mappings().all()
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

def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default

def _as_timezone_datetime(value: Any, timezone: ZoneInfo) -> datetime:
    """Normalize chart-note timestamps returned as either DATETIME or text."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)

def _within_paraffin_event_window(card_time: datetime, event_time: datetime) -> bool:
    """A paraffin observation excludes baseline cards from three days before through after it."""
    return event_time - timedelta(days=3) <= card_time <= event_time + timedelta(days=3)

def _select_current_card_medoid(cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Python equivalent of RodPumpCardArchiveService::selectMedoid."""
    candidates = []
    for row in cards:
        points = _json_value(row.get("data_points"), [])
        loads = [float(item["load"]) for item in points if isinstance(item, dict) and _number(item.get("load"))]
        if len(loads) < 2:
            continue
        low, high = min(loads), max(loads)
        samples = []
        for index in range(64):
            position = index * (len(loads) - 1) / 63
            left, right = math.floor(position), math.ceil(position)
            value = loads[left] + (loads[right] - loads[left]) * (position - left)
            samples.append((value - low) / (high - low) if high > low else 0.0)
        candidates.append((row, samples))
    if not candidates:
        return None
    selected, selected_score = None, None
    for row, curve in candidates:
        score = sum(sum(abs(left - right) for left, right in zip(curve, other)) / len(curve) for _, other in candidates)
        if selected is None or score < selected_score or (score == selected_score and row["source_card_id"] < selected["source_card_id"]):
            selected, selected_score = dict(row), score
    selected["selection_metadata"] = {"algorithm": "current-card-medoid-v1", "candidate_card_count": len(candidates), "medoid_distance": round(selected_score, 8)}
    return selected

def _card_feature_row(source: dict[str, Any], well_id: int, pull_timestamp: int, side: str, timezone: ZoneInfo) -> dict[str, Any]:
    points = _json_value(source.get("data_points"), [])
    info = _json_value(source.get("info"), {})
    metadata = _json_value(source.get("selection_metadata"), source.get("selection_metadata") or {})
    values = [(float(item["position"]), float(item["load"])) for item in points if isinstance(item, dict) and _number(item.get("position")) and _number(item.get("load"))]
    reasons: list[str] = []
    if len(values) < 16: reasons.append("insufficient_numeric_points")
    positions, loads = zip(*values) if values else ((), ())
    position_span = max(positions) - min(positions) if positions else 0
    load_span = max(loads) - min(loads) if loads else 0
    if position_span <= 0: reasons.append("zero_position_span")
    if load_span <= 0: reasons.append("zero_load_span")
    orientation, orientation_reason = _verified_orientation(info, list(positions))
    if not orientation: reasons.append(orientation_reason or "orientation_unverified")
    quality = "invalid" if any(reason in reasons for reason in ("insufficient_numeric_points", "zero_position_span", "zero_load_span")) else ("orientation_unknown" if not orientation else "valid")
    features: dict[str, Any] = {"point_count": len(values), "normalized_stroke_span": 100.0 if position_span > 0 else None, "raw_load_range": load_span}
    if values and position_span > 0 and load_span > 0:
        normalized = [((position - min(positions)) / position_span * 100, (load - min(loads)) / load_span) for position, load in values]
        if orientation == "verified_upstroke_first":
            turning = max(range(len(normalized)), key=lambda index: normalized[index][0])
            up, down = normalized[:turning + 1], normalized[turning:]
        elif orientation == "verified_downstroke_first":
            turning = min(range(len(normalized)), key=lambda index: normalized[index][0])
            down, up = normalized[:turning + 1], normalized[turning:]
        else:
            up, down = [], []
        features.update(_curve_features(normalized, up, down, side))
    return {"well_id": well_id, "pull_timestamp": pull_timestamp, "card_side": side, "card_type": "current", "source_card_id": source.get("source_card_id"), "observed_at": datetime.fromtimestamp(pull_timestamp, timezone), "quality_status": quality, "quality_reasons_json": json.dumps(reasons), "orientation": orientation, "raw_position_min": min(positions) if positions else None, "raw_position_max": max(positions) if positions else None, "raw_load_min": min(loads) if loads else None, "raw_load_max": max(loads) if loads else None, "feature_json": json.dumps(features), "selection_metadata_json": json.dumps(metadata)}

def _with_card_state_context(feature: dict[str, Any], state_samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach eligibility for 15 minutes before and 10 minutes after a card."""
    result = dict(feature)
    features = _json_value(result["feature_json"], {})
    observed_at = result["observed_at"]
    features["state_context"] = _stable_pumping_context(observed_at, state_samples)
    result["feature_json"] = json.dumps(features)
    return result

def _stable_pumping_context(card_time: datetime, samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify an observed pumping run covers card −15 minutes through +10.

    A state is known to hold between observations only when the two consecutive
    observations have the *same* code. Two 7 observations, for example,
    establish Pumping Normal for all intervening time irrespective of the gap
    length. A change from 7 to 31 (or 7 to any other code) leaves the interval
    between them uncertain, so a card in that interval is not eligible.
    """
    before_minutes, after_minutes = 15, 10
    ordered = sorted((row for row in samples if isinstance(row.get("time"), datetime) and _number(row.get("code"))), key=lambda row: row["time"])
    start, end = card_time - timedelta(minutes=before_minutes), card_time + timedelta(minutes=after_minutes)
    context = {"window_before_minutes": before_minutes, "window_after_minutes": after_minutes, "window_start_at": start.isoformat(), "window_end_at": end.isoformat(), "eligible": False}
    anchor_index = next((index for index in range(len(ordered) - 1, -1, -1) if ordered[index]["time"] <= start), None)
    if anchor_index is None or anchor_index >= len(ordered) - 1:
        return {**context, "reason": "well_state_code_window_incomplete"}
    # Each adjacent pair is the evidence for the full period it bounds. A
    # differing pair does not prove when the transition occurred, so none of
    # that interval is eligible even if both endpoint codes are pumping codes.
    last_index = anchor_index
    for index in range(anchor_index, len(ordered) - 1):
        left, right = ordered[index], ordered[index + 1]
        left_code, right_code = int(left["code"]), int(right["code"])
        if left_code != right_code:
            return {**context, "state_at_card": left_code, "previous_state_code": left_code, "next_state_code": right_code, "state_transition_start_at": left["time"].isoformat(), "state_transition_end_at": right["time"].isoformat(), "reason": "well_state_code_transition_uncertain"}
        if left_code not in PUMPING_STATE_CODES:
            return {**context, "state_at_card": left_code, "non_pumping_state_code": left_code, "non_pumping_state_at": left["time"].isoformat(), "reason": "non_pumping_well_state_within_stable_pumping_window"}
        last_index = index + 1
        if right["time"] >= end:
            run_start_index = anchor_index
            while run_start_index > 0 and int(ordered[run_start_index - 1]["code"]) == left_code and int(ordered[run_start_index - 1]["code"]) in PUMPING_STATE_CODES:
                run_start_index -= 1
            return {**context, "eligible": True, "state_at_card": left_code, "pumping_run_started_at": ordered[run_start_index]["time"].isoformat(), "confirmation_state_code": int(right["code"]), "confirmation_at": right["time"].isoformat()}
    return {**context, "reason": "well_state_code_window_incomplete"}

def _verified_orientation(info: dict[str, Any], positions: list[float]) -> tuple[str | None, str | None]:
    """Use explicit metadata when available, otherwise validate one traversal.

    SAM1 cards do not currently carry an orientation field.  The ordered
    position vector is nevertheless an objective traversal record: exactly one
    min→max→min cycle is verified as upstroke-first and exactly one
    max→min→max cycle as downstroke-first.  We reject anything that does not
    meet those shape/monotonicity checks; this is not an orientation guess.
    """
    value = str(info.get("orientation") or info.get("traversal_orientation") or info.get("stroke_direction") or "").strip().casefold()
    if value in {"upstroke_first", "upstroke-first", "verified_upstroke_first"}: return "verified_upstroke_first", None
    if value in {"downstroke_first", "downstroke-first", "verified_downstroke_first"}: return "verified_downstroke_first", None
    if len(positions) < 16:
        return None, "insufficient_points_for_orientation"
    low, high = min(positions), max(positions)
    span = high - low
    if span <= 0:
        return None, "zero_position_span"
    tolerance = span * .08
    first, last = positions[0], positions[-1]
    max_index, min_index = positions.index(high), positions.index(low)
    # The endpoint must be near the same end of the stroke and the turning
    # point must be internal. This prevents partial cards from becoming a
    # purported stroke simply because their position happened to rise/fall.
    def monotonic_ratio(values: list[float], rising: bool) -> float:
        moves = [right - left for left, right in zip(values, values[1:]) if abs(right - left) > span * .001]
        if not moves:
            return 0.0
        return sum((move > 0) == rising for move in moves) / len(moves)
    if first <= low + tolerance and last <= low + tolerance and 2 <= max_index <= len(positions) - 3:
        if monotonic_ratio(positions[:max_index + 1], True) >= .90 and monotonic_ratio(positions[max_index:], False) >= .90:
            return "verified_upstroke_first", None
    if first >= high - tolerance and last >= high - tolerance and 2 <= min_index <= len(positions) - 3:
        if monotonic_ratio(positions[:min_index + 1], False) >= .90 and monotonic_ratio(positions[min_index:], True) >= .90:
            return "verified_downstroke_first", None
    return None, "position_traversal_is_not_one_verified_stroke"

def _curve_features(curve: list[tuple[float, float]], up: list[tuple[float, float]], down: list[tuple[float, float]], side: str) -> dict[str, float]:
    # All metrics are dimensionless after load normalization except raw_load_range.
    area = abs(sum(left[0] * right[1] - right[0] * left[1] for left, right in zip(curve, curve[1:] + curve[:1]))) / 200
    widths = [abs(left[1] - right[1]) for left, right in zip(up, reversed(down))]
    load_spread = max(widths) if widths else 0.0
    pickup = next((position for position, load in up if load >= .2), 100.0)
    release = next((position for position, load in down if load <= .2), 0.0)
    top = [load for position, load in curve if position >= 85]
    bottom = [load for position, load in curve if position <= 15]
    return {"normalized_area": area, "load_spread": load_spread, "loop_width": load_spread, "pickup_position_percent": pickup, "load_release_position_percent": release, "upstroke_transition_width": _transition_width(up, .2, .8), "bottom_transition_width": _transition_width(down, .8, .2), "top_transition_width": max(top) - min(top) if len(top) > 1 else 0.0, "residual_bottom_load": _median(bottom) or 0.0, "loading_asymmetry": abs((_median([value for _, value in up]) or 0) - (_median([value for _, value in down]) or 0))}

def _transition_width(branch: list[tuple[float, float]], low: float, high: float) -> float:
    first = next((position for position, load in branch if load >= low), None)
    last = next((position for position, load in branch if load >= high), None)
    return abs(last - first) if first is not None and last is not None else 0.0

def _feature_baseline(rows: list[dict[str, Any]]) -> dict[str, Any]:
    keys = {key for row in rows for key, value in row.items() if _number(value)}
    result = {}
    for key in keys:
        values = [float(row[key]) for row in rows if _number(row.get(key))]
        if values:
            center = median(values); deviations = [abs(value - center) for value in values]
            result[key] = {"median": center, "mad": median(deviations), "count": len(values)}
    return result

def _qualifying_count(rows: list[dict[str, Any]], baseline: dict[str, Any], key: str, relative_change: float) -> int:
    reference = baseline.get(key, {}).get("median") if baseline else None
    if not _number(reference): return 0
    cutoff = float(reference) + max(abs(float(reference)) * relative_change, .02)
    return sum(_number(row.get(key)) and float(row[key]) > cutoff for row in rows[-5:])

def _meaningful_transition_baseline(baseline: dict[str, Any], key: str) -> bool:
    """Reject a zero/near-zero transition reference before relative comparison."""
    reference = baseline.get(key, {}).get("median") if baseline else None
    return _number(reference) and float(reference) >= MIN_MEANINGFUL_TRANSITION_BASELINE_PERCENT

def _low_qualifying_count(rows: list[dict[str, Any]], baseline: dict[str, Any], key: str, relative_change: float) -> int:
    reference = baseline.get(key, {}).get("median") if baseline else None
    if not _number(reference): return 0
    cutoff = float(reference) - max(abs(float(reference)) * relative_change, .02)
    return sum(_number(row.get(key)) and float(row[key]) < cutoff for row in rows[-5:])

def _card_metric_evidence(rows: list[dict[str, Any]], baseline: dict[str, Any], key: str) -> dict[str, Any]:
    """Preserve deterministic values behind the user-facing card comparison."""
    observations = []
    for row in rows[-5:]:
        features = _json_value(row.get("feature_json"), row)
        if _number(features.get(key)):
            observations.append({
                "observed_at": row.get("observed_at").isoformat() if hasattr(row.get("observed_at"), "isoformat") else row.get("observed_at"),
                "source_card_id": row.get("source_card_id"),
                "value": float(features[key]),
            })
    return {
        "card_feature_name": key,
        "card_feature_unit": "percent_of_observed_stroke",
        "clean_card_baseline_window_days": 30,
        "clean_card_median": baseline.get(key, {}).get("median"),
        "card_observations": observations,
    }

def _episode(code: str, severity: str, summary: str, evidence: dict[str, Any], state: str = "active") -> dict[str, Any]: return {"code": code, "severity": severity, "state": state, "summary": summary, "evidence": evidence}


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

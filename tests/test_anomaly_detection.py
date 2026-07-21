from __future__ import annotations

from datetime import datetime, timedelta, timezone

from omai.repositories.anomaly_repository import DataPointTarget
from omai.services.anomaly_detection_service import AnomalyDetectionService


class FakeRepository:
    def __init__(self, targets, rules=None, state=None):
        self.targets = targets
        self.rules_value = rules or []
        self.state = state or {}
        self.saved_states = []
        self.events = []

    def data_point_targets(self, site_id):
        return self.targets

    def rules(self, site_id, source_type="data_point"):
        return self.rules_value

    def metric_state(self, site_id, source_key):
        return self.state.get(source_key)

    def upsert_metric_state(self, state):
        self.saved_states.append(state)
        self.state[state["source_key"]] = state

    def insert_event(self, event):
        self.events.append(event)


class FakeGraphite:
    def __init__(self, series):
        self.series = series
        self.requested_targets = []

    def fetch(self, targets, start, end, max_data_points=720):
        self.requested_targets.extend(targets)
        return self.series


def target(target_id=1):
    return DataPointTarget(
        id=target_id,
        site_id=4,
        site_key="HARTZOG",
        facility_id=10,
        facility_name="HDU_2510",
        device_name="HDU_2510",
        data_point_name="stroke_min",
        tags="stroke_min",
        active=True,
    )


def points(days: int, value: float = 10.0):
    end = datetime.now(timezone.utc)
    return [
        {"time": end - timedelta(days=index), "value": value}
        for index in range(days)
    ]


def test_baseline_requires_at_least_ten_days_of_graphite_history():
    repo = FakeRepository([target()])
    service = AnomalyDetectionService(repo, FakeGraphite({1: points(9)}))

    summary = service.build_data_point_baseline(4, days=30)

    assert summary["insufficient_history"] == 1
    assert repo.saved_states[0]["baseline_status"] == "insufficient_history"
    assert repo.saved_states[0]["baseline_days_available"] == 9


def test_baseline_ready_after_ten_days_of_history():
    repo = FakeRepository([target()])
    service = AnomalyDetectionService(repo, FakeGraphite({1: points(10)}))

    summary = service.build_data_point_baseline(4, days=30)

    assert summary["ready"] == 1
    assert repo.saved_states[0]["baseline_status"] == "ready"


def test_check_records_anomaly_when_recent_value_exceeds_baseline():
    old_state = {
        "site_id": 4,
        "source_type": "data_point",
        "source_key": "data_point:1",
        "scope_type": "data_point",
        "scope_id": 1,
        "metric_key": "stroke_min",
        "comparison_mode": "point_value",
        "rolling_count": 30,
        "rolling_avg": 10.0,
        "rolling_stddev": 1.0,
        "rolling_min": 8.0,
        "rolling_max": 12.0,
        "rolling_median": 10.0,
        "rolling_mad": 1.0,
        "baseline_status": "ready",
        "baseline_days_available": 30,
    }
    repo = FakeRepository([target()], state={"data_point:1": old_state})
    now = datetime.now(timezone.utc)
    service = AnomalyDetectionService(repo, FakeGraphite({1: [{"time": now, "value": 30.0}]}))

    summary = service.check_data_point_anomalies(4)

    assert summary["checked"] == 1
    assert summary["anomalies"] == 1
    assert repo.events[0]["metric_key"] == "stroke_min"

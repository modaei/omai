from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from omai.clients.rod_pump_analysis_client import METRICS, RodPumpAnalysisClient
from omai.services.paraffin_prediction import (
    FEATURE_NAMES,
    build_prediction_features,
    explicit_paraffin_in_horizon,
    load_predictor,
    score_prediction,
)
from omai.training.paraffin import load_live_histories


AS_OF = datetime(2026, 7, 6, tzinfo=timezone.utc)


def _series():
    return {
        metric: [
            {"time": (AS_OF - timedelta(hours=hours)).isoformat(), "value": 100 + (30 - hours)}
            for hours in range(30, -1, -1)
        ]
        for metric in (
            "Min Load Last Stroke",
            "Peak Load Last Stroke",
            "Pump Fillage",
            "Yesterday Cycles",
            "Yesterday Strokes per minute",
        )
    }


def test_only_explicit_future_paraffin_is_a_positive_label():
    notes = [
        {"time": (AS_OF + timedelta(hours=12)).isoformat(), "event": "chemical_treatment"},
        {"time": (AS_OF + timedelta(hours=24)).isoformat(), "event": "thermal_treatment"},
    ]
    assert explicit_paraffin_in_horizon(notes, AS_OF) is False
    notes.append({"time": (AS_OF + timedelta(hours=47)).isoformat(), "event": "paraffin"})
    assert explicit_paraffin_in_horizon(notes, AS_OF) is True
    notes[-1]["time"] = (AS_OF + timedelta(hours=49)).isoformat()
    assert explicit_paraffin_in_horizon(notes, AS_OF) is False


def test_treatments_are_prior_event_features_and_future_events_do_not_leak():
    notes = [
        {"time": (AS_OF - timedelta(hours=12)).isoformat(), "event": "chemical_treatment"},
        {"time": (AS_OF + timedelta(hours=1)).isoformat(), "event": "thermal_treatment"},
    ]
    features, warnings = build_prediction_features(_series(), notes, AS_OF)
    assert warnings == []
    assert features["chemical_treatment|hours_since"] == 12
    assert features["chemical_treatment|count_7d"] == 1
    assert features["thermal_treatment|count_7d"] == 0
    assert "card" not in " ".join(FEATURE_NAMES).lower()


def test_naive_chart_note_times_use_the_snapshot_timezone():
    features, _ = build_prediction_features(
        _series(),
        [{"time": "2026-07-05 12:00:00", "event": "thermal_treatment"}],
        AS_OF,
    )
    assert features["thermal_treatment|hours_since"] == 12


def test_runtime_rejects_model_below_validation_gate(tmp_path):
    joblib = pytest.importorskip("joblib")
    path = tmp_path / "bad.joblib"
    joblib.dump({
        "artifact_type": "omai-paraffin-48h",
        "feature_names": FEATURE_NAMES,
        "test_metrics": {"precision": .49, "recall": .9},
    }, path)
    with pytest.raises(ValueError, match="precision"):
        load_predictor(str(path))


def test_scoring_uses_artifact_threshold_and_never_needs_cards():
    class Model:
        def predict_proba(self, rows):
            assert len(rows[0]) == len(FEATURE_NAMES)
            return [[.2, .8]]

    features, _ = build_prediction_features(_series(), [], AS_OF)
    result = score_prediction({
        "model": Model(),
        "threshold": .7,
        "model_version": "test",
        "feature_reference": {},
    }, features)
    assert result["horizon_hours"] == 48
    assert result["probability"] == .8
    assert result["alert"] is True


def test_live_training_history_reads_graphite_and_categorizes_database_notes():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE sites (id INTEGER, `key` TEXT)"))
        connection.execute(text("CREATE TABLE wells (id INTEGER, site_id INTEGER, name TEXT, `key` TEXT, pump_type TEXT)"))
        connection.execute(text("CREATE TABLE chart_notes (object_type TEXT, object_id INTEGER, x_axis_value TEXT, chart_name TEXT, note TEXT)"))
        connection.execute(text("INSERT INTO sites VALUES (4, 'SITE')"))
        connection.execute(text("INSERT INTO wells VALUES (10, 4, 'WELL 10', 'W10', 'RoD')"))
        connection.execute(text("INSERT INTO chart_notes VALUES ('RodPumpOilWell', 10, '2026-05-15 00:00:00', 'Load', 'Hot water treatment')"))
        connection.execute(text("INSERT INTO chart_notes VALUES ('RodPumpOilWell', 10, '2026-06-15 00:00:00', 'Load', 'Paraffin buildup')"))

    client = RodPumpAnalysisClient(engine, "http://graphite", batch_workers=1)
    requested_max_points = []

    def trends(site_key, well_key, start, end, max_data_points=None):
        requested_max_points.append(max_data_points)
        points = [{"time": start.isoformat(), "value": 100.0}]
        return {label: list(points) for label in METRICS.values()}, []

    client._trend_series = trends
    records = load_live_histories(
        client,
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 6, 20, tzinfo=timezone.utc),
        {4},
    )

    assert len(records) == 1
    assert records[0]["well_key"] == "W10"
    assert [note["event"] for note in records[0]["chart_note_events"]] == [
        "thermal_treatment", "paraffin",
    ]
    assert requested_max_points and set(requested_max_points) == {168}

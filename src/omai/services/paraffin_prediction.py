from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any


PREDICTION_HORIZON_HOURS = 48
WINDOW_HOURS = (6, 24, 168, 720)
TELEMETRY_METRICS = (
    "Min Load Last Stroke",
    "Peak Load Last Stroke",
    "Pump Fillage",
    "Yesterday Cycles",
    "Yesterday Strokes per minute",
)
STAT_NAMES = ("latest", "minimum", "maximum", "mean", "stddev", "change", "slope", "coverage")
FEATURE_NAMES = tuple(
    f"{metric}|{hours}h|{stat}"
    for metric in TELEMETRY_METRICS
    for hours in WINDOW_HOURS
    for stat in STAT_NAMES
) + tuple(
    f"{event}|{name}"
    for event in ("chemical_treatment", "thermal_treatment", "paraffin")
    for name in ("hours_since", "count_7d", "count_30d", "count_90d")
) + tuple(
    f"{event}|response|{metric}"
    for event in ("chemical_treatment", "thermal_treatment")
    for metric in TELEMETRY_METRICS
)


def build_prediction_features(
    series: dict[str, list[dict[str, Any]]],
    notes: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[dict[str, float], list[str]]:
    """Build leakage-safe telemetry and prior-event features for one snapshot."""
    features = {name: math.nan for name in FEATURE_NAMES}
    warnings: list[str] = []
    for metric in TELEMETRY_METRICS:
        points = _points_before(series.get(metric, []), as_of)
        if not points:
            warnings.append(f"{metric} has no samples in the prediction history.")
        for hours in WINDOW_HOURS:
            window = [(stamp, value) for stamp, value in points if stamp > as_of - timedelta(hours=hours)]
            prefix = f"{metric}|{hours}h|"
            if not window:
                continue
            values = [value for _, value in window]
            elapsed = max((window[-1][0] - window[0][0]).total_seconds() / 3600, 0)
            expected = max(1, hours)
            features.update({
                prefix + "latest": values[-1],
                prefix + "minimum": min(values),
                prefix + "maximum": max(values),
                prefix + "mean": mean(values),
                prefix + "stddev": pstdev(values) if len(values) > 1 else 0.0,
                prefix + "change": _ratio(values[-1], values[0]),
                prefix + "slope": _linear_slope(window),
                # Graphite may return sub-hourly or downsampled values. Coverage
                # is therefore bounded and acts as a data-quality indicator.
                prefix + "coverage": min(1.0, max(len(window) / expected, elapsed / hours)),
            })

    prior_events = _events_before(notes, as_of)
    for event in ("chemical_treatment", "thermal_treatment", "paraffin"):
        times = [stamp for stamp, name in prior_events if name == event]
        prefix = f"{event}|"
        if times:
            features[prefix + "hours_since"] = (as_of - max(times)).total_seconds() / 3600
        for days in (7, 30, 90):
            features[prefix + f"count_{days}d"] = float(
                sum(stamp > as_of - timedelta(days=days) for stamp in times)
            )
        if event in ("chemical_treatment", "thermal_treatment") and times:
            latest_event = max(times)
            for metric in TELEMETRY_METRICS:
                points = _points_before(series.get(metric, []), as_of)
                before = [value for stamp, value in points if latest_event - timedelta(hours=24) < stamp <= latest_event]
                after = [value for stamp, value in points if latest_event < stamp <= min(as_of, latest_event + timedelta(hours=24))]
                if before and after:
                    features[f"{event}|response|{metric}"] = _ratio(mean(after), mean(before))
    return features, warnings


def load_predictor(path: str):
    """Load a trusted, locally provisioned joblib artifact."""
    try:
        import joblib
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise RuntimeError("Install OMAI with the 'ml' extra to score paraffin models.") from exc
    artifact = joblib.load(Path(path))
    if not isinstance(artifact, dict) or artifact.get("artifact_type") != "omai-paraffin-48h":
        raise ValueError("Artifact is not an OMAI 48-hour paraffin model.")
    if tuple(artifact.get("feature_names", ())) != FEATURE_NAMES:
        raise ValueError("Artifact feature schema does not match this OMAI version.")
    metrics = artifact.get("test_metrics", {})
    if float(metrics.get("precision", 0)) < .50 or float(metrics.get("recall", 0)) < .60:
        raise ValueError("Artifact did not meet the 50% precision and 60% recall gate.")
    return artifact


def score_prediction(artifact: dict[str, Any], features: dict[str, float]) -> dict[str, Any]:
    row = [[features[name] for name in FEATURE_NAMES]]
    probability = float(artifact["model"].predict_proba(row)[0][1])
    threshold = float(artifact["threshold"])
    explanations = _explain(features, artifact.get("feature_reference", {}))
    return {
        "available": True,
        "horizon_hours": PREDICTION_HORIZON_HOURS,
        "probability": round(probability, 3),
        "threshold": round(threshold, 3),
        "alert": probability >= threshold,
        "model_version": artifact.get("model_version", "unknown"),
        "supporting_factors": explanations[0],
        "opposing_factors": explanations[1],
    }


def explicit_paraffin_in_horizon(notes: list[dict[str, Any]], as_of: datetime) -> bool:
    """Training label: only explicit paraffin/wax events in the next 48 hours."""
    end = as_of + timedelta(hours=PREDICTION_HORIZON_HOURS)
    return any(as_of < stamp <= end and event == "paraffin" for stamp, event in _all_events(notes, as_of.tzinfo))


def _points_before(points, as_of):
    output = []
    for point in points:
        try:
            stamp = _datetime(point["time"], as_of.tzinfo)
            value = float(point["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if stamp <= as_of and math.isfinite(value):
            output.append((stamp, value))
    return sorted(output)


def _all_events(notes, tzinfo=None):
    output = []
    for note in notes:
        try:
            output.append((_datetime(note["time"], tzinfo), str(note["event"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(output)


def _events_before(notes, as_of):
    return [(stamp, event) for stamp, event in _all_events(notes, as_of.tzinfo) if stamp <= as_of]


def _datetime(value, tzinfo=None):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=tzinfo)


def _ratio(current, previous):
    return (current - previous) / abs(previous) if previous else 0.0


def _linear_slope(points):
    if len(points) < 2:
        return 0.0
    origin = points[0][0]
    xs = [(stamp - origin).total_seconds() / 3600 for stamp, _ in points]
    ys = [value for _, value in points]
    x_mean, y_mean = mean(xs), mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator if denominator else 0.0


def _explain(features, reference):
    ranked = []
    for name, value in features.items():
        row = reference.get(name, {})
        scale = float(row.get("scale", 0) or 0)
        if not scale or not math.isfinite(value):
            continue
        deviation = (value - float(row.get("median", 0))) / scale
        influence = deviation * float(row.get("direction", 0))
        ranked.append((influence, name, round(value, 3)))
    supporting = [{"feature": name, "value": value} for score, name, value in sorted(ranked, reverse=True)[:3] if score > 0]
    opposing = [{"feature": name, "value": value} for score, name, value in sorted(ranked)[:3] if score < 0]
    return supporting, opposing

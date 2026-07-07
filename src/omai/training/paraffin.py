from __future__ import annotations

import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import text

from omai.clients.rod_pump_analysis_client import (
    METRICS,
    RodPumpAnalysisClient,
    categorize_chart_note,
)
from omai.config.settings import Settings

from omai.services.paraffin_prediction import (
    FEATURE_NAMES,
    build_prediction_features,
    explicit_paraffin_in_horizon,
)


def generate_examples(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create six-hour snapshots from exported telemetry and chart-note histories."""
    examples = []
    for record in records:
        notes = record.get("chart_note_events", [])
        timestamps = [
            _parse(point["time"])
            for points in record.get("trend_series", {}).values()
            for point in points
            if point.get("time")
        ]
        if not timestamps:
            continue
        start = _ceil_six_hours(min(timestamps) + timedelta(days=30))
        end = max(timestamps) - timedelta(hours=48)
        paraffin_times = sorted(
            _parse(note["time"]) for note in notes if note.get("event") == "paraffin"
        )
        as_of = start
        while as_of <= end:
            # Do not teach the model that immediate post-event operation is a
            # normal negative condition while a field response is in progress.
            if not any(event <= as_of < event + timedelta(days=7) for event in paraffin_times):
                features, _ = build_prediction_features(record["trend_series"], notes, as_of)
                coverage = [features[name] for name in FEATURE_NAMES if name.endswith("|coverage")]
                if sum(value >= .25 for value in coverage if math.isfinite(value)) >= 3:
                    examples.append({
                        "as_of": as_of,
                        "site_id": record.get("site_id"),
                        "well_key": record.get("well_key"),
                        "features": features,
                        "label": int(explicit_paraffin_in_horizon(notes, as_of)),
                    })
            as_of += timedelta(hours=6)
    return sorted(examples, key=lambda row: row["as_of"])


def load_live_histories(
    client: RodPumpAnalysisClient,
    start: datetime,
    end: datetime,
    site_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve reproducible multi-well histories directly from MySQL and Graphite."""
    if end <= start + timedelta(days=30, hours=48):
        raise ValueError("The live training interval must exceed 32 days.")
    with client.engine.connect() as connection:
        wells = connection.execute(text("""
            SELECT w.id well_id, w.site_id, w.name well_name, w.`key` well_key,
                   s.`key` site_key
            FROM wells w JOIN sites s ON s.id=w.site_id
            WHERE LOWER(w.pump_type)='rod'
            ORDER BY w.site_id, w.name
        """)).mappings().all()
    selected = [dict(well) for well in wells if site_ids is None or int(well["site_id"]) in site_ids]
    if not selected:
        raise ValueError("No rod-pump wells matched the requested site scope.")

    telemetry_start = start - timedelta(days=30)
    note_start = start - timedelta(days=90)

    def retrieve(well):
        series = {label: [] for label in METRICS.values()}
        warnings = []
        chunk_start = telemetry_start
        # Seven-day requests with at least 168 points preserve hourly source
        # resolution while keeping Graphite responses bounded.
        while chunk_start < end:
            chunk_end = min(end, chunk_start + timedelta(days=7))
            chunk, chunk_warnings = client._trend_series(
                well["site_key"], well["well_key"], chunk_start, chunk_end,
                max_data_points=max(client.max_data_points, 168),
            )
            for metric, points in chunk.items():
                series.setdefault(metric, []).extend(points)
            warnings.extend(chunk_warnings)
            chunk_start = chunk_end
        series = {
            metric: list({str(point["time"]): point for point in points}.values())
            for metric, points in series.items()
        }
        for points in series.values():
            points.sort(key=lambda point: str(point["time"]))
        with client.engine.connect() as connection:
            notes = connection.execute(text("""
                SELECT x_axis_value, chart_name, note
                FROM chart_notes
                WHERE object_type='RodPumpOilWell' AND object_id=:well_id
                  AND x_axis_value>=:start_time AND x_axis_value<=:end_time
                ORDER BY x_axis_value
            """), {
                "well_id": well["well_id"],
                "start_time": note_start.replace(tzinfo=None),
                "end_time": end.replace(tzinfo=None),
            }).mappings().all()
        return {
            "site_id": int(well["site_id"]),
            "well_id": int(well["well_id"]),
            "well_name": str(well["well_name"]),
            "well_key": str(well["well_key"]),
            "trend_series": series,
            "chart_note_events": [
                {
                    "time": str(note["x_axis_value"]),
                    "chart": note["chart_name"],
                    "event": categorize_chart_note(note["note"] or ""),
                }
                for note in notes
            ],
            "retrieval_warnings": sorted(set(warnings)),
        }

    records = []
    with ThreadPoolExecutor(max_workers=client.batch_workers) as executor:
        futures = {executor.submit(retrieve, well): well for well in selected}
        for future in as_completed(futures):
            records.append(future.result())
    return sorted(records, key=lambda row: (row["site_id"], row["well_name"]))


def train(examples: list[dict[str, Any]], model_version: str) -> dict[str, Any]:
    """Train, calibrate, threshold, and test a leakage-resistant model artifact."""
    try:
        import numpy as np
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.compose import ColumnTransformer
        from sklearn.ensemble import HistGradientBoostingClassifier
        from sklearn.frozen import FrozenEstimator
        from sklearn.impute import SimpleImputer
        from sklearn.metrics import average_precision_score, brier_score_loss, precision_score, recall_score, roc_auc_score
        from sklearn.pipeline import Pipeline
        from sklearn.utils.class_weight import compute_sample_weight
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Install OMAI with the 'ml' extra to train a model.") from exc

    if len(examples) < 100 or sum(row["label"] for row in examples) < 20:
        raise ValueError("Training requires at least 100 snapshots and 20 positive snapshots.")
    train_rows, calibration_rows, validation_rows, test_rows = _chronological_split(examples)
    if any(not rows or len({row["label"] for row in rows}) < 2 for rows in (train_rows, calibration_rows, validation_rows, test_rows)):
        raise ValueError("Every chronological partition must contain positive and negative snapshots.")

    def xy(rows):
        return (
            np.asarray([[row["features"][name] for name in FEATURE_NAMES] for row in rows], dtype=float),
            np.asarray([row["label"] for row in rows], dtype=int),
        )

    x_train, y_train = xy(train_rows)
    x_cal, y_cal = xy(calibration_rows)
    x_validation, y_validation = xy(validation_rows)
    x_test, y_test = xy(test_rows)
    preprocessing = ColumnTransformer([("numeric", SimpleImputer(strategy="median", add_indicator=True), list(range(len(FEATURE_NAMES))))])
    base = Pipeline([
        ("preprocessing", preprocessing),
        ("classifier", HistGradientBoostingClassifier(max_iter=250, max_leaf_nodes=15, learning_rate=.05, l2_regularization=1.0, random_state=42)),
    ])
    base.fit(x_train, y_train, classifier__sample_weight=compute_sample_weight("balanced", y_train))
    model = CalibratedClassifierCV(FrozenEstimator(base), method="sigmoid", cv=3)
    model.fit(x_cal, y_cal)
    validation_probability = model.predict_proba(x_validation)[:, 1]
    threshold = _select_threshold(y_validation, validation_probability)
    validation_metrics = _metrics(y_validation, validation_probability, threshold, precision_score, recall_score, average_precision_score, roc_auc_score, brier_score_loss)
    test_probability = model.predict_proba(x_test)[:, 1]
    test_metrics = _metrics(y_test, test_probability, threshold, precision_score, recall_score, average_precision_score, roc_auc_score, brier_score_loss)
    reference = _feature_reference(train_rows)
    return {
        "artifact_type": "omai-paraffin-48h",
        "model_version": model_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "horizon_hours": 48,
        "feature_names": FEATURE_NAMES,
        "model": model,
        "threshold": threshold,
        "validation_metrics": validation_metrics,
        "test_metrics": test_metrics,
        "feature_reference": reference,
        "dataset": {
            "snapshots": len(examples),
            "positives": sum(row["label"] for row in examples),
            "period_start": examples[0]["as_of"].isoformat(),
            "period_end": examples[-1]["as_of"].isoformat(),
            "partitions": {"train": len(train_rows), "calibration": len(calibration_rows), "validation": len(validation_rows), "test": len(test_rows)},
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieve data and train OMAI's 48-hour paraffin model.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="Use a previously saved JSON history.")
    source.add_argument("--live", action="store_true", help="Retrieve histories directly from Graphite and MySQL.")
    parser.add_argument("--start", help="First desired snapshot time for --live (ISO-8601).")
    parser.add_argument("--end", help="Historical cutoff for --live (ISO-8601); must include known future labels.")
    parser.add_argument("--site-id", type=int, action="append", help="Limit --live retrieval to a site; repeat as needed.")
    parser.add_argument("--dataset-output", help="Optionally save the retrieved source dataset as JSON.")
    parser.add_argument("--output", required=True, help="Destination .joblib artifact.")
    parser.add_argument("--model-version", required=True)
    args = parser.parse_args()
    if args.live:
        if not args.start or not args.end:
            parser.error("--live requires --start and --end")
        client = RodPumpAnalysisClient.from_settings(Settings.from_env())
        start = _parse(args.start).astimezone(client.timezone)
        end = _parse(args.end).astimezone(client.timezone)
        records = load_live_histories(client, start, end, set(args.site_id) if args.site_id else None)
        if args.dataset_output:
            Path(args.dataset_output).write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "requested_snapshot_start": start.isoformat(),
                "historical_cutoff": end.isoformat(),
                "wells": records,
            }, indent=2))
    else:
        if args.start or args.end or args.site_id:
            parser.error("--start, --end, and --site-id are valid only with --live")
        payload = json.loads(Path(args.input).read_text())
        records = payload["wells"] if isinstance(payload, dict) else payload
    artifact = train(generate_examples(records), args.model_version)
    import joblib
    joblib.dump(artifact, args.output)
    print(json.dumps({key: artifact[key] for key in ("model_version", "threshold", "validation_metrics", "test_metrics", "dataset")}, indent=2))


def _chronological_split(examples):
    # Four chronological blocks leave calibration and final testing independent.
    n = len(examples)
    boundaries = (int(n * .65), int(n * .75), int(n * .875))
    raw = [examples[:boundaries[0]], examples[boundaries[0]:boundaries[1]], examples[boundaries[1]:boundaries[2]], examples[boundaries[2]:]]
    output = []
    for index, rows in enumerate(raw):
        if index and rows:
            cutoff = rows[0]["as_of"] + timedelta(hours=48)
            rows = [row for row in rows if row["as_of"] >= cutoff]
        output.append(rows)
    return output


def _select_threshold(labels, probabilities):
    from sklearn.metrics import precision_score, recall_score
    candidates = []
    for threshold in sorted(set(float(value) for value in probabilities)):
        predicted = probabilities >= threshold
        precision = precision_score(labels, predicted, zero_division=0)
        recall = recall_score(labels, predicted, zero_division=0)
        if precision >= .50:
            candidates.append((recall, -threshold, threshold))
    if not candidates:
        raise ValueError("No validation threshold achieved precision >= 0.50.")
    return float(max(candidates)[2])


def _metrics(labels, probabilities, threshold, precision, recall, pr_auc, roc_auc, brier):
    from sklearn.metrics import confusion_matrix
    predicted = probabilities >= threshold
    tn, fp, fn, tp = confusion_matrix(labels, predicted, labels=[0, 1]).ravel()
    return {
        "precision": round(float(precision(labels, predicted, zero_division=0)), 4),
        "recall": round(float(recall(labels, predicted, zero_division=0)), 4),
        "pr_auc": round(float(pr_auc(labels, probabilities)), 4),
        "roc_auc": round(float(roc_auc(labels, probabilities)), 4),
        "brier_score": round(float(brier(labels, probabilities)), 4),
        "confusion_matrix": {"true_negative": int(tn), "false_positive": int(fp), "false_negative": int(fn), "true_positive": int(tp)},
    }


def _feature_reference(rows):
    output = {}
    positives = [row for row in rows if row["label"]]
    negatives = [row for row in rows if not row["label"]]
    for name in FEATURE_NAMES:
        values = [row["features"][name] for row in rows if math.isfinite(row["features"][name])]
        positive = [row["features"][name] for row in positives if math.isfinite(row["features"][name])]
        negative = [row["features"][name] for row in negatives if math.isfinite(row["features"][name])]
        if not values:
            continue
        center = median(values)
        deviations = [abs(value - center) for value in values]
        output[name] = {"median": center, "scale": median(deviations) or 1.0, "direction": 1 if positive and negative and median(positive) >= median(negative) else -1}
    return output


def _ceil_six_hours(stamp):
    stamp = stamp.replace(minute=0, second=0, microsecond=0)
    return stamp + timedelta(hours=(-stamp.hour) % 6)


def _parse(value):
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import hashlib
import math
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from omai.config.logging import configure_logging
from omai.config.settings import Settings


ROD_PUMP_TAG_MAPPING = {
    "peak_load_sp": "max_load",
    "min_load_sp": "min_load",
    "peak_load_ls": "peak_load_last_stroke",
    "min_load_ls": "min_load_last_stroke",
}


def main() -> None:
    """Generate deterministic synthetic Graphite plaintext metrics for the demo."""
    parser = argparse.ArgumentParser(
        description="Generate synthetic Graphite metrics from sanitized demo metadata."
    )
    parser.add_argument("--site-id", type=int)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--interval-minutes", type=int, default=60)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    settings.validate()
    settings.validate_database()

    site_id = args.site_id or settings.demo_site_id
    if site_id <= 0:
        raise SystemExit("--site-id is required unless DEMO_SITE_ID is configured.")
    if args.days <= 0:
        raise SystemExit("--days must be positive.")
    if args.interval_minutes <= 0:
        raise SystemExit("--interval-minutes must be positive.")

    # Keep generated telemetry aligned with the real current demo date.
    end_date = args.end_date or date.today()
    output = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        metric_count, sample_count = generate_metrics(
            settings=settings,
            site_id=site_id,
            end_date=end_date,
            days=args.days,
            interval_minutes=args.interval_minutes,
            output=output,
        )
    finally:
        if args.output:
            output.close()

    print(
        f"Generated {sample_count} samples for {metric_count} synthetic metrics.",
        file=sys.stderr,
    )


def generate_metrics(
    *,
    settings: Settings,
    site_id: int,
    end_date: date,
    days: int,
    interval_minutes: int,
    output: TextIO,
) -> tuple[int, int]:
    """Write one Graphite plaintext series per unique sanitized telemetry path."""
    rows = _data_points(settings, site_id)
    start = datetime.combine(end_date - timedelta(days=days - 1), time.min, timezone.utc)
    end = datetime.combine(end_date, time.max, timezone.utc)
    interval = timedelta(minutes=interval_minutes)
    timestamps = []
    current = start
    while current <= end:
        timestamps.append(current)
        current += interval

    metric_rows = {
        _graphite_path(row): row
        for row in rows
        if _graphite_path(row)
    }
    for target, row in sorted(metric_rows.items()):
        for timestamp, value in _series_values(target, row["data_point_name"], timestamps):
            output.write(f"{target} {value:.6f} {int(timestamp.timestamp())}\n")

    return len(metric_rows), len(metric_rows) * len(timestamps)


def _data_points(settings: Settings, site_id: int) -> list[dict[str, Any]]:
    """Load metadata only; no current or historical operational values are read."""
    engine = create_engine(
        URL.create(
            "mysql+pymysql",
            username=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        ),
        pool_pre_ping=True,
    )
    query = text(
        """
        SELECT s.`key` AS site_key,
            COALESCE(parent.facility_name, dp.facility_name) AS facility_name,
            dp.tag,
            dp.data_point_name
        FROM data_points dp
        LEFT JOIN data_points parent ON parent.id = dp.facility_id
        JOIN sites s ON s.id = dp.site_id
        WHERE dp.site_id = :site_id
          AND dp.active = 1
          AND dp.tag IS NOT NULL
          AND TRIM(dp.tag) <> ''
          AND COALESCE(parent.facility_name, dp.facility_name) IS NOT NULL
          AND dp.data_point_name IS NOT NULL
        """
    )
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(query, {"site_id": site_id}).mappings()]


def _graphite_path(row: dict[str, Any]) -> str | None:
    """Build the exact target convention used by Omai's trend retrieval client."""
    site_key = str(row.get("site_key") or "").strip()
    facility_name = str(row.get("facility_name") or "").strip()
    tag = str(row.get("tag") or "").strip()
    if not site_key or not facility_name or not tag:
        return None
    target_tag = ROD_PUMP_TAG_MAPPING.get(tag, tag)
    return f"MI3.{site_key}.{facility_name.replace(' ', '_')}.{target_tag}"


def _series_values(
    target: str,
    data_point_name: Any,
    timestamps: list[datetime],
) -> list[tuple[datetime, float]]:
    """Generate stable, plausible-looking values without using operational data."""
    seed = int(hashlib.sha256(target.encode("utf-8")).hexdigest()[:16], 16)
    generator = random.Random(seed)
    baseline = _baseline_for_name(str(data_point_name), generator)
    amplitude = max(abs(baseline) * 0.025, 0.5)
    drift = generator.uniform(-0.12, 0.12) * amplitude
    phase = generator.uniform(0, math.tau)
    values = []
    denominator = max(len(timestamps) - 1, 1)
    for index, timestamp in enumerate(timestamps):
        cycle = math.sin((index / 24) * math.tau + phase) * amplitude
        noise = generator.uniform(-0.35, 0.35) * amplitude
        value = max(0.0, baseline + cycle + noise + drift * (index / denominator))
        values.append((timestamp, value))
    return values


def _baseline_for_name(data_point_name: str, generator: random.Random) -> float:
    """Choose only synthetic baseline ranges based on generic telemetry semantics."""
    normalized = data_point_name.lower()
    if "load" in normalized:
        return generator.uniform(12_000, 30_000)
    if "fillage" in normalized or "percent" in normalized:
        return generator.uniform(45, 95)
    if "temperature" in normalized:
        return generator.uniform(80, 220)
    if "pressure" in normalized or normalized in {"pip", "tubing"}:
        return generator.uniform(100, 1_500)
    if "frequency" in normalized:
        return generator.uniform(35, 65)
    if "level" in normalized:
        return generator.uniform(2, 14)
    if "rate" in normalized or "flow" in normalized:
        return generator.uniform(20, 1_000)
    if "current" in normalized or "amp" in normalized:
        return generator.uniform(10, 150)
    return generator.uniform(10, 500)

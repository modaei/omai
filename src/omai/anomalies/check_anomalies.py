from __future__ import annotations

import argparse
import logging
from datetime import timedelta

from omai.clients.graphite_trend_client import GraphiteTrendClient
from omai.config.logging import configure_logging
from omai.config.settings import Settings
from omai.repositories.anomaly_repository import AnomalyRepository
from omai.services.anomaly_detection_service import AnomalyDetectionService


logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check recent Omai anomaly windows.")
    parser.add_argument("--site-id", type=int, required=True)
    parser.add_argument("--window", default="1h", help="Recent window. V1 supports minutes or hours, e.g. 30m or 1h.")
    args = parser.parse_args()

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    settings.validate()
    settings.validate_database()

    service = AnomalyDetectionService(
        repository=AnomalyRepository.from_settings(settings),
        graphite=GraphiteTrendClient(
            settings.graphite_api_url,
            settings.timezone,
            timeout_seconds=settings.graphite_timeout_seconds,
            batch_size=100,
        ),
    )
    summary = service.check_data_point_anomalies(args.site_id, window=_window(args.window))
    logger.info("Anomaly check completed: %s", summary)
    print(summary)


def _window(value: str) -> timedelta:
    raw = value.strip().lower()
    if raw.endswith("h"):
        return timedelta(hours=float(raw[:-1]))
    if raw.endswith("m"):
        return timedelta(minutes=float(raw[:-1]))
    return timedelta(hours=float(raw))


if __name__ == "__main__":
    main()

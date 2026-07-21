from __future__ import annotations

import argparse
import logging

from omai.clients.graphite_trend_client import GraphiteTrendClient
from omai.config.logging import configure_logging
from omai.config.settings import Settings
from omai.repositories.anomaly_repository import AnomalyRepository
from omai.services.anomaly_detection_service import AnomalyDetectionService


logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Omai data-point anomaly baselines.")
    parser.add_argument("--site-id", type=int, required=True)
    parser.add_argument("--days", type=int, default=30)
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
    summary = service.build_data_point_baseline(args.site_id, days=args.days)
    logger.info("Anomaly baseline build completed: %s", summary)
    print(summary)


if __name__ == "__main__":
    main()

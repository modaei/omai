from __future__ import annotations

import json
import logging
from datetime import date, datetime, time

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.repositories.anomaly_repository import (
    AnomalyRepository,
    AnomalyRepositoryError,
)


logger = logging.getLogger(__name__)


class SearchAnomalyEventsInput(BaseModel):
    start_date: str | None = Field(default=None, description="Start date in YYYY-MM-DD format.")
    end_date: str | None = Field(default=None, description="End date in YYYY-MM-DD format.")
    source_type: str | None = Field(
        default=None,
        description="Optional source type filter: data_point, reading, or report.",
    )
    status: str | None = Field(default="open", description="Event status, usually open.")
    limit: int = Field(default=25, ge=1, le=100)


def build_anomaly_tools(repository: AnomalyRepository, site_id: int) -> list[StructuredTool]:
    def search_anomaly_events(
        start_date: str | None = None,
        end_date: str | None = None,
        source_type: str | None = None,
        status: str | None = "open",
        limit: int = 25,
    ) -> str:
        """Search stored anomaly findings for the current site."""
        logger.info(
            "Searching anomaly events site_id=%s start=%s end=%s source=%s",
            site_id,
            start_date,
            end_date,
            source_type,
        )
        try:
            rows = repository.search_events(
                site_id=site_id,
                start=_start(start_date),
                end=_end(end_date),
                source_type=source_type,
                status=status,
                limit=limit,
            )
            return json.dumps({"ok": True, "count": len(rows), "events": rows}, default=str, separators=(",", ":"))
        except (ValueError, AnomalyRepositoryError) as exc:
            logger.warning("Anomaly event search failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":"))

    return [
        StructuredTool.from_function(
            func=search_anomaly_events,
            name="search_anomaly_events",
            description=(
                "Search previously detected anomaly events for data points, readings, "
                "and reports. Use for questions like 'any anomalies today' or "
                "'show abnormal data points last week'. This tool reads stored findings; "
                "it does not run a new anomaly scan."
            ),
            args_schema=SearchAnomalyEventsInput,
        )
    ]


def _start(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.combine(date.fromisoformat(value), time.min)


def _end(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.combine(date.fromisoformat(value), time.max)

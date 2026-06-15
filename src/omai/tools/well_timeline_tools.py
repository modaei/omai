from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.clients.well_timeline_client import (
    WellTimelineClient,
    WellTimelineClientError,
)


logger = logging.getLogger(__name__)


class WellTimelineInput(BaseModel):
    well_name: str = Field(description="Well name or distinctive part of the well name.")
    start_date: str = Field(description="Start date in YYYY-MM-DD format.")
    end_date: str = Field(description="End date in YYYY-MM-DD format.")


def _json_result(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def build_well_timeline_tools(
    client: WellTimelineClient, site_id: int
) -> list[StructuredTool]:
    def get_well_timeline(well_name: str, start_date: str, end_date: str) -> str:
        """Get a chronological well timeline from readings, shutdowns, notes, work, alarms, and history."""
        logger.info(
            "Getting well timeline site_id=%s well=%s start=%s end=%s",
            site_id,
            well_name,
            start_date,
            end_date,
        )
        try:
            return _json_result(
                {
                    "ok": True,
                    **client.get_well_timeline(
                        site_id, well_name, start_date, end_date
                    ),
                }
            )
        except WellTimelineClientError as exc:
            logger.warning("Well timeline lookup failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    return [
        StructuredTool.from_function(
            func=get_well_timeline,
            name="get_well_timeline",
            description=(
                "Return a chronological timeline for one well and date range. "
                "Includes well tests, fluid levels, shutdowns, work orders matched "
                "by well name, alarm events matched by data point names, notes "
                "matched by well name, chart notes, and well history where available."
            ),
            args_schema=WellTimelineInput,
        )
    ]

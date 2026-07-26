from __future__ import annotations

import json
import logging

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.clients.data_point_client import DataPointClient, DataPointClientError


logger = logging.getLogger(__name__)


class GetDataPointValuesInput(BaseModel):
    facility_name: str = Field(
        description="Facility name or shorthand, for example HDU_2510 or 2510."
    )
    data_point_name: str = Field(
        description="Data-point name in human or telemetry form, such as min load or min_load."
    )
    device_name: str | None = Field(
        default=None,
        description=(
            "Optional device name. Omit it when the user did not name a device; "
            "the facility name will then be used as the device selector."
        ),
    )


class AnalyzeDataPointTrendInput(BaseModel):
    """Input schema for Graphite-backed trend analysis of one telemetry point."""

    facility_name: str = Field(
        description="Facility, well, or monitored equipment name/shorthand, such as HDU_2510, 2510, or Tank 10-1 Oil."
    )
    data_point_name: str = Field(
        description="Human data-point name, such as Pump Fillage, stroke min, level, discharge, or pressure."
    )
    days: int = Field(
        default=7,
        ge=1,
        le=366,
        description="Number of trailing days to analyze. Use 7 when the user did not specify a period.",
    )
    start_date: str | None = Field(
        default=None,
        description="Optional explicit start date in YYYY-MM-DD format for calendar/month/date-range requests.",
    )
    end_date: str | None = Field(
        default=None,
        description="Optional explicit end date in YYYY-MM-DD format for calendar/month/date-range requests.",
    )
    device_name: str | None = Field(
        default=None,
        description="Optional device name. Omit when the same selector should be used for facility and device.",
    )
    equipment_type: str | None = Field(
        default=None,
        description="Optional monitored equipment type when the user says tank, pump, or treater.",
    )


def build_data_point_tools(
    client: DataPointClient, site_id: int
) -> list[StructuredTool]:
    def get_data_point_values(
        facility_name: str,
        data_point_name: str,
        device_name: str | None = None,
    ) -> str:
        """Get current telemetry values using normalized site-scoped selectors."""
        logger.info(
            "Getting data-point values site_id=%s facility=%s device=%s point=%s",
            site_id,
            facility_name,
            device_name,
            data_point_name,
        )
        try:
            result = client.get_values(
                site_id=site_id,
                facility_name=facility_name,
                data_point_name=data_point_name,
                device_name=device_name,
            )
            return json.dumps({"ok": True, **result}, default=str, separators=(",", ":"))
        except DataPointClientError as exc:
            logger.warning("Data-point value lookup failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":"))

    def analyze_data_point_trend(
        facility_name: str,
        data_point_name: str,
        days: int = 7,
        device_name: str | None = None,
        equipment_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Analyze historical telemetry values from Graphite for one data point."""
        logger.info(
            "Analyzing data-point trend site_id=%s facility=%s device=%s equipment=%s point=%s days=%s start=%s end=%s",
            site_id,
            facility_name,
            device_name,
            equipment_type,
            data_point_name,
            days,
            start_date,
            end_date,
        )
        try:
            result = client.analyze_trend(
                site_id=site_id,
                facility_name=facility_name,
                data_point_name=data_point_name,
                days=days,
                device_name=device_name,
                equipment_type=equipment_type,
                start_date=start_date,
                end_date=end_date,
            )
            return json.dumps({"ok": True, **result}, default=str, separators=(",", ":"))
        except DataPointClientError as exc:
            logger.warning("Data-point trend analysis failed: %s", exc)
            return json.dumps({"ok": False, "error": str(exc)}, separators=(",", ":"))

    return [
        StructuredTool.from_function(
            func=get_data_point_values,
            name="get_data_point_values",
            description=(
                "Get the latest current value and received time for one or more "
                "matching telemetry data points. Use when the user provides a "
                "facility, optional device, and data-point name. Names are normalized; "
                "a request such as min load may return both min_load_ls and min_load_sp."
            ),
            args_schema=GetDataPointValuesInput,
        ),
        StructuredTool.from_function(
            func=analyze_data_point_trend,
            name="analyze_data_point_trend",
            description=(
                "Fetch and analyze historical Graphite trend samples for one telemetry data point. "
                "Use for requests such as 'analyze pump fillage trend of 2510', "
                "'analyze data point stroke min for 2510 in the past 7 days', "
                "'analyze current load trend of 2510 in July', or "
                "'analyze tank 10-1 Oil level trend'. The tool resolves facility, optional "
                "device, monitored equipment, fuzzy data-point names, and explicit date ranges."
            ),
            args_schema=AnalyzeDataPointTrendInput,
        ),
    ]

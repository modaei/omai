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
        )
    ]

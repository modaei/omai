from __future__ import annotations

import json
import logging
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.clients.report_client import AVAILABLE_REPORTS, ReportClient, ReportClientError


logger = logging.getLogger(__name__)

ReportName = Literal[
    "battery",
    "gas_flared",
    "gas_flared_vented",
    "gas_fuel",
    "gas_production",
    "gas_vented",
    "injection_allocation",
    "oil_production",
    "oil_sale",
    "production_allocation",
    "water_injection",
    "water_production",
    "water_transfer",
]


class RunReportInput(BaseModel):
    report_name: ReportName = Field(description="The report to generate.")
    start_date: str = Field(description="Start date in YYYY-MM-DD format.")
    end_date: str = Field(description="End date in YYYY-MM-DD format.")


class CompareReportInput(BaseModel):
    report_name: ReportName = Field(description="The report metric to compare.")
    first_start_date: str = Field(description="First period start, YYYY-MM-DD.")
    first_end_date: str = Field(description="First period end, YYYY-MM-DD.")
    second_start_date: str = Field(description="Second period start, YYYY-MM-DD.")
    second_end_date: str = Field(description="Second period end, YYYY-MM-DD.")


def _json_result(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def build_report_tools(
    client: ReportClient, site_id: int, site_name: str | None = None
) -> list[StructuredTool]:
    site_display_name = site_name or "selected site"

    def list_available_reports() -> str:
        """List report names available to the assistant."""
        return _json_result(
            {"site_name": site_display_name, "reports": AVAILABLE_REPORTS}
        )

    def run_report(report_name: str, start_date: str, end_date: str) -> str:
        """Run one oil-field report for the selected site and date range."""
        logger.info(
            "Running report site_id=%s report=%s start=%s end=%s",
            site_id,
            report_name,
            start_date,
            end_date,
        )
        try:
            data = client.run_report(
                site_id, report_name, start_date, end_date
            )
            return _json_result(
                {
                    "ok": True,
                    "site_name": site_display_name,
                    "report_name": report_name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "data": data,
                }
            )
        except ReportClientError as exc:
            logger.warning("Report failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    def compare_report_periods(
        report_name: str,
        first_start_date: str,
        first_end_date: str,
        second_start_date: str,
        second_end_date: str,
    ) -> str:
        """Retrieve the same report for two periods so they can be compared."""
        logger.info("Comparing report site_id=%s report=%s", site_id, report_name)
        try:
            first = client.run_report(
                site_id, report_name, first_start_date, first_end_date
            )
            second = client.run_report(
                site_id, report_name, second_start_date, second_end_date
            )
            return _json_result(
                {
                    "ok": True,
                    "site_name": site_display_name,
                    "report_name": report_name,
                    "first_period": {
                        "start_date": first_start_date,
                        "end_date": first_end_date,
                        "data": first,
                    },
                    "second_period": {
                        "start_date": second_start_date,
                        "end_date": second_end_date,
                        "data": second,
                    },
                }
            )
        except ReportClientError as exc:
            logger.warning("Report comparison failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    return [
        StructuredTool.from_function(
            func=list_available_reports,
            name="list_available_reports",
            description="List all oil-field reports supported by the reporting service.",
        ),
        StructuredTool.from_function(
            func=run_report,
            name="run_report",
            description=(
                "Run a production, sales, gas, water, battery, injection, or "
                "allocation report for one date range."
            ),
            args_schema=RunReportInput,
        ),
        StructuredTool.from_function(
            func=compare_report_periods,
            name="compare_report_periods",
            description=(
                "Retrieve the same report for two date ranges for comparison. "
                "Use this instead of making two separate report calls."
            ),
            args_schema=CompareReportInput,
        ),
    ]

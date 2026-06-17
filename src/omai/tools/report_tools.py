from __future__ import annotations

import json
import logging
import re
from calendar import monthrange
from datetime import date, datetime, timezone
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


class MonthlyReportSummaryInput(BaseModel):
    report_name: ReportName = Field(description="The report metric to summarize by month.")
    year: int = Field(description="Calendar year to summarize, for example 2026.")
    battery_name: str | None = Field(
        default=None,
        description=(
            "Optional battery name or number to filter, for example 'Battery 6' "
            "or '6'."
        ),
    )
    value_key: str | None = Field(
        default=None,
        description=(
            "Optional metric key inside each daily row. Leave empty for single-metric "
            "reports such as oil_production, gas_flared, and water_injection."
        ),
    )


def _json_result(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _battery_number(value: str) -> str | None:
    match = re.search(r"(\d+)$", value.strip())
    return match.group(1) if match else None


def _matches_battery(candidate: str, requested: str) -> bool:
    candidate_norm = _normalize_name(candidate)
    requested_norm = _normalize_name(requested)
    if candidate_norm == requested_norm:
        return True

    requested_number = _battery_number(requested)
    candidate_number = _battery_number(candidate)
    return requested_number is not None and requested_number == candidate_number


def _row_month(row: dict[str, Any]) -> str | None:
    raw_date = row.get("date")
    if raw_date is None:
        return None

    if isinstance(raw_date, str):
        try:
            return date.fromisoformat(raw_date[:10]).isoformat()[:7]
        except ValueError:
            return None

    if isinstance(raw_date, (int, float)):
        try:
            # omreports indexed report rows use Unix milliseconds for dates.
            return datetime.fromtimestamp(raw_date / 1000, tz=timezone.utc).strftime(
                "%Y-%m"
            )
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(raw_date, date):
        return raw_date.isoformat()[:7]

    return None


def _month_keys(year: int) -> list[str]:
    return [f"{year}-{month:02d}" for month in range(1, 13)]


def _single_metric_value_key(report_name: str, rows: list[dict[str, Any]]) -> str | None:
    if report_name == "battery":
        return None
    if rows and "value" in rows[0]:
        return "value"
    return None


def _monthly_summary(
    data: Any, report_name: str, year: int, battery_name: str | None, value_key: str | None
) -> dict[str, Any]:
    date_battery_values = (
        data.get("date_battery_values") if isinstance(data, dict) else None
    )
    if not isinstance(date_battery_values, dict):
        return {
            "ok": False,
            "error": "Report result did not include date_battery_values.",
        }

    selected_battery = None
    if battery_name:
        for candidate in date_battery_values:
            if _matches_battery(candidate, battery_name):
                selected_battery = candidate
                break
        if selected_battery is None:
            return {
                "ok": False,
                "error": f"No battery matched {battery_name!r}.",
                "available_batteries": sorted(date_battery_values),
            }

    batteries = [selected_battery] if selected_battery else sorted(date_battery_values)
    monthly_totals = {month: 0.0 for month in _month_keys(year)}

    resolved_value_key = value_key
    matched_rows = 0
    for battery in batteries:
        rows = date_battery_values.get(battery, [])
        if not isinstance(rows, list):
            continue

        if resolved_value_key is None:
            resolved_value_key = _single_metric_value_key(report_name, rows)
        if resolved_value_key is None:
            return {
                "ok": False,
                "error": (
                    "value_key is required for this report because rows contain "
                    "multiple metrics."
                ),
            }

        for row in rows:
            if not isinstance(row, dict):
                continue
            month = _row_month(row)
            if month not in monthly_totals:
                continue
            value = row.get(resolved_value_key)
            if value is None:
                continue
            try:
                monthly_totals[month] += float(value)
                matched_rows += 1
            except (TypeError, ValueError):
                continue

    return {
        "ok": True,
        "report_name": report_name,
        "year": year,
        "battery_name": selected_battery,
        "value_key": resolved_value_key,
        "monthly_totals": {
            month: round(value, 2) for month, value in monthly_totals.items()
        },
        "matched_daily_rows": matched_rows,
    }


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

    def summarize_report_by_month(
        report_name: str,
        year: int,
        battery_name: str | None = None,
        value_key: str | None = None,
    ) -> str:
        """Run one report for a year and aggregate its daily battery rows by month."""
        start_date = f"{year}-01-01"
        end_date = f"{year}-12-{monthrange(year, 12)[1]}"
        logger.info(
            "Summarizing report by month site_id=%s report=%s year=%s battery=%s",
            site_id,
            report_name,
            year,
            battery_name,
        )
        try:
            data = client.run_report(site_id, report_name, start_date, end_date)
            summary = _monthly_summary(
                data=data,
                report_name=report_name,
                year=year,
                battery_name=battery_name,
                value_key=value_key,
            )
            summary.update(
                {
                    "site_name": site_display_name,
                    "start_date": start_date,
                    "end_date": end_date,
                }
            )
            return _json_result(summary)
        except ReportClientError as exc:
            logger.warning("Monthly report summary failed: %s", exc)
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
        StructuredTool.from_function(
            func=summarize_report_by_month,
            name="summarize_report_by_month",
            description=(
                "Run one report for a full calendar year and aggregate daily "
                "battery rows into month totals. Use this for month-by-month "
                "battery comparisons instead of calling run_report once per month."
            ),
            args_schema=MonthlyReportSummaryInput,
        ),
    ]

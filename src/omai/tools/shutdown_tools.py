from __future__ import annotations

import json
import logging
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.clients.shutdown_client import ShutdownClient, ShutdownClientError
from omai.tools.rag_enrichment import (
    OperationalContextSearchStore,
    add_operational_context,
)


logger = logging.getLogger(__name__)

ShutdownType = Literal["all", "short", "long"]


class GetShutdownsInput(BaseModel):
    start_date: str = Field(description="Start date in YYYY-MM-DD format.")
    end_date: str = Field(description="End date in YYYY-MM-DD format.")
    shutdown_type: ShutdownType = Field(
        default="all",
        description="Which shutdowns to return: all, short, or long.",
    )


class CurrentLongShutdownsInput(BaseModel):
    as_of_date: str | None = Field(
        default=None,
        description="Optional date in YYYY-MM-DD format. Defaults to today.",
    )


class ActiveWellsInput(BaseModel):
    active_date: str = Field(description="Date in YYYY-MM-DD format.")


class ShutdownCauseSummaryInput(BaseModel):
    start_date: str = Field(description="Start date in YYYY-MM-DD format.")
    end_date: str = Field(description="End date in YYYY-MM-DD format.")
    shutdown_type: ShutdownType = Field(
        default="all",
        description="Which shutdowns to summarize: all, short, or long.",
    )


def _json_result(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def build_shutdown_tools(
    client: ShutdownClient,
    site_id: int,
    operational_context_store: OperationalContextSearchStore | None = None,
) -> list[StructuredTool]:
    def list_downtime_codes() -> str:
        """List downtime code descriptions for well shutdowns."""
        return _json_result(client.list_downtime_codes())

    def get_well_shutdowns(
        start_date: str,
        end_date: str,
        shutdown_type: str = "all",
    ) -> str:
        """Get well shutdowns for the selected site and date range."""
        logger.info(
            "Getting well shutdowns site_id=%s start=%s end=%s type=%s",
            site_id,
            start_date,
            end_date,
            shutdown_type,
        )
        try:
            result = {
                "ok": True,
                **client.get_shutdowns(site_id, start_date, end_date, shutdown_type),
            }
            enriched = add_operational_context(
                result,
                operational_context_store,
                site_id=site_id,
                query=f"shutdown downtime context {shutdown_type}",
                start_date=start_date,
                end_date=end_date,
                limit=8,
            )
            return _json_result(enriched)
        except ShutdownClientError as exc:
            logger.warning("Well shutdown lookup failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    def get_current_long_shutdowns(as_of_date: str | None = None) -> str:
        """Get ongoing long shutdowns for the selected site."""
        logger.info(
            "Getting current long shutdowns site_id=%s as_of=%s",
            site_id,
            as_of_date,
        )
        try:
            result = {
                "ok": True,
                **client.get_current_long_shutdowns(site_id, as_of_date),
            }
            as_of = result.get("as_of_date")
            enriched = add_operational_context(
                result,
                operational_context_store,
                site_id=site_id,
                query="current ongoing long shutdown context",
                start_date=as_of,
                end_date=as_of,
                limit=8,
            )
            return _json_result(enriched)
        except ShutdownClientError as exc:
            logger.warning("Current long shutdown lookup failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    def get_active_wells(active_date: str) -> str:
        """Count wells active on one day using ONRR state and shutdown records."""
        logger.info("Getting active wells site_id=%s date=%s", site_id, active_date)
        try:
            return _json_result(
                {
                    "ok": True,
                    **client.get_active_wells(site_id, active_date),
                }
            )
        except ShutdownClientError as exc:
            logger.warning("Active well lookup failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    def summarize_shutdown_causes(
        start_date: str,
        end_date: str,
        shutdown_type: str = "all",
    ) -> str:
        """Summarize and rank shutdown causes for the selected site and date range."""
        logger.info(
            "Summarizing shutdown causes site_id=%s start=%s end=%s type=%s",
            site_id,
            start_date,
            end_date,
            shutdown_type,
        )
        try:
            result = {
                "ok": True,
                **client.summarize_shutdown_causes(
                    site_id, start_date, end_date, shutdown_type
                ),
            }
            main_cause = result.get("main_cause") or {}
            query = "shutdown downtime causes context"
            if main_cause.get("downtime_reason"):
                query = f"{query} {main_cause['downtime_reason']}"
            enriched = add_operational_context(
                result,
                operational_context_store,
                site_id=site_id,
                query=query,
                start_date=start_date,
                end_date=end_date,
                limit=10,
            )
            return _json_result(enriched)
        except ShutdownClientError as exc:
            logger.warning("Shutdown cause summary failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    return [
        StructuredTool.from_function(
            func=list_downtime_codes,
            name="list_downtime_codes",
            description="List well shutdown downtime codes and their meanings.",
        ),
        StructuredTool.from_function(
            func=get_well_shutdowns,
            name="get_well_shutdowns",
            description=(
                "Get short/hourly and long-term well shutdowns for a date range. "
                "Use this for downtime, shutdown, shut-in, or reactivation questions."
            ),
            args_schema=GetShutdownsInput,
        ),
        StructuredTool.from_function(
            func=get_current_long_shutdowns,
            name="get_current_long_shutdowns",
            description=(
                "Get wells currently on long shutdown as of a date. "
                "Use this for current or ongoing long shutdown questions."
            ),
            args_schema=CurrentLongShutdownsInput,
        ),
        StructuredTool.from_function(
            func=get_active_wells,
            name="get_active_wells",
            description=(
                "Count and list active wells for one date. Use this for questions "
                "asking how many wells are active, inactive, online, producing, "
                "or available on a specific day. A well is active only when its "
                "ONRR code is active_well=true as of that day and it is not shut "
                "down for the full day. Wells with partial shutdowns are returned "
                "separately and excluded from active and inactive counts."
            ),
            args_schema=ActiveWellsInput,
        ),
        StructuredTool.from_function(
            func=summarize_shutdown_causes,
            name="summarize_shutdown_causes",
            description=(
                "Summarize and rank shutdown causes for a date range. Use this "
                "when the user asks for the main, top, most common, or biggest "
                "cause/reason for shutdowns or downtime."
            ),
            args_schema=ShutdownCauseSummaryInput,
        ),
    ]

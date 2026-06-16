from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from omai.agents.chat_agent import answer_report_question, build_model
from omai.clients.capability_client import CapabilityClient, UnavailableCapabilityClient
from omai.clients.reading_client import ReadingClient, UnavailableReadingClient
from omai.clients.report_client import ReportClient
from omai.clients.shutdown_client import ShutdownClient, UnavailableShutdownClient
from omai.clients.site_client import SiteClient
from omai.clients.well_timeline_client import (
    UnavailableWellTimelineClient,
    WellTimelineClient,
)
from omai.config.settings import Settings
from omai.tools.capability_tools import build_capability_tools
from omai.tools.reading_tools import build_reading_tools
from omai.tools.report_tools import build_report_tools
from omai.tools.shutdown_tools import build_shutdown_tools
from omai.tools.well_timeline_tools import build_well_timeline_tools


logger = logging.getLogger(__name__)


def answer_chat(
    settings: Settings,
    site_id: int,
    site_name: str | None,
    history: list[dict[str, str]],
    question: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    settings.validate()
    resolved_site_name = site_name or _site_name_from_db(settings, site_id)

    report_client = ReportClient(
        api_url=settings.omreports_api_url,
        timeout_seconds=settings.omreports_timeout_seconds,
        max_report_days=settings.max_report_days,
    )
    try:
        reading_client = ReadingClient.from_settings(settings)
    except ValueError as exc:
        reading_client = UnavailableReadingClient(str(exc))
    try:
        shutdown_client = ShutdownClient.from_settings(settings)
    except ValueError as exc:
        shutdown_client = UnavailableShutdownClient(str(exc))
    try:
        well_timeline_client = WellTimelineClient.from_settings(settings)
    except ValueError as exc:
        well_timeline_client = UnavailableWellTimelineClient(str(exc))
    try:
        capability_client = CapabilityClient(_knowledge_dir())
    except Exception as exc:
        capability_client = UnavailableCapabilityClient(str(exc))

    tools = [
        *build_capability_tools(capability_client),
        *build_report_tools(report_client, site_id, resolved_site_name),
        *build_reading_tools(reading_client, site_id),
        *build_shutdown_tools(shutdown_client, site_id),
        *build_well_timeline_tools(well_timeline_client, site_id),
    ]
    model = build_model(
        api_key=settings.openrouter_api_key,
        model=settings.openrouter_model,
        base_url=settings.openrouter_base_url,
    )
    return answer_report_question(
        model=model,
        tools=tools,
        site_id=site_id,
        site_name=resolved_site_name,
        history=history,
        question=question,
    )


def _site_name_from_db(settings: Settings, site_id: int) -> str | None:
    try:
        return SiteClient.from_settings(settings).get_site_name(site_id)
    except Exception:
        logger.exception("Site lookup failed")
        return None


def _knowledge_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "knowledge" / "capabilities"

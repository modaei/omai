from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from omai.agents.chat_agent import (
    answer_chat_question,
    build_model,
    reasoning_effort_for_response_mode,
)
from omai.clients.capability_client import CapabilityClient, UnavailableCapabilityClient
from omai.clients.database_schema_client import (
    DatabaseSchemaClient,
    UnavailableDatabaseSchemaClient,
)
from omai.clients.reading_client import ReadingClient, UnavailableReadingClient
from omai.clients.report_client import ReportClient
from omai.clients.shutdown_client import ShutdownClient, UnavailableShutdownClient
from omai.clients.site_client import SiteClient
from omai.clients.well_timeline_client import (
    UnavailableWellTimelineClient,
    WellTimelineClient,
)
from omai.clients.work_order_client import UnavailableWorkOrderClient, WorkOrderClient
from omai.config.settings import Settings
from omai.rag.vector_store import (
    UnavailableOperationalContextStore,
    VectorOperationalContextStore,
)
from omai.tools.capability_tools import build_capability_tools
from omai.tools.database_schema_tools import build_database_schema_tools
from omai.tools.operational_context_tools import build_operational_context_tools
from omai.tools.reading_tools import build_reading_tools
from omai.tools.report_tools import build_report_tools
from omai.tools.shutdown_tools import build_shutdown_tools
from omai.tools.well_timeline_tools import build_well_timeline_tools
from omai.tools.work_order_tools import build_work_order_tools


logger = logging.getLogger(__name__)


def answer_chat(
    settings: Settings,
    site_id: int,
    site_name: str | None,
    history: list[dict[str, str]],
    question: str,
    response_mode: str = "faster",
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
        work_order_client = WorkOrderClient.from_settings(settings)
    except ValueError as exc:
        work_order_client = UnavailableWorkOrderClient(str(exc))
    try:
        capability_client = CapabilityClient(_knowledge_dir())
    except Exception as exc:
        capability_client = UnavailableCapabilityClient(str(exc))
    try:
        database_schema_client = DatabaseSchemaClient.from_settings(
            settings,
            _database_schema_path(),
        )
    except Exception as exc:
        database_schema_client = UnavailableDatabaseSchemaClient(str(exc))
    operational_context_store = _operational_context_store_from_settings(settings)

    tools = [
        *build_capability_tools(capability_client),
        *build_database_schema_tools(
            database_schema_client,
            site_id,
            max_rows=settings.operational_sql_max_rows,
        ),
        # This tool searches the vector DB derived index for notes/comments,
        # work-order context, shutdown explanations, alarms, and history.
        *build_operational_context_tools(operational_context_store, site_id),
        *build_report_tools(report_client, site_id, resolved_site_name),
        *build_reading_tools(reading_client, site_id),
        *build_shutdown_tools(shutdown_client, site_id),
        *build_well_timeline_tools(well_timeline_client, site_id),
        *build_work_order_tools(work_order_client, site_id),
    ]
    model = build_model(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        reasoning_effort=reasoning_effort_for_response_mode(response_mode),
    )
    return answer_chat_question(
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


def _database_schema_path() -> Path:
    return Path(__file__).resolve().parents[3] / "knowledge" / "database_schema.md"


@lru_cache(maxsize=8)
def _operational_context_store_from_settings(
    settings: Settings,
) -> VectorOperationalContextStore | UnavailableOperationalContextStore:
    try:
        # Operational RAG is optional at runtime. Keep the initialized vector DB
        # engine/embedding wrapper cached so each chat request does not rebuild
        # the same retrieval client.
        return VectorOperationalContextStore.from_settings(settings)
    except Exception as exc:
        return UnavailableOperationalContextStore(str(exc))

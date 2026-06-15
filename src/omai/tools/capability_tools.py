from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.clients.capability_client import CapabilityClient, CapabilityClientError


logger = logging.getLogger(__name__)


class SearchCapabilitiesInput(BaseModel):
    query: str = Field(
        description="User question about Ometrics capabilities, workflows, or which feature/report to use."
    )
    limit: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Maximum number of capability matches to return.",
    )


def _json_result(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def build_capability_tools(client: CapabilityClient) -> list[StructuredTool]:
    def search_ometrics_capabilities(query: str, limit: int = 3) -> str:
        """Search Ometrics product capability guidance."""
        logger.info("Searching Ometrics capabilities query=%s", query)
        try:
            return _json_result({"ok": True, **client.search(query, limit)})
        except CapabilityClientError as exc:
            logger.warning("Capability search failed: %s", exc)
            return _json_result({"ok": False, "error": str(exc)})

    return [
        StructuredTool.from_function(
            func=search_ometrics_capabilities,
            name="search_ometrics_capabilities",
            description=(
                "Search Ometrics capability and workflow guidance. Use this for "
                "questions like how to register a down well, which report to use, "
                "where to enter readings, how to find missing readings, or what "
                "Ometrics feature supports a business workflow. Answer only from "
                "the returned capability guidance; do not invent UI steps or options."
            ),
            args_schema=SearchCapabilitiesInput,
        )
    ]

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from omai.clients.rod_pump_analysis_client import (
    RodPumpAnalysisClient,
    RodPumpAnalysisError,
)


class RodPumpAnalysisInput(BaseModel):
    well_name: str = Field(description="Exact well name in the selected site.")
    start_time: str | None = Field(
        default=None,
        description="Optional ISO-8601 start time; defaults to 24 hours before end_time.",
    )
    end_time: str | None = Field(
        default=None,
        description="Optional ISO-8601 end time; defaults to now.",
    )


def _json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def build_rod_pump_analysis_tools(
    client: RodPumpAnalysisClient,
    site_id: int,
) -> list[StructuredTool]:
    """Expose site-bound analysis without allowing the model to choose site scope."""
    def analyze_rod_pump(
        well_name: str,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> str:
        try:
            return _json({"ok": True, **client.analyze(site_id, well_name, start_time, end_time)})
        except RodPumpAnalysisError as exc:
            return _json({"ok": False, "error": str(exc)})

    return [
        StructuredTool.from_function(
            func=analyze_rod_pump,
            name="analyze_rod_pump",
            description=(
                "Analyze one exact rod-pump well using its controller trends, every averaged "
                "surface and downhole dynograph pull, and chart-note history. Returns "
                "current diagnoses, severity, confidence, explainable evidence, health "
                "score, recommended review actions, and gated next-24-hour paraffin "
                "prediction status. Prefer this over SQL for rod-pump health questions."
            ),
            args_schema=RodPumpAnalysisInput,
        ),
    ]

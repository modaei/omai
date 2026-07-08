from __future__ import annotations

import json
import re
from time import perf_counter
from typing import Any, TypedDict

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph


class DataPointValueState(TypedDict, total=False):
    """State for deterministic current data-point value retrieval."""

    question: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    answer: str
    traces: list[dict[str, Any]]
    tool_timings: list[dict[str, Any]]


def try_answer_data_point_value_question(
    *, tools: list[BaseTool], question: str
) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """Answer common facility data-point value questions without an LLM call."""
    arguments = _parse_value_question(question)
    if arguments is None:
        return None
    tool = next((item for item in tools if item.name == "get_data_point_values"), None)
    if tool is None:
        return None

    graph = StateGraph(DataPointValueState)

    def fetch_value(state: DataPointValueState) -> dict[str, Any]:
        started_at = perf_counter()
        try:
            raw_result = tool.invoke(arguments)
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except Exception as exc:
            result = {"ok": False, "error": f"Data-point lookup failed: {exc}"}
        seconds = perf_counter() - started_at
        return {
            "result": result,
            "traces": [
                {"tool": "get_data_point_values", "arguments": arguments, "result": result}
            ],
            "tool_timings": [{"tool": "get_data_point_values", "seconds": seconds}],
        }

    def format_answer(state: DataPointValueState) -> dict[str, str]:
        return {"answer": _format_result(state["result"])}

    graph.add_node("get_data_point_values", fetch_value)
    graph.add_node("format_answer", format_answer)
    graph.add_edge(START, "get_data_point_values")
    graph.add_edge("get_data_point_values", "format_answer")
    graph.add_edge("format_answer", END)
    started_at = perf_counter()
    state = graph.compile().invoke({"question": question, "arguments": arguments})
    elapsed = perf_counter() - started_at
    timings = state.get("tool_timings", [])
    return (
        state["answer"],
        state.get("traces", []),
        {
            "total_seconds": elapsed,
            "model_seconds": 0.0,
            "tool_seconds": sum(item["seconds"] for item in timings),
            "model_calls": 0,
            "tool_calls": timings,
        },
    )


def _parse_value_question(question: str) -> dict[str, Any] | None:
    """Parse explicit data-point value questions and ignore other value domains.

    The data-point marker is mandatory. This keeps similarly phrased reading,
    tank, report, and production questions out of this deterministic route.
    """
    data_point_marker = re.compile(r"\bdata(?:\s|_|-)?point\b", re.IGNORECASE)
    if len(data_point_marker.findall(question)) != 1:
        return None

    normalized = " ".join(question.strip().rstrip("?.").split())
    normalized = data_point_marker.sub("data point", normalized)
    body = re.sub(
        r"^(?:what is|what's|show me|show|get|give me)\s+",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    body = re.sub(
        r"^(?:the\s+)?(?:current\s+)?(?:value\s+of\s+)?",
        "",
        body,
        flags=re.IGNORECASE,
    ).strip()

    # Point-first: "data point peak load for 4048" and the established
    # "value of data point peak load in 4048" form.
    point_first = re.fullmatch(
        r"data point\s+(.+?)\s+(?:in|for|on)\s+(.+)",
        body,
        flags=re.IGNORECASE,
    )
    if point_first:
        data_point_name = point_first.group(1).strip()
        location = point_first.group(2).strip()
    else:
        # Facility-first: "4048 data point peak load". Device selection remains
        # explicit through the `device` keyword in the location portion.
        facility_first = re.fullmatch(
            r"(.+?)\s+data point\s+(.+)",
            body,
            flags=re.IGNORECASE,
        )
        if not facility_first:
            return None
        location = facility_first.group(1).strip()
        data_point_name = facility_first.group(2).strip()

    facility_name, device_name = _parse_location(location)
    if not facility_name or not data_point_name:
        return None
    return {
        "facility_name": facility_name,
        "data_point_name": data_point_name,
        **({"device_name": device_name} if device_name else {}),
    }


def _parse_location(location: str) -> tuple[str, str | None]:
    """Split an explicit device selector while leaving facility shorthand intact."""
    device_match = re.fullmatch(
        r"(?:facility\s+)?(.+?)(?:,?\s+device\s+)(.+)",
        location,
        flags=re.IGNORECASE,
    )
    if device_match:
        facility_name = device_match.group(1).strip()
        device_name = device_match.group(2).strip()
    else:
        facility_name = re.sub(
            r"^(?:facility|well)\s+", "", location, flags=re.IGNORECASE
        ).strip()
        device_name = None
    facility_name = re.sub(r"(?:'s|’)\s*$", "", facility_name).strip()
    return facility_name, device_name


def _format_result(result: dict[str, Any]) -> str:
    """Format values while hiding a device selector defaulted from facility."""
    if not result.get("ok", True):
        return str(result.get("error") or "The data-point lookup failed.")
    status = result.get("status")
    if status == "ambiguous":
        candidates = ", ".join(str(item) for item in result.get("candidates", []))
        return (
            f"Multiple {str(result.get('selector', '')).replace('_', ' ')} names match "
            f"'{result.get('requested')}': {candidates}. Please specify one."
        )
    if status == "not_found":
        selector = str(result.get("selector", "data point")).replace("_", " ")
        return f"No matching {selector} was found for '{result.get('requested')}'."

    facility = result.get("facility_name")
    device_defaulted = bool(result.get("device_defaulted"))
    device = result.get("device_name")
    lines = []
    for item in result.get("values", []):
        target = f" for {facility}"
        if not device_defaulted:
            target += f" on {device}"
        value = item.get("value") if item.get("value_available") else "not available"
        received = item.get("received_at") or "time unavailable"
        lines.append(
            f"{item.get('data_point_name')}{target}: {value}. Last received: {received}."
        )
    return "\n".join(lines) if lines else "No current value is available."

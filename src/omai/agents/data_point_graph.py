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
    """Parse explicit data-point value questions and ignore other value domains."""
    data_point_marker = re.compile(r"\bdata(?:\s|_|-)?point\b", re.IGNORECASE)
    if not data_point_marker.search(question):
        return None
    # The marker establishes the domain but is not part of the telemetry selector.
    without_marker = data_point_marker.sub("", question)
    normalized = " ".join(without_marker.strip().rstrip("?.").split())
    match = re.match(
        r"^(?:what is|what's|show me|show|get|give me)\s+"
        r"(?:the\s+)?(?:current\s+)?value\s+of\s+(.+?)\s+"
        r"(?:in|for|on)\s+(.+)$",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    data_point_name = match.group(1).strip()
    location = match.group(2).strip()
    device_match = re.match(
        r"(?:facility\s+)?(.+?)(?:,?\s+device\s+)(.+)$",
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
    if not facility_name or not data_point_name:
        return None
    return {
        "facility_name": facility_name,
        "data_point_name": data_point_name,
        **({"device_name": device_name} if device_name else {}),
    }


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

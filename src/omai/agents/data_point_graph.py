from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date
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


class DataPointTrendState(TypedDict, total=False):
    """State for deterministic historical data-point trend analysis."""

    question: str
    arguments: dict[str, Any]
    result: dict[str, Any]
    answer: str
    traces: list[dict[str, Any]]
    tool_timings: list[dict[str, Any]]


EQUIPMENT_TYPES = {"tank", "pump", "treater"}
COMMON_TREND_POINT_SUFFIXES = [
    "pump fillage",
    "stroke length",
    "stroke min",
    "strokes per minute",
    "min load",
    "max load",
    "peak load",
    "level",
    "pressure",
    "temperature",
    "discharge",
]
MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


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


def try_answer_data_point_trend_question(
    *, tools: list[BaseTool], question: str, today: date | None = None
) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """Answer clear single-data-point trend questions without an LLM call."""
    arguments = _parse_trend_question(question, today=today or date.today())
    if arguments is None:
        return None
    tool = next((item for item in tools if item.name == "analyze_data_point_trend"), None)
    if tool is None:
        return None

    graph = StateGraph(DataPointTrendState)

    def fetch_trend(state: DataPointTrendState) -> dict[str, Any]:
        started_at = perf_counter()
        try:
            raw_result = tool.invoke(arguments)
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except Exception as exc:
            result = {"ok": False, "error": f"Data-point trend analysis failed: {exc}"}
        seconds = perf_counter() - started_at
        return {
            "result": result,
            "traces": [
                {"tool": "analyze_data_point_trend", "arguments": arguments, "result": result}
            ],
            "tool_timings": [{"tool": "analyze_data_point_trend", "seconds": seconds}],
        }

    def format_answer(state: DataPointTrendState) -> dict[str, str]:
        return {"answer": _format_trend_result(state["result"])}

    graph.add_node("analyze_data_point_trend", fetch_trend)
    graph.add_node("format_answer", format_answer)
    graph.add_edge(START, "analyze_data_point_trend")
    graph.add_edge("analyze_data_point_trend", "format_answer")
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


def _parse_trend_question(question: str, today: date | None = None) -> dict[str, Any] | None:
    """Parse obvious trend-analysis forms and leave uncertain phrasing to the LLM."""
    today = today or date.today()
    normalized = " ".join(question.strip().rstrip("?.").split())
    if not re.search(r"\b(?:analy[sz]e|trend|history)\b", normalized, re.IGNORECASE):
        return None
    date_range, normalized = _extract_date_range(normalized, today)
    days = _parse_days(normalized)
    cleaned = re.sub(
        r"\b(?:in\s+)?(?:the\s+)?past\s+\d+\s+days?\b|\blast\s+\d+\s+days?\b",
        "",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r"^(?:please\s+)?(?:analy[sz]e|show|get)\s+", "", cleaned, flags=re.IGNORECASE).strip()

    # Point-first: "pump fillage trend of 2510".
    point_first = re.fullmatch(
        r"(.+?)\s+trend\s+(?:of|for|in)\s+(.+)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if point_first:
        data_point_name = point_first.group(1).strip()
        location = _strip_trend_tail(point_first.group(2).strip())
        equipment_type, location = _extract_equipment_location(location)
        facility_name, device_name = _parse_location(location)
        if facility_name and data_point_name:
            equipment_type = equipment_type or _infer_equipment_type(
                facility_name, data_point_name
            )
            return {
                "facility_name": facility_name,
                "data_point_name": _canonical_trend_data_point_name(
                    equipment_type, data_point_name
                ),
                "days": days,
                **date_range,
                **({"equipment_type": equipment_type} if equipment_type else {}),
                **({"device_name": device_name} if device_name else {}),
            }

    # Explicit data point marker: "data point stroke min for 2510 trend".
    marker = re.fullmatch(
        r"data(?:\s|_|-)?point\s+(.+?)\s+(?:in|for|on)\s+(.+?)(?:\s+trend)?",
        cleaned,
        flags=re.IGNORECASE,
    )
    if marker:
        location = _strip_trend_tail(marker.group(2))
        equipment_type, location = _extract_equipment_location(location)
        facility_name, device_name = _parse_location(location)
        data_point_name = _strip_trend_tail(marker.group(1))
        equipment_type = equipment_type or _infer_equipment_type(
            facility_name, data_point_name
        )
        return {
            "facility_name": facility_name,
            "data_point_name": _canonical_trend_data_point_name(
                equipment_type, data_point_name
            ),
            "days": days,
            **date_range,
            **({"equipment_type": equipment_type} if equipment_type else {}),
            **({"device_name": device_name} if device_name else {}),
        }

    # Equipment-first: "tank 10-1 Oil level trend". The equipment type gives
    # the resolver enough context to find the equipment row before the data point.
    equipment_match = re.fullmatch(
        r"(tank|pump|treater)\s+(.+?)\s+trend",
        cleaned,
        flags=re.IGNORECASE,
    )
    if equipment_match:
        equipment_type = equipment_match.group(1).lower()
        target = equipment_match.group(2).strip()
        split = _split_equipment_and_point(target)
        if split is None:
            return None
        facility_name, data_point_name = split
        return {
            "facility_name": facility_name,
            "data_point_name": _canonical_trend_data_point_name(
                equipment_type, data_point_name
            ),
            "equipment_type": equipment_type,
            "days": days,
            **date_range,
        }

    # Equipment-first without an explicit trailing "trend": "tank 10-5 Water Level".
    equipment_match = re.fullmatch(
        r"(tank|pump|treater)\s+(.+)",
        cleaned,
        flags=re.IGNORECASE,
    )
    if equipment_match:
        equipment_type = equipment_match.group(1).lower()
        split = _split_equipment_and_point(equipment_match.group(2).strip())
        if split is None:
            return None
        facility_name, data_point_name = split
        return {
            "facility_name": facility_name,
            "data_point_name": _canonical_trend_data_point_name(
                equipment_type, data_point_name
            ),
            "equipment_type": equipment_type,
            "days": days,
            **date_range,
        }

    # Tank shorthand without the word "trend": "analyze 13-7 Water level".
    # Keep this narrow to level questions and tank-like selectors so unrelated
    # "analyze 2510 pressure" requests still fall through to the main agent.
    split = _split_equipment_and_point(cleaned)
    if split is not None:
        facility_name, data_point_name = split
        equipment_type = _infer_equipment_type(facility_name, data_point_name)
        if equipment_type:
            return {
                "facility_name": facility_name,
                "data_point_name": _canonical_trend_data_point_name(
                    equipment_type, data_point_name
                ),
                "equipment_type": equipment_type,
                "days": days,
                **date_range,
            }

    return None


def _parse_days(question: str) -> int:
    """Read a trailing-day interval, defaulting trend questions to one week."""
    match = re.search(
        r"\b(?:past|last)\s+(\d+)\s+days?\b",
        question,
        flags=re.IGNORECASE,
    )
    if not match:
        return 7
    return max(1, min(366, int(match.group(1))))


def _extract_date_range(question: str, today: date) -> tuple[dict[str, str], str]:
    """Extract explicit and rolling month ranges before selector parsing."""
    rolling_month_match = re.search(
        r"\b(?:in\s+)?(?:the\s+)?(?:past|last)\s+(\d+)\s+months?\b",
        question,
        flags=re.IGNORECASE,
    )
    if rolling_month_match:
        months = max(1, min(120, int(rolling_month_match.group(1))))
        start = _shift_months(today, -months)
        cleaned = (
            question[: rolling_month_match.start()]
            + question[rolling_month_match.end() :]
        ).strip()
        return {"start_date": start.isoformat(), "end_date": today.isoformat()}, cleaned

    month_match = re.search(
        r"\b(?:in|during|for)\s+("
        + "|".join(MONTHS)
        + r")(?:\s+(\d{4}))?\b",
        question,
        flags=re.IGNORECASE,
    )
    if not month_match:
        return {}, question
    month = MONTHS[month_match.group(1).lower()]
    year = int(month_match.group(2) or today.year)
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    cleaned = (question[: month_match.start()] + question[month_match.end() :]).strip()
    return {"start_date": start.isoformat(), "end_date": end.isoformat()}, cleaned


def _shift_months(value: date, months: int) -> date:
    """Move a date by whole months, clamping the day to the target month."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def _strip_trend_tail(value: str) -> str:
    return re.sub(r"\s+trend\s*$", "", value.strip(), flags=re.IGNORECASE).strip()


def _extract_equipment_location(location: str) -> tuple[str | None, str]:
    """Read an equipment-type prefix from location text such as "tank 13-7"."""
    match = re.fullmatch(
        r"(tank|pump|treater)\s+(.+)",
        location.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None, location
    return match.group(1).lower(), match.group(2).strip()


def _infer_equipment_type(facility_name: str, data_point_name: str) -> str | None:
    """Infer tank only for level requests with tank-like human selectors."""
    if _normalize_tokens(data_point_name) != "level":
        return None
    normalized_facility = _normalize_tokens(facility_name)
    if re.search(r"\b(?:tank|oil|water|float|reject)\b", normalized_facility):
        return "tank"
    if re.search(r"\b\d+\s*-\s*\d+", facility_name):
        return "tank"
    return None


def _split_equipment_and_point(value: str) -> tuple[str, str] | None:
    """Split "10-1 Oil level" into equipment selector and data-point selector."""
    normalized = value.lower()
    for suffix in COMMON_TREND_POINT_SUFFIXES:
        if normalized.endswith(f" {suffix}"):
            return value[: -len(suffix)].strip(), suffix
    parts = value.rsplit(" ", 1)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].strip()


def _canonical_trend_data_point_name(
    equipment_type: str | None,
    data_point_name: str,
) -> str:
    """Use DB-facing capitalization for common equipment telemetry names."""
    if equipment_type == "tank" and _normalize_tokens(data_point_name) == "level":
        return "Level"
    return data_point_name


def _normalize_tokens(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-zA-Z0-9]+", " ", value).lower()).strip()


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


def _format_trend_result(result: dict[str, Any]) -> str:
    """Render trend evidence as a concise graph-style interpretation."""
    if not result.get("ok", True):
        return str(result.get("error") or "The data-point trend analysis failed.")
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

    summary = result.get("summary", {})
    point = result.get("data_point_name")
    equipment = result.get("equipment") or {}
    target = equipment.get("name") or result.get("facility_name")
    interval = result.get("interval", {})
    if summary.get("status") == "no_data":
        return (
            f"No trend samples were returned for {point} on {target} "
            f"from {interval.get('start')} to {interval.get('end')}."
        )

    latest = summary.get("latest", {})
    first = summary.get("first", {})
    interval_label = result.get("interval_label") or f"the last {result.get('days')} day(s)"
    band = summary.get("typical_band") or {}
    band_low = band.get("low", summary.get("typical_low"))
    band_high = band.get("high", summary.get("typical_high"))
    lines = [_trend_opening(point, target, interval_label, first, latest, summary)]
    lines.append(
        _trend_behavior(
            point=point,
            summary=summary,
            band_low=band_low,
            band_high=band_high,
        )
    )
    changes = summary.get("significant_changes") or _changes_from_legacy_anomalies(
        summary.get("anomalies") or []
    )
    if changes:
        lines.append(_change_sentence(changes))
    else:
        lines.append(_variability_sentence(summary))
    coverage_note = _coverage_note(result, first, latest)
    if coverage_note:
        lines.append(coverage_note)
    return "\n".join(lines)


def _trend_opening(
    point: str,
    target: str,
    interval_label: str,
    first: dict[str, Any],
    latest: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    """Describe the first-to-last move without making it the only conclusion."""
    change = _format_number(summary.get("change"))
    change_percent = summary.get("change_percent")
    change_text = change
    if change_percent is not None:
        change_text += f" ({change_percent:.2f}%)"
    direction = {
        "rising": "finished higher",
        "falling": "finished lower",
        "stable": "finished close to where it started",
    }.get(str(summary.get("trend")), "changed")
    return (
        f"{point} on {target} over {interval_label} started at "
        f"{_format_number(first.get('value'))} on {first.get('time')} and "
        f"{direction} at {_format_number(latest.get('value'))} on {latest.get('time')}, "
        f"a change of {change_text}."
    )


def _trend_behavior(
    *,
    point: str,
    summary: dict[str, Any],
    band_low: Any,
    band_high: Any,
) -> str:
    """Describe the visible pattern and normal range of the graph."""
    shape = str(summary.get("overall_shape") or summary.get("pattern") or "stable")
    variability = str(summary.get("variability") or "moderate")
    band_text = ""
    if band_low is not None and band_high is not None:
        if abs(float(band_high) - float(band_low)) < 0.000001:
            band_text = f", usually staying around {_format_number(band_low)}"
        else:
            band_text = (
                f", usually staying between {_format_number(band_low)} and "
                f"{_format_number(band_high)}"
            )

    shape_text = {
        "mostly_flat": "was mostly flat",
        "oscillating": "mostly oscillated",
        "gradual_increase": "showed a gradual increase",
        "gradual_decrease": "showed a gradual decrease",
        "step_increase": "showed a clear upward level shift",
        "step_decrease": "showed a clear downward level shift",
        "late_increase": "turned upward late in the period",
        "late_decrease": "turned downward late in the period",
        "decline_then_recovery": "showed an initial decline followed by recovery",
        "increase_then_decline": "rose first and then declined",
        "highly_variable": "was highly variable without one sustained direction",
    }.get(shape, "showed no clear sustained direction")
    variability_text = {
        "low": "low short-term variability",
        "moderate": "moderate short-term variability",
        "high": "high short-term variability",
    }.get(variability, "moderate short-term variability")
    return f"Overall, {point} {shape_text}{band_text}, with {variability_text}."


def _change_sentence(changes: list[dict[str, Any]]) -> str:
    """Render the largest sudden changes inline instead of a separate anomaly section."""
    parts = []
    for change in changes[:3]:
        direction = "rose" if change.get("direction") == "spike" else "dropped"
        raw_magnitude = abs(float(change.get("change", 0) or 0))
        percent = change.get("change_percent")
        percent_text = f" ({abs(float(percent)):.2f}%)" if percent is not None else ""
        detail = (
            f"around {change.get('time')}, it {direction} from "
            f"{_format_number(change.get('from'))} to {_format_number(change.get('to'))}"
        )
        if raw_magnitude > 0:
            detail += f", a change of {_format_number(raw_magnitude)}{percent_text}"
        parts.append(detail)
    if len(parts) == 1:
        return f"The largest sudden movement occurred {parts[0]}."
    return "The largest sudden movements occurred " + "; ".join(parts) + "."


def _variability_sentence(summary: dict[str, Any]) -> str:
    """Close with variability when there are no specific spikes/drops to call out."""
    variability = str(summary.get("variability") or "moderate")
    if variability == "low":
        return "No major sudden spike or drop is visible in the returned samples."
    if variability == "high":
        return "The graph has repeated short-term rises and falls, but no single sudden movement clearly dominates."
    return "The graph has some short-term movement, but no major sudden spike or drop is visible in the returned samples."


def _changes_from_legacy_anomalies(anomalies: list[Any]) -> list[dict[str, Any]]:
    """Keep older tool-result test doubles readable until all callers return change evidence."""
    changes = []
    for anomaly in anomalies:
        text = str(anomaly)
        zero = re.search(r"Drop to zero at (.+?)(?:;|$)", text)
        if zero:
            changes.append(
                {
                    "direction": "drop",
                    "time": zero.group(1),
                    "from": "normal range",
                    "to": 0,
                    "change": 0,
                    "change_percent": None,
                }
            )
            continue
        spike = re.search(
            r"Abnormal high spike(?:/plateau)? (?:from|at) (.+?)(?: to .+?)?, peaking at ([\d,.\-]+)",
            text,
        )
        if spike:
            changes.append(
                {
                    "direction": "spike",
                    "time": spike.group(1).rstrip("."),
                    "from": "normal range",
                    "to": spike.group(2).rstrip("."),
                    "change": 0,
                    "change_percent": None,
                }
            )
    return changes


def _format_number(value: Any) -> str:
    if value is None:
        return "not available"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{numeric:,.2f}".rstrip("0").rstrip(".")


def _coverage_note(
    result: dict[str, Any],
    first: dict[str, Any],
    latest: dict[str, Any],
) -> str | None:
    """Mention when Graphite returned only part of a requested date range."""
    start_date = result.get("start_date")
    end_date = result.get("end_date")
    if not start_date or not end_date:
        return None
    first_day = _timestamp_date(first.get("timestamp"))
    latest_day = _timestamp_date(latest.get("timestamp"))
    if first_day is None or latest_day is None:
        return None
    if first_day <= start_date and latest_day >= end_date:
        return None
    return (
        f"Returned samples cover {first_day} to {latest_day}, not the full requested "
        f"{start_date} to {end_date} range."
    )


def _timestamp_date(value: Any) -> str | None:
    if not value:
        return None
    try:
        return str(value)[:10]
    except Exception:
        return None

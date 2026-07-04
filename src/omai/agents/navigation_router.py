from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date
from time import perf_counter
from typing import Any

from langchain_core.tools import BaseTool

from omai.agents.producing_wells_graph import MONTHS, _resolve_date_range


DeterministicAnswer = tuple[str, list[dict[str, Any]], dict[str, Any]]


VIEW_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("production_allocation", ("production allocation",)),
    ("injection_allocation", ("injection allocation",)),
    ("oil_production", ("oil production",)),
    ("oil_sale", ("oil sales", "oil sale")),
    ("water_production", ("water production",)),
    ("water_transfer", ("water transfer",)),
    ("water_injection", ("water injection",)),
    ("flared_vent", ("gas flared", "gas vented", "flared gas", "vented gas")),
    ("fuel_gas", ("fuel gas",)),
    ("battery", ("battery report",)),
    ("flow_meter_readings", ("flow meter reading", "flowmeter reading")),
    ("water_plant_readings", ("water plant reading",)),
    ("tank_readings", ("tank reading",)),
    ("flare_readings", ("flare reading",)),
    ("pump_readings", ("pump reading",)),
    ("treater_readings", ("treater reading",)),
    ("knockout_readings", ("knockout reading", "knock out reading")),
    ("lact_readings", ("lact reading",)),
    ("well_tests", ("well test",)),
    ("well_fluids", ("well fluid level", "well fluid", "fluid level")),
    ("well_injections", ("well injection", "injection reading")),
    ("short_shutdowns", ("short shutdown", "hourly shutdown")),
    ("long_shutdowns", ("long shutdown",)),
    ("shutdowns", ("shutdown",)),
    ("general_notes", ("general note",)),
    ("work_orders", ("work order",)),
    ("run_tickets", ("run ticket",)),
    ("water_draws", ("water draw",)),
)


ENTRY_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("flow_meter_reading", ("flow meter reading", "flowmeter reading")),
    ("water_plant_reading", ("water plant reading",)),
    ("knockout_reading", ("knockout reading", "knock out reading")),
    ("flare_reading", ("flare reading",)),
    ("pump_reading", ("pump reading",)),
    ("treater_reading", ("treater reading",)),
    ("lact_reading", ("lact reading",)),
    ("tank_reading", ("tank reading",)),
    ("well_test", ("well test",)),
    ("well_fluid", ("well fluid level", "fluid level")),
    ("well_injection", ("well injection", "injection reading")),
    ("well_shutdown", ("well shutdown", "long shutdown", "short shutdown", "shutdown")),
    ("water_draw", ("water draw",)),
    ("run_ticket", ("run ticket",)),
    ("general_note", ("general note",)),
    ("work_order", ("work order",)),
)


ENTRY_FIELDS: dict[str, tuple[str, ...]] = {
    "flare_reading": ("pressure", "volume"),
    "flow_meter_reading": ("total", "flow", "odometer"),
    "pump_reading": ("suction_pressure", "discharge_pressure"),
    "treater_reading": ("oil_intake", "pressure", "temperature"),
    "knockout_reading": ("inlet", "oil_off"),
    "water_plant_reading": ("flow_rate", "suction_pressure", "discharge_pressure"),
    "lact_reading": ("reading", "temperature", "bs_w"),
    "well_test": (
        "oil",
        "water",
        "gas",
        "pip",
        "m_temp",
        "amps",
        "tbgp",
        "csgp",
        "fluid_level",
        "runtime",
    ),
    "well_fluid": ("level",),
    "well_injection": ("flow_rate", "total", "tbg", "csg"),
    "well_shutdown": ("hours",),
    "work_order": ("cost_estimate", "final_cost", "priority"),
}


READING_ENTITY_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("flow_meter_reading", ("flow meter", "flowmeter")),
    ("water_plant_reading", ("water plant",)),
    ("knockout_reading", ("knockout", "knock out")),
    ("flare_reading", ("flare",)),
    ("pump_reading", ("pump",)),
    ("treater_reading", ("treater",)),
    ("lact_reading", ("lact",)),
    ("tank_reading", ("tank",)),
)


def try_answer_navigation_request(
    *,
    tools: list[BaseTool],
    question: str,
    today: date | None = None,
) -> DeterministicAnswer | None:
    """Route unambiguous view and form-navigation requests without an LLM call."""
    current_date = today or date.today()
    route = _parse_view_request(question, current_date)
    tool_name = "prepare_data_view"
    if route is None:
        route = _parse_data_entry_request(question, current_date)
        tool_name = "prepare_data_entry"
    if route is None:
        return None

    tool = next((item for item in tools if item.name == tool_name), None)
    if tool is None:
        return None
    started_at = perf_counter()
    tool_started_at = perf_counter()
    try:
        raw_result = tool.invoke(route)
        result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except Exception as exc:
        result = {"status": "invalid", "message": f"Navigation failed: {exc}"}
        raw_result = json.dumps(result, separators=(",", ":"))
    tool_seconds = perf_counter() - tool_started_at
    trace = {"tool": tool_name, "arguments": route, "result": raw_result}
    return (
        _format_navigation_result(result, tool_name),
        [trace],
        {
            "total_seconds": perf_counter() - started_at,
            "model_seconds": 0.0,
            "tool_seconds": tool_seconds,
            "model_calls": 0,
            "tool_calls": [{"tool": tool_name, "seconds": tool_seconds}],
        },
    )


def _parse_view_request(question: str, today: date) -> dict[str, Any] | None:
    """Parse explicit show-me navigation while rejecting uncertain destinations."""
    normalized = _normalize(question)
    if not normalized.startswith("show me "):
        return None
    matches = _alias_matches(normalized, VIEW_ALIASES)
    if len(matches) != 1:
        return None
    arguments: dict[str, Any] = {"view_type": matches[0]}
    date_range = _safe_date_range(normalized, today)
    if _contains_date_marker(normalized) and date_range is None:
        return None
    if date_range:
        arguments["start_date"] = date_range[0].isoformat()
        arguments["end_date"] = date_range[1].isoformat()
    search_text = _view_search_text(normalized, today)
    if search_text:
        arguments["search_text"] = search_text
    if matches[0] == "work_orders":
        status_match = re.search(r"\b(open|closed|all)\s+work orders?\b", normalized)
        if status_match:
            arguments["status"] = status_match.group(1)
    return arguments


def _parse_data_entry_request(question: str, today: date) -> dict[str, Any] | None:
    """Parse one explicit form request, falling through when values may be lost."""
    normalized = _normalize(question)
    if not re.search(r"\b(create|add|enter|record|register)\b", normalized):
        return None
    equipment_terms = re.findall(
        r"\b(lact|flare|pump|treater|tank|flow meter|flowmeter|water plant|"
        r"knockout|knock out|well)\b",
        normalized,
    )
    if " and " in normalized and len(set(equipment_terms)) > 1:
        return None
    matches = _alias_matches(normalized, ENTRY_ALIASES)
    if len(matches) > 1:
        return None
    if not matches:
        if not re.search(r"\b(?:create|add|enter|record|register)\b.*\breading\b", normalized):
            return None
        entry_type = "generic_reading"
    else:
        entry_type = matches[0]

    date_range = _safe_date_range(normalized, today)
    if _contains_date_marker(normalized) and date_range is None:
        return None
    entity_name = _entry_entity_name(normalized, entry_type, today)
    if entry_type == "generic_reading" and entity_name:
        inferred_types = _reading_types_from_entity(entity_name)
        if len(inferred_types) > 1:
            return None
        if inferred_types:
            entry_type = inferred_types[0]

    # Field extraction must happen after generic requests have been resolved to a
    # concrete reading type. Otherwise type-specific values remain unexplained.
    values, consumed_numeric_spans = _entry_values(normalized, entry_type, today)
    entity_numbers = set(re.findall(r"[-+]?\d+(?:\.\d+)?", entity_name or ""))
    # Unknown numbers often represent form values. Let the model interpret them
    # rather than silently opening a form with incomplete prefilled data.
    for numeric_match in re.finditer(r"(?<![a-z])[-+]?\d+(?:\.\d+)?", normalized):
        if _span_is_consumed(numeric_match.span(), consumed_numeric_spans):
            continue
        if numeric_match.group() in entity_numbers:
            continue
        if _number_is_part_of_date(normalized, numeric_match.span()):
            continue
        return None

    arguments: dict[str, Any] = {"entry_type": entry_type, "values": values}
    if entity_name:
        arguments["entity_name"] = entity_name
    if entry_type == "well_shutdown":
        values["long_shutdown"] = "long shutdown" in normalized
    return arguments


def _reading_types_from_entity(entity_name: str) -> list[str]:
    """Infer reading types from explicit equipment words in an entity phrase."""
    matches = []
    for entry_type, markers in READING_ENTITY_MARKERS:
        if any(re.search(rf"\b{re.escape(marker)}\b", entity_name) for marker in markers):
            matches.append(entry_type)
    return list(dict.fromkeys(matches))


def _alias_matches(
    text: str,
    aliases: tuple[tuple[str, tuple[str, ...]], ...],
) -> list[str]:
    """Return unique canonical types whose complete aliases occur in the text."""
    found = []
    for canonical, variants in aliases:
        if any(re.search(rf"\b{re.escape(alias)}s?\b", text) for alias in variants):
            found.append(canonical)
    # A specific alias also contains broader words such as "shutdowns". Keep the
    # first, most-specific mapping declared for the same concept.
    if "shutdowns" in found and any(
        item in found for item in ("short_shutdowns", "long_shutdowns")
    ):
        found.remove("shutdowns")
    return list(dict.fromkeys(found))


def _safe_date_range(text: str, today: date) -> tuple[date, date] | None:
    """Resolve supported relative, ISO, and US-formatted dates without guessing."""
    explicit_range = re.search(
        r"\bfrom\s+(.+?)\s+(?:until|to|through)\s+(.+?)"
        r"(?:\s+for\s+.+)?\s*$",
        text,
    )
    if not explicit_range:
        explicit_range = re.search(
            r"\bbetween\s+(.+?)\s+and\s+(.+?)(?:\s+for\s+.+)?\s*$",
            text,
        )
    if explicit_range:
        start = _parse_date_endpoint(explicit_range.group(1), today, "start")
        end = _parse_date_endpoint(explicit_range.group(2), today, "end")
        return (start, end) if start and end else None

    named_dates = re.findall(
        rf"\b({'|'.join(MONTHS)})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
        r"(?:,?\s+(\d{4}))?\b",
        text,
    )
    if named_dates:
        try:
            values = [
                date(int(year or today.year), MONTHS[month], int(day))
                for month, day, year in named_dates[:2]
            ]
        except ValueError:
            return None
        return values[0], values[-1]
    us_dates = re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if us_dates:
        try:
            values = [
                date(int(year), int(month), int(day))
                for month, day, year in us_dates[:2]
            ]
        except ValueError:
            return None
        return values[0], values[-1]
    try:
        return _resolve_date_range(text, today)
    except ValueError:
        return None


def _parse_date_endpoint(
    value: str,
    today: date,
    boundary: str,
) -> date | None:
    """Parse one boundary in an explicit from/between date expression."""
    normalized = value.strip(" ,.?\t\n").lower()
    if normalized in {"today", "now"}:
        return today
    if normalized == "yesterday":
        return date.fromordinal(today.toordinal() - 1)

    iso_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", normalized)
    us_match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", normalized)
    named_match = re.fullmatch(
        rf"({'|'.join(MONTHS)})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
        r"(?:,?\s+(\d{4}))?",
        normalized,
    )
    month_match = re.fullmatch(
        rf"({'|'.join(MONTHS)})(?:\s+(\d{{4}}))?",
        normalized,
    )
    try:
        if iso_match:
            return date(*(int(part) for part in iso_match.groups()))
        if us_match:
            month, day, year = (int(part) for part in us_match.groups())
            return date(year, month, day)
        if named_match:
            month_name, day, year = named_match.groups()
            return date(int(year or today.year), MONTHS[month_name], int(day))
        if month_match:
            month_name, year = month_match.groups()
            resolved_year = int(year or today.year)
            month = MONTHS[month_name]
            day = 1 if boundary == "start" else monthrange(resolved_year, month)[1]
            return date(resolved_year, month, day)
    except ValueError:
        return None
    return None


def _contains_date_marker(text: str) -> bool:
    """Identify date-like text so malformed dates do not silently lose filters."""
    return bool(
        re.search(
            r"\b(today|yesterday|this month|last month|last \d+ days?|"
            r"past \d+ days?|january|february|march|april|may|june|july|"
            r"august|september|october|november|december)\b",
            text,
        )
        or re.search(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{4}\b", text)
    )


def _view_search_text(text: str, today: date) -> str | None:
    """Extract a plain index query after `for`, excluding a trailing date phrase."""
    match = re.search(r"\bfor\s+(.+)$", text)
    if not match:
        return None
    candidate = match.group(1).strip(" .?")
    candidate = re.split(
        r"\s+(?:on|in|during|from|between)\s+(?=(?:today|yesterday|this|last|past|"
        r"january|february|march|april|may|june|july|august|september|october|"
        r"november|december|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}))",
        candidate,
        maxsplit=1,
    )[0]
    if _safe_date_range(candidate, today):
        return None
    return candidate.strip() or None


def _entry_values(
    text: str,
    entry_type: str,
    today: date,
) -> tuple[dict[str, Any], list[tuple[int, int]]]:
    """Extract explicitly labelled scalar values and a single entry date."""
    values: dict[str, Any] = {}
    consumed = []
    date_range = _safe_date_range(text, today)
    if date_range and date_range[0] == date_range[1]:
        values["time" if entry_type == "run_ticket" else "date"] = date_range[0].isoformat()
    for field in ENTRY_FIELDS.get(entry_type, ()):
        label = field.replace("_", r"[ _-]")
        match = re.search(rf"\b{label}\s*(?:is|=|:)?\s*([-+]?\d+(?:\.\d+)?)\b", text)
        if match:
            values[field] = float(match.group(1))
            consumed.append(match.span(1))
    comments = re.search(r"\bcomments?\s*(?:is|=|:)?\s*[\"']([^\"']+)[\"']", text)
    if not comments:
        comments = re.search(r"\bcomments?\s*(?:is|=|:)?\s+(.+)$", text)
    if comments:
        values["comments"] = comments.group(1).strip()
        consumed.extend(_numeric_spans_in_group(comments, 1))
    subject = re.search(r"\bsubject\s*(?:is|=|:)?\s*[\"']([^\"']+)[\"']", text)
    if subject and entry_type == "work_order":
        values["subject"] = subject.group(1).strip()
        consumed.extend(_numeric_spans_in_group(subject, 1))
    return values, consumed


def _numeric_spans_in_group(
    match: re.Match[str],
    group: int,
) -> list[tuple[int, int]]:
    """Return absolute spans for numbers inside an explicitly captured text value."""
    offset = match.start(group)
    return [
        (offset + item.start(), offset + item.end())
        for item in re.finditer(r"[-+]?\d+(?:\.\d+)?", match.group(group))
    ]


def _entry_entity_name(text: str, entry_type: str, today: date) -> str | None:
    """Extract only an entity introduced by an explicit `for` preposition."""
    if entry_type in {"general_note", "work_order"}:
        return None
    match = re.search(r"\bfor\s+(.+)$", text)
    if not match:
        return None
    candidate = match.group(1).strip(" .?")
    candidate = re.split(r"\s+with\s+", candidate, maxsplit=1)[0]
    candidate = re.split(
        r"\s+(?:on|at)\s+(?=(?:today|yesterday|\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}/\d{1,2}/\d{4}))",
        candidate,
        maxsplit=1,
    )[0]
    return None if _safe_date_range(candidate, today) else candidate.strip() or None


def _span_is_consumed(
    span: tuple[int, int], consumed: list[tuple[int, int]]
) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in consumed)


def _number_is_part_of_date(text: str, span: tuple[int, int]) -> bool:
    context = text[max(0, span[0] - 20) : min(len(text), span[1] + 20)]
    return bool(
        re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}", context)
        or re.search(r"\b(?:last|past)\s+\d+\s+days?\b", context)
        or re.search(
            rf"\b(?:{'|'.join(MONTHS)})\s+\d{{1,2}}(?:st|nd|rd|th)?"
            r"(?:,?\s+\d{4})?\b",
            context,
        )
    )


def _format_navigation_result(result: dict[str, Any], tool_name: str) -> str:
    """Convert validated client statuses into concise user-facing responses."""
    status = result.get("status")
    if status == "ready":
        if tool_name == "prepare_data_entry":
            return "Opening the prefilled data-entry form."
        return "Opening the requested page."
    message = str(result.get("message") or "The request could not be prepared.")
    candidates = result.get("candidates") or []
    if candidates:
        labels = []
        for item in candidates:
            label = (
                f"{item.get('type')} - {item.get('name')}"
                if item.get("type")
                else str(item.get("name"))
            )
            labels.append(label)
        return f"{message} Candidates: {', '.join(labels)}"
    return message


def _normalize(value: str) -> str:
    return " ".join(value.lower().strip().rstrip("?.").split())

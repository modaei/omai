from __future__ import annotations

import json
import re
from datetime import date
from time import perf_counter
from typing import Any, Literal, TypedDict

from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from omai.agents.producing_wells_graph import _resolve_date_range


class WellTestAnalysisState(TypedDict, total=False):
    """State for deterministic well-test analysis and dependency prefetching."""

    question: str
    analysis_mode: Literal["latest_previous", "range_summary"]
    group_by: Literal["well", "battery", "site"]
    arguments: dict[str, Any]
    result: dict[str, Any]
    answer: str
    traces: list[dict[str, Any]]
    tool_timings: list[dict[str, Any]]


class WellTestAnalysisDependency(TypedDict):
    """Structured well-test analysis supplied to the general agent."""

    context: str
    traces: list[dict[str, Any]]
    stats: dict[str, Any]
    tool_name: str


def try_answer_well_test_analysis(
    *,
    tools: list[BaseTool],
    question: str,
    history: list[dict[str, str]],
    today: date | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """Answer complete, explicit well-test analyses without an LLM call."""
    route = _classify_well_test_analysis(question, history, today or date.today())
    if route is None or route["route_type"] != "terminal":
        return None
    tool = _tool_by_name(tools, "analyze_well_tests")
    if tool is None:
        return None

    state, elapsed = _run_analysis_graph(tool, question, route["arguments"])
    result = state["result"]
    answer = _format_analysis_result(result)
    timings = state.get("tool_timings", [])
    return answer, state.get("traces", []), _stats(elapsed, timings)


def prepare_well_test_analysis_dependency(
    *,
    tools: list[BaseTool],
    question: str,
    history: list[dict[str, str]],
    today: date | None = None,
) -> WellTestAnalysisDependency | None:
    """Prefetch well-test analysis needed by a broader multi-tool answer."""
    route = _classify_well_test_analysis(question, history, today or date.today())
    if route is None or route["route_type"] != "dependency":
        return None
    tool = _tool_by_name(tools, "analyze_well_tests")
    if tool is None:
        return None

    state, elapsed = _run_analysis_graph(tool, question, route["arguments"])
    result = state["result"]
    context = {
        "authoritative_data_type": "well_test_analysis",
        "tool": "analyze_well_tests",
        "arguments": route["arguments"],
        "result": result,
    }
    timings = state.get("tool_timings", [])
    return {
        "context": json.dumps(context, separators=(",", ":"), default=str),
        "traces": state.get("traces", []),
        "stats": _stats(elapsed, timings),
        "tool_name": "analyze_well_tests",
    }


def _run_analysis_graph(
    tool: BaseTool,
    question: str,
    arguments: dict[str, Any],
) -> tuple[WellTestAnalysisState, float]:
    """Execute the fixed analysis node and preserve tool diagnostics."""
    graph = StateGraph(WellTestAnalysisState)

    def analyze(state: WellTestAnalysisState) -> dict[str, Any]:
        started_at = perf_counter()
        try:
            raw_result = tool.invoke(arguments)
            result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
        except Exception as exc:
            result = {"ok": False, "error": f"Well-test analysis failed: {exc}"}
        seconds = perf_counter() - started_at
        return {
            "result": result,
            "traces": [
                {"tool": "analyze_well_tests", "arguments": arguments, "result": result}
            ],
            "tool_timings": [{"tool": "analyze_well_tests", "seconds": seconds}],
        }

    graph.add_node("analyze_well_tests", analyze)
    graph.add_edge(START, "analyze_well_tests")
    graph.add_edge("analyze_well_tests", END)
    started_at = perf_counter()
    state = graph.compile().invoke(
        {"question": question, "arguments": arguments, "traces": [], "tool_timings": []}
    )
    return state, perf_counter() - started_at


def _classify_well_test_analysis(
    question: str,
    history: list[dict[str, str]],
    today: date,
) -> dict[str, Any] | None:
    """Resolve explicit analysis intent, including short contextual follow-ups."""
    current = " ".join(question.lower().split())
    prior = _latest_well_test_user_question(history)
    has_current_subject = bool(re.search(r"\bwell[ -]?tests?\b", current))
    is_follow_up = bool(
        prior
        and re.search(
            r"\b(them|those|each|previous|previus|prior|before|break(?:\s+it)?\s+down|by battery|whole site)\b",
            current,
        )
    )
    if not has_current_subject and not is_follow_up:
        return None

    contextual = f"{prior} {current}".strip() if is_follow_up else current
    latest_previous = bool(
        re.search(
            r"\b(latest|last|most recent)\b.*\b(previous|previus|prior|before|last test)\b",
            contextual,
        )
        or re.search(r"\bcompare\b.*\b(previous|previus|prior|before)\b", contextual)
        or re.search(r"\btest one before\b", contextual)
    )
    range_summary = bool(
        re.search(
            r"\b(analy[sz]e|summary|summarize|average|avg|sum|total|minimum|maximum|min|max|breakdown|break down)\b",
            contextual,
        )
    )
    if not latest_previous and not range_summary:
        return None

    analysis_mode = "latest_previous" if latest_previous else "range_summary"
    if re.search(r"\b(?:by|per)\s+batter(?:y|ies)\b|\bbattery breakdown\b", contextual):
        group_by = "battery"
    elif re.search(r"\b(?:by|per|each)\s+well\b|\bfor each well\b", contextual):
        group_by = "well"
    elif re.search(r"\b(?:whole|entire|selected) site\b|\b(?:by|per) site\b", contextual):
        group_by = "site"
    else:
        group_by = "well" if analysis_mode == "latest_previous" else "site"

    arguments: dict[str, Any] = {
        "analysis_mode": analysis_mode,
        "group_by": group_by,
    }
    resolved_dates = _resolve_date_range(contextual, today)
    if resolved_dates:
        arguments["start_date"] = resolved_dates[0].isoformat()
        arguments["end_date"] = resolved_dates[1].isoformat()
    elif analysis_mode == "range_summary":
        return None

    battery_match = re.search(r"\bbattery\s+([a-z0-9_-]+)\b", current, re.IGNORECASE)
    if battery_match and battery_match.group(1).lower() not in {"breakdown", "summary"}:
        arguments["battery_name"] = battery_match.group(1)
    well_match = re.search(r"\bwell\s+([a-z0-9_-]*\d[a-z0-9_-]*)\b", current, re.IGNORECASE)
    if well_match:
        arguments["well_name"] = well_match.group(1)

    composite_terms = re.search(
        r"\b(shutdowns?|alarms?|notes?|work orders?|production|allocation|"
        r"injection|flares?|tanks?|why|causes?|explain|correlat\w*|impact)\b",
        current,
    )
    return {
        "route_type": "dependency" if composite_terms else "terminal",
        "arguments": arguments,
    }


def _latest_well_test_user_question(history: list[dict[str, str]]) -> str:
    """Find the latest user turn that established a well-test subject or period."""
    for item in reversed(history[-10:]):
        if item.get("role") != "user":
            continue
        content = " ".join(str(item.get("content", "")).lower().split())
        if re.search(r"\bwell[ -]?tests?\b", content):
            return content
    return ""


def _format_analysis_result(result: dict[str, Any]) -> str:
    """Format complete analyses consistently without a final model call."""
    if not result.get("ok", True):
        return str(result.get("error") or "Well-test analysis could not be completed.")
    groups = result.get("groups", [])
    if not groups:
        return "No matching well tests were found."
    mode = result.get("analysis_mode")
    group_by = result.get("group_by")
    if mode == "latest_previous":
        lines = [
            f"Compared the latest and previous well tests for {len(groups)} {group_by} group(s)."
        ]
        lines.extend(_format_latest_previous_group(group, group_by) for group in groups)
        return "\n".join(lines)

    start = _display_date(result.get("start_date"))
    end = _display_date(result.get("end_date"))
    lines = [f"Well-test summary from {start} through {end} by {group_by}:"]
    lines.extend(_format_range_group(group) for group in groups)
    return "\n".join(lines)


def _format_latest_previous_group(group: dict[str, Any], group_by: str) -> str:
    name = str(group.get("group_name", "Unknown"))
    if group_by == "well":
        dates = f"{_display_date(group.get('previous_date'))} -> {_display_date(group.get('latest_date'))}"
        return (
            f"- {name} ({dates}): "
            f"oil {_transition(group, 'oil')} bbl; "
            f"water {_transition(group, 'water')} bbl; "
            f"gas {_transition(group, 'gas')}; "
            f"runtime {_transition(group, 'runtime')} hours"
        )
    return (
        f"- {name} ({group.get('well_count', 0)} wells): "
        f"oil {_aggregate_transition(group, 'oil')} bbl; "
        f"water {_aggregate_transition(group, 'water')} bbl; "
        f"gas {_aggregate_transition(group, 'gas')}"
    )


def _format_range_group(group: dict[str, Any]) -> str:
    return (
        f"- {group.get('group_name', 'Unknown')}: {group.get('test_count', 0)} tests, "
        f"{group.get('well_count', 0)} wells; "
        f"oil total {_number(group.get('oil_sum'))} bbl, average {_number(group.get('oil_average'))} bbl; "
        f"water total {_number(group.get('water_sum'))} bbl, average {_number(group.get('water_average'))} bbl; "
        f"gas total {_number(group.get('gas_sum'))}, average {_number(group.get('gas_average'))}"
    )


def _transition(group: dict[str, Any], metric: str) -> str:
    return (
        f"{_number(group.get(f'previous_{metric}'))} -> "
        f"{_number(group.get(f'latest_{metric}'))} "
        f"({_signed(group.get(f'{metric}_change'))})"
    )


def _aggregate_transition(group: dict[str, Any], metric: str) -> str:
    return (
        f"{_number(group.get(f'previous_{metric}_sum'))} -> "
        f"{_number(group.get(f'latest_{metric}_sum'))} "
        f"({_signed(group.get(f'{metric}_sum_change'))})"
    )


def _number(value: Any) -> str:
    if value is None:
        return "not recorded"
    number = float(value)
    return f"{number:,.3f}".rstrip("0").rstrip(".")


def _signed(value: Any) -> str:
    if value is None:
        return "change not available"
    number = float(value)
    return f"{number:+,.3f}".rstrip("0").rstrip(".")


def _display_date(value: Any) -> str:
    if not value:
        return "date unavailable"
    return date.fromisoformat(str(value)[:10]).strftime("%m/%d/%Y")


def _tool_by_name(tools: list[BaseTool], name: str) -> BaseTool | None:
    return next((tool for tool in tools if tool.name == name), None)


def _stats(elapsed: float, timings: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "total_seconds": elapsed,
        "model_seconds": 0.0,
        "tool_seconds": sum(item["seconds"] for item in timings),
        "model_calls": 0,
        "tool_calls": timings,
    }

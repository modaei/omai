from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Callable, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field


class InvestigationCancelled(RuntimeError):
    """Raised between graph stages when the user has requested cancellation."""


Metric = Literal[
    "oil_production",
    "water_production",
    "gas_production",
    "water_injection",
    "water_transfer",
    "gas_flared",
    "gas_vented",
    "gas_flared_vented",
    "gas_fuel",
    "oil_sale",
    "production_allocation",
    "injection_allocation",
    "tank_inventory",
    "equipment_reading",
    "well_status",
    "operational_context",
]


class InvestigationPlan(BaseModel):
    """Typed plan that can be translated directly into existing domain tools."""

    metric: Metric
    scope_type: Literal["site", "battery", "well", "equipment"] = "site"
    scope_name: str | None = None
    reading_type: str | None = Field(
        default=None,
        description="Required only for equipment_reading when it can be inferred.",
    )
    investigation_start_date: str
    investigation_end_date: str
    comparison_start_date: str | None = None
    comparison_end_date: str | None = None
    objective: str


class InvestigationState(TypedDict, total=False):
    question: str
    site_id: int
    site_name: str
    conversation_context: str
    plan: dict[str, Any]
    primary_evidence: list[dict[str, Any]]
    supporting_evidence: list[dict[str, Any]]
    final_answer: str


class ToolSpec(TypedDict):
    label: str
    tool: str
    arguments: dict[str, Any]


ProgressCallback = Callable[[str, int, dict[str, Any] | None], None]
CancellationCheck = Callable[[], bool]


def build_investigation_graph(
    model: ChatOpenAI,
    tools: list[BaseTool],
    progress: ProgressCallback,
    cancelled: CancellationCheck,
):
    """Build an adaptive graph with two model calls and concurrent tool retrieval."""
    tool_map = {tool.name: tool for tool in tools}

    def stage(name: str, percent: int, normalized: dict[str, Any] | None = None) -> None:
        if cancelled():
            raise InvestigationCancelled("Investigation was cancelled.")
        progress(name, percent, normalized)

    def understand(state: InvestigationState) -> dict[str, Any]:
        stage("understanding_request", 10)
        planner = model.with_structured_output(InvestigationPlan)
        response = planner.invoke(
            "Normalize this oil-field investigation into a tool-execution plan. "
            f"Today is {date.today().isoformat()}. Resolve relative dates exactly. "
            "For a change or deviation without an explicit baseline, use the "
            "immediately preceding equal-length period. Map oil/water/gas production, "
            "water injection/transfer, flared/vented/fuel gas, oil sales, tank stock, "
            "equipment readings, well status, and operational history to the closest "
            "metric enum. Extract Battery N as scope_type=battery and a well reference "
            "as scope_type=well. Do not invent a scope name.\n\n"
            f"Recent conversation:\n{state.get('conversation_context', '')}\n\n"
            f"Question: {state['question']}"
        )
        plan = response.model_dump() if isinstance(response, BaseModel) else dict(response)
        stage("understanding_request", 15, plan)
        return {"plan": plan}

    def retrieve_primary(state: InvestigationState) -> dict[str, Any]:
        stage("checking_primary_metric", 25)
        specs = _primary_tool_specs(state["plan"])
        evidence = _execute_specs(specs, tool_map, cancelled)
        stage("checking_primary_metric", 55)
        return {"primary_evidence": evidence}

    def retrieve_support(state: InvestigationState) -> dict[str, Any]:
        stage("checking_relevant_causes", 62)
        specs = _support_tool_specs(state["plan"], state["question"])
        evidence = _execute_specs(specs, tool_map, cancelled)
        stage("checking_relevant_causes", 88)
        return {"supporting_evidence": evidence}

    def synthesize(state: InvestigationState) -> dict[str, str]:
        stage("correlating_evidence", 95)
        response = model.invoke(
            [
                SystemMessage(
                    content=(
                        "You are completing an oil-field operational investigation. "
                        "Use only the supplied tool evidence. First verify whether the "
                        "reported change or condition exists. Lead directly with the "
                        "supported conclusion, quantify the change when possible, and "
                        "identify the most relevant causes. Distinguish verified causes "
                        "from correlations and data limitations. Do not expose tool names, "
                        "database IDs, internal stages, SQL, or a Sources section. Use "
                        "MM/DD/YYYY dates, $ for costs, and barrels for volumes unless the "
                        "user requested otherwise."
                    )
                ),
                HumanMessage(
                    content=(
                        f"Question: {state['question']}\n"
                        f"Site: {state.get('site_name', 'selected site')}\n"
                        f"Plan: {json.dumps(state.get('plan', {}), default=str)}\n"
                        "Primary evidence: "
                        f"{json.dumps(state.get('primary_evidence', []), default=str)}\n"
                        "Supporting evidence: "
                        f"{json.dumps(state.get('supporting_evidence', []), default=str)}"
                    )
                ),
            ]
        )
        return {"final_answer": _message_text(response.content).strip()}

    graph = StateGraph(InvestigationState)
    graph.add_node("understand", understand)
    graph.add_node("retrieve_primary", retrieve_primary)
    graph.add_node("retrieve_support", retrieve_support)
    graph.add_node("synthesize", synthesize)
    graph.add_edge(START, "understand")
    graph.add_edge("understand", "retrieve_primary")
    graph.add_edge("retrieve_primary", "retrieve_support")
    graph.add_edge("retrieve_support", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


def _primary_tool_specs(plan: dict[str, Any]) -> list[ToolSpec]:
    """Translate the typed plan into authoritative metric and allocation calls."""
    metric = str(plan["metric"])
    start = str(plan["investigation_start_date"])
    end = str(plan["investigation_end_date"])
    comparison_start = plan.get("comparison_start_date")
    comparison_end = plan.get("comparison_end_date")
    specs: list[ToolSpec] = []

    if metric in _REPORT_METRICS:
        report_name = _REPORT_METRICS[metric]
        if comparison_start and comparison_end:
            specs.append(
                _spec(
                    "Comparison-period report",
                    "run_report",
                    report_name=report_name,
                    start_date=str(comparison_start),
                    end_date=str(comparison_end),
                )
            )
        specs.append(
            _spec(
                "Investigation-period report",
                "run_report",
                report_name=report_name,
                start_date=start,
                end_date=end,
            )
        )

    allocation_type = _allocation_type(metric)
    if allocation_type:
        for label, period_start, period_end in _periods(plan):
            arguments: dict[str, Any] = {
                "allocation_type": allocation_type,
                "start_date": period_start,
                "end_date": period_end,
                "sort_by": _allocation_sort_key(metric),
                "sort_direction": "desc",
                "limit": 20,
                "well_filters": [],
            }
            _apply_well_scope(arguments, plan)
            specs.append(
                _spec(f"{label} well allocation", "list_well_allocation", **arguments)
            )

    if metric == "tank_inventory":
        for label, period_start, period_end in _periods(plan):
            arguments = {
                "start_date": period_start,
                "end_date": period_end,
                "contains": "any",
                "filters": [],
            }
            _apply_equipment_scope(arguments, plan, entity_field="tank_name")
            specs.append(_spec(f"{label} tank inventory", "search_tank_readings", **arguments))

    if metric == "equipment_reading" and plan.get("reading_type"):
        for label, period_start, period_end in _periods(plan):
            arguments = {
                "reading_type": plan["reading_type"],
                "start_date": period_start,
                "end_date": period_end,
                "equipment_filters": [],
                "reading_filters": [],
                "computed_filters": [],
            }
            _apply_equipment_scope(arguments, plan, entity_field="entity_name")
            specs.append(_spec(f"{label} equipment readings", "search_equipment_readings", **arguments))

    if metric == "well_status":
        specs.append(
            _spec(
                "Well shutdown records",
                "get_well_shutdowns",
                start_date=start,
                end_date=end,
                shutdown_type="all",
            )
        )
        if plan.get("scope_type") == "well" and plan.get("scope_name"):
            specs.append(
                _spec(
                    "Effective ONRR status",
                    "get_well_onrr_status_as_of_date",
                    well_name=plan["scope_name"],
                    as_of_date=end,
                )
            )
    return _deduplicate_specs(specs)


def _support_tool_specs(plan: dict[str, Any], question: str) -> list[ToolSpec]:
    """Select only evidence families that can explain the planned metric."""
    metric = str(plan["metric"])
    start, end = _combined_date_range(plan)
    specs: list[ToolSpec] = [
        _spec(
            "Operational notes, alarms, and work history",
            "search_operational_context",
            query=question,
            start_date=start,
            end_date=end,
            entity_name=_context_entity(plan),
            source_types=None,
            limit=12,
        )
    ]

    if metric in _WELL_IMPACT_METRICS:
        specs.append(
            _spec(
                "Shutdown and downtime evidence",
                "get_well_shutdowns",
                start_date=start,
                end_date=end,
                shutdown_type="all",
            )
        )
    elif metric == "tank_inventory":
        specs.append(
            _spec(
                "Missing reading evidence",
                "find_all_missing_readings_for_range",
                start_date=str(plan["investigation_start_date"]),
                end_date=str(plan["investigation_end_date"]),
            )
        )
    elif metric == "equipment_reading":
        specs.append(
            _spec(
                "Missing reading evidence",
                "find_all_missing_readings_for_range",
                start_date=str(plan["investigation_start_date"]),
                end_date=str(plan["investigation_end_date"]),
            )
        )
    elif metric in _FLARE_METRICS:
        arguments: dict[str, Any] = {
            "reading_type": "flare",
            "start_date": str(plan["investigation_start_date"]),
            "end_date": str(plan["investigation_end_date"]),
            "equipment_filters": [],
            "reading_filters": [],
            "computed_filters": [],
        }
        _apply_equipment_scope(arguments, plan, entity_field="entity_name")
        specs.append(_spec("Flare reading evidence", "search_equipment_readings", **arguments))
    return _deduplicate_specs(specs)


def _execute_specs(
    specs: list[ToolSpec],
    tool_map: dict[str, BaseTool],
    cancelled: CancellationCheck,
) -> list[dict[str, Any]]:
    """Execute independent domain tools concurrently without intermediate LLM calls."""
    if not specs:
        return [{"label": "Investigation", "ok": False, "error": "No applicable tool was available."}]
    results: list[dict[str, Any] | None] = [None] * len(specs)
    with ThreadPoolExecutor(max_workers=min(4, len(specs))) as executor:
        futures = {
            executor.submit(_invoke_spec, spec, tool_map): index
            for index, spec in enumerate(specs)
        }
        for future in as_completed(futures):
            if cancelled():
                for pending in futures:
                    pending.cancel()
                raise InvestigationCancelled("Investigation was cancelled.")
            index = futures[future]
            results[index] = future.result()
    return [result for result in results if result is not None]


def _invoke_spec(spec: ToolSpec, tool_map: dict[str, BaseTool]) -> dict[str, Any]:
    tool = tool_map.get(spec["tool"])
    if tool is None:
        return {"label": spec["label"], "ok": False, "error": "Evidence tool is unavailable."}
    try:
        raw = tool.invoke(spec["arguments"])
        payload = _parse_tool_result(raw)
        return {"label": spec["label"], "result": payload}
    except Exception as exc:
        return {"label": spec["label"], "ok": False, "error": str(exc)}


def _parse_tool_result(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw[:16000]
    return raw


def _periods(plan: dict[str, Any]) -> list[tuple[str, str, str]]:
    periods = [
        (
            "Investigation period",
            str(plan["investigation_start_date"]),
            str(plan["investigation_end_date"]),
        )
    ]
    if plan.get("comparison_start_date") and plan.get("comparison_end_date"):
        periods.insert(
            0,
            (
                "Comparison period",
                str(plan["comparison_start_date"]),
                str(plan["comparison_end_date"]),
            ),
        )
    return periods


def _combined_date_range(plan: dict[str, Any]) -> tuple[str, str]:
    starts = [str(plan["investigation_start_date"])]
    ends = [str(plan["investigation_end_date"])]
    if plan.get("comparison_start_date"):
        starts.append(str(plan["comparison_start_date"]))
    if plan.get("comparison_end_date"):
        ends.append(str(plan["comparison_end_date"]))
    return min(starts), max(ends)


def _apply_well_scope(arguments: dict[str, Any], plan: dict[str, Any]) -> None:
    scope_type = plan.get("scope_type")
    scope_name = plan.get("scope_name")
    if scope_type == "battery" and scope_name:
        arguments["well_filters"] = [{"field": "battery", "value": scope_name}]
    elif scope_type == "well" and scope_name:
        arguments["well_name"] = scope_name


def _apply_equipment_scope(
    arguments: dict[str, Any],
    plan: dict[str, Any],
    entity_field: str,
) -> None:
    scope_type = plan.get("scope_type")
    scope_name = plan.get("scope_name")
    if scope_type == "battery" and scope_name:
        arguments["battery_name"] = scope_name
    elif scope_type in {"well", "equipment"} and scope_name:
        arguments[entity_field] = scope_name


def _context_entity(plan: dict[str, Any]) -> str | None:
    if plan.get("scope_type") in {"well", "equipment"}:
        return plan.get("scope_name")
    return None


def _allocation_type(metric: str) -> str | None:
    if metric == "water_injection" or metric == "injection_allocation":
        return "injection"
    if metric in _PRODUCTION_METRICS or metric == "production_allocation":
        return "production"
    return None


def _allocation_sort_key(metric: str) -> str:
    return {
        "water_production": "total_allocated_water",
        "gas_production": "total_allocated_gas",
        "water_injection": "total_allocated_injection",
        "injection_allocation": "total_allocated_injection",
    }.get(metric, "total_allocated_oil")


def _spec(label: str, tool: str, **arguments: Any) -> ToolSpec:
    return {"label": label, "tool": tool, "arguments": arguments}


def _deduplicate_specs(specs: list[ToolSpec]) -> list[ToolSpec]:
    seen: set[str] = set()
    unique: list[ToolSpec] = []
    for spec in specs:
        key = json.dumps([spec["tool"], spec["arguments"]], sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(spec)
    return unique


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


_REPORT_METRICS = {
    "oil_production": "oil_production",
    "water_production": "water_production",
    "gas_production": "gas_production",
    "water_injection": "water_injection",
    "water_transfer": "water_transfer",
    "gas_flared": "gas_flared",
    "gas_vented": "gas_vented",
    "gas_flared_vented": "gas_flared_vented",
    "gas_fuel": "gas_fuel",
    "oil_sale": "oil_sale",
}
_PRODUCTION_METRICS = {"oil_production", "water_production", "gas_production"}
_WELL_IMPACT_METRICS = _PRODUCTION_METRICS | {
    "water_injection",
    "production_allocation",
    "injection_allocation",
}
_FLARE_METRICS = {"gas_flared", "gas_vented", "gas_flared_vented"}

"""Specialized LangGraph flow for one rod-pump well's health discussion."""

from __future__ import annotations

import json
import re
from datetime import date
from time import perf_counter
from typing import Any, Callable, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph


ROD_PUMP_LANGUAGE = re.compile(
    r"\b(rod[-\s]*pump|dynograph|pump\s*fillage|fluid\s*pound|pump\s*off|"
    r"load\s*set(?:ting|point)|min\s*load|max\s*load|paraffin\s*risk|"
    r"downhole\s*(?:card|impact)|surface\s*card)\b",
    re.IGNORECASE,
)
REASSESS_LANGUAGE = re.compile(
    r"\b(reassess|re-assess|analy[sz]e\s+again|rerun|re-run|update\s+analysis|"
    r"new\s+(?:analysis|data)|last\s+\d+\s+(?:day|days|hour|hours))\b",
    re.IGNORECASE,
)
FOLLOW_UP_LANGUAGE = re.compile(
    r"\b(setting|setpoint|load|fillage|dynograph|card|pumping|pump|fluid|"
    r"paraffin|site|remote|reassess)\b|^(?:can|should)\s+i\b|^i\s+(?:can\s*not|can't)\s+go\s+to\s+site\b|"
    r"^what\s+about\s+(?:it|that)\b",
    re.IGNORECASE,
)
ROD_PUMP_ANALYSIS_INTENT = re.compile(
    r"\b(assess|analy[sz]e|diagnos|troubleshoot|health|condition|pumping|load|fillage|dynograph)\b",
    re.IGNORECASE,
)
WELL_CANDIDATE = re.compile(r"\b(?:well\s+)?([a-z]{2,5}[_-])?([0-9]{3,5}[a-z]?)\b", re.IGNORECASE)


class RodPumpGraphState(TypedDict, total=False):
    """State exchanged by the constrained single-well specialist graph."""

    question: str
    history: list[dict[str, str]]
    prior_context: dict[str, Any] | None
    analysis: dict[str, Any]
    answer: str
    traces: list[dict[str, Any]]
    stats: dict[str, Any]


def is_rod_pump_question(
    question: str,
    prior_context: dict[str, Any] | None = None,
    rod_pump_well_resolver: Callable[[str], bool] | None = None,
) -> bool:
    """Identify clear specialist requests without adding a routing-model call."""
    if ROD_PUMP_LANGUAGE.search(question):
        return True
    if rod_pump_well_resolver and ROD_PUMP_ANALYSIS_INTENT.search(question):
        candidate = _well_candidate(question)
        if candidate and rod_pump_well_resolver(candidate):
            return True
    return bool(prior_context and _is_follow_up(question))


def is_explicit_reassessment(question: str) -> bool:
    """Return whether the user explicitly requests fresh rod-pump evidence."""
    return bool(REASSESS_LANGUAGE.search(question))


def try_answer_rod_pump_question(
    *,
    model: Any,
    tools: list[BaseTool],
    question: str,
    history: list[dict[str, str]],
    site_name: str | None,
    today: date,
    prior_context: dict[str, Any] | None = None,
    rod_pump_well_resolver: Callable[[str], bool] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]] | None:
    """Run the rod-pump specialist only for a clear single-well conversation."""
    if not is_rod_pump_question(question, prior_context, rod_pump_well_resolver):
        return None
    tool = next((item for item in tools if item.name == "analyze_rod_pump"), None)
    if tool is None:
        return None

    graph = StateGraph(RodPumpGraphState)

    def obtain_analysis(state: RodPumpGraphState) -> dict[str, Any]:
        """Use cached evidence for follow-ups unless the user asks to reassess."""
        if state.get("prior_context") and not is_explicit_reassessment(state["question"]):
            return {"analysis": state["prior_context"], "traces": []}

        started_at = perf_counter()
        routed = model.bind_tools([tool], parallel_tool_calls=False).invoke(
            [
                SystemMessage(content=_analysis_request_prompt(site_name, today)),
                HumanMessage(content=state["question"]),
            ]
        )
        model_seconds = perf_counter() - started_at
        if not routed.tool_calls:
            return {
                "answer": _message_text(routed.content),
                "traces": [],
                "stats": _stats(model_seconds=model_seconds),
            }

        call = routed.tool_calls[0]
        arguments = call.get("args", {})
        tool_started_at = perf_counter()
        try:
            raw = tool.invoke(arguments)
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:
            payload = {"ok": False, "error": f"Rod-pump analysis failed: {exc}"}
        tool_seconds = perf_counter() - tool_started_at
        trace = {"tool": "analyze_rod_pump", "arguments": arguments, "result": payload}
        if not isinstance(payload, dict) or not payload.get("ok"):
            return {
                "answer": str(payload.get("error", "Rod-pump analysis could not be completed.")),
                "traces": [trace],
                "stats": _stats(model_seconds=model_seconds, tool_seconds=tool_seconds),
            }
        return {
            "analysis": _compact_analysis_context(payload),
            "traces": [trace],
            "stats": _stats(model_seconds=model_seconds, tool_seconds=tool_seconds),
        }

    def write_answer(state: RodPumpGraphState) -> dict[str, Any]:
        """Produce the specialist's final response without unrelated tools."""
        if state.get("answer"):
            return {}
        started_at = perf_counter()
        response = model.invoke(
            [
                SystemMessage(content=_response_prompt(site_name, today, bool(state.get("prior_context")))),
                HumanMessage(content=_conversation_text(state["history"], state["question"])),
                AIMessage(content="Authoritative rod-pump analysis:\n" + json.dumps(state["analysis"], default=str)),
            ]
        )
        stats = dict(state.get("stats", _stats()))
        elapsed = perf_counter() - started_at
        stats["model_seconds"] += elapsed
        stats["model_calls"] += 1
        stats["total_seconds"] += elapsed
        return {"answer": _message_text(response.content), "stats": stats}

    graph.add_node("obtain_analysis", obtain_analysis)
    graph.add_node("write_answer", write_answer)
    graph.add_edge(START, "obtain_analysis")
    graph.add_edge("obtain_analysis", "write_answer")
    graph.add_edge("write_answer", END)
    started_at = perf_counter()
    state = graph.compile().invoke(
        {"question": question, "history": history[-6:], "prior_context": prior_context}
    )
    stats = dict(state.get("stats", _stats()))
    stats["total_seconds"] = round(perf_counter() - started_at, 3)
    return state.get("answer", "Rod-pump analysis could not be completed."), state.get("traces", []), stats


def compact_rod_pump_context(value: dict[str, Any]) -> dict[str, Any] | None:
    """Extract persistable evidence from a successful rod-pump tool payload."""
    if not value.get("ok"):
        return None
    return _compact_analysis_context(value)


def _compact_analysis_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep only evidence needed for a safe follow-up, never raw card series."""
    return {
        "well": payload.get("well", {}),
        "interval": payload.get("interval", {}),
        "current_status": payload.get("current_status", {}),
        "trend_features": payload.get("trend_features", {}),
        "dynograph_features": payload.get("dynograph_features", {}),
        "chart_note_events": payload.get("chart_note_events", [])[-10:],
        "diagnoses": payload.get("diagnoses", []),
        "health": payload.get("health", {}),
        "anomaly": payload.get("anomaly", {}),
        "paraffin_prediction": payload.get("paraffin_prediction", {}),
        "warnings": payload.get("warnings", []),
    }


def _analysis_request_prompt(site_name: str | None, today: date) -> str:
    return (
        "You route a single-well rod-pump request for Ometrics. "
        f"Selected site: {site_name or 'selected site'}; today: {today.isoformat()}. "
        "Call analyze_rod_pump exactly once when the user provides one well identifier. "
        "Use an ISO interval only when the user explicitly requests one. "
        "If one exact rod-pump well is not identifiable, ask a concise clarification."
    )


def _response_prompt(site_name: str | None, today: date, is_follow_up: bool) -> str:
    opening = (
        "This is a follow-up. Do not repeat a Current status paragraph and do not claim a reassessment."
        if is_follow_up
        else "Start with one concise status paragraph: well name, diagnosis, severity, confidence, and evidence."
    )
    return (
        "You are Omai's rod-pump health specialist. Use only the authoritative analysis supplied below. "
        f"Selected site: {site_name or 'selected site'}; today: {today.isoformat()}. "
        f"{opening} "
        "Then provide practical, conservative troubleshooting actions. Do not invent, rename, or increase a diagnosis, "
        "severity, confidence, measurement, prediction, or action. Dynographs alone do not confirm paraffin or another mechanical condition. "
        "Do not recommend unsupported setpoint changes. State limitations or warnings when present."
    )


def _conversation_text(history: list[dict[str, str]], question: str) -> str:
    lines = [f"{item['role'].title()}: {item['content']}" for item in history if item.get("content")]
    lines.append(f"User: {question}")
    return "\n".join(lines)


def _is_follow_up(question: str) -> bool:
    """Avoid hijacking short unrelated questions in a shared conversation."""
    return len(question.split()) <= 22 and bool(FOLLOW_UP_LANGUAGE.search(question))


def _well_candidate(question: str) -> str | None:
    """Extract one likely field identifier without treating dates as well names."""
    matches = list(WELL_CANDIDATE.finditer(question))
    if len(matches) != 1:
        return None
    return matches[0].group(0).removeprefix("well ").strip()


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        ).strip()
    return str(content)


def _stats(model_seconds: float = 0.0, tool_seconds: float = 0.0) -> dict[str, Any]:
    return {
        "total_seconds": model_seconds + tool_seconds,
        "model_seconds": model_seconds,
        "tool_seconds": tool_seconds,
        "model_calls": 1 if model_seconds else 0,
        "tool_calls": ([{"tool": "analyze_rod_pump", "seconds": tool_seconds}] if tool_seconds else []),
    }

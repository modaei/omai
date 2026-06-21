from __future__ import annotations

import re
from datetime import date
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE


MAX_TOOL_ROUNDS = 6
REASONING_EFFORT_BY_RESPONSE_MODE = {
    "faster": "medium",
    "more_accurate": "high",
}


def build_model(
    api_key: str,
    model: str,
    base_url: str,
    reasoning_effort: str | None = None,
) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=api_key,
        model=model,
        base_url=base_url,
        temperature=0,
        timeout=60,
        max_retries=2,
        reasoning_effort=reasoning_effort,
    )


def reasoning_effort_for_response_mode(response_mode: str) -> str:
    return REASONING_EFFORT_BY_RESPONSE_MODE.get(response_mode, "medium")


def answer_chat_question(
    model: ChatOpenAI,
    tools: list[BaseTool],
    site_id: int,
    history: list[dict[str, str]],
    question: str,
    site_name: str | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    started_at = perf_counter()
    tool_map = {tool.name: tool for tool in tools}
    model_with_tools = model.bind_tools(tools, parallel_tool_calls=False)

    messages = [
        SystemMessage(
            content=(
                "You are a read-only oil-field reporting assistant. "
                f"The selected site is {site_name or 'the selected site'}. "
                f"The internal site_id is {site_id}; use it only for tool calls and never mention it in answers. "
                f"Today is {date.today().isoformat()}. "
                "If the user asks anything outside Ometrics, oil-field operations, "
                "reports, readings, alarms, shutdowns, work orders, notes, "
                "production, injection, or supported software workflows, do not "
                f"answer the question. Reply only: \"{OUT_OF_DOMAIN_RESPONSE}\" "
                "Use search_ometrics_capabilities for questions about Ometrics "
                "capabilities, workflows, how to do something in the product, or "
                "which feature or report the user should use. Questions phrased "
                "as 'how to know', 'how can I know', or 'which report' are workflow "
                "guidance questions even when they include a date range. "
                "Use report tools for factual questions about calculated production, "
                "sales, gas, water, battery, injection, and allocation reports. "
                "Use summarize_report_by_month for month-by-month or monthly "
                "battery comparisons; do not call run_report separately for each "
                "month. "
                "Only run report tools when the user clearly asks to run, show, "
                "calculate, get, compare, or return actual report values, or when "
                "the user accepts a previous offer to run a specific report for a "
                "specific date range. "
                "Use reading tools for raw daily readings such as LACT, tank, "
                "water plant, flow meter, well test, pump, treater, flare, or "
                "knock-out readings, including missing-reading questions. Use "
                "reading tools for tank volume questions. Linear tank readings may "
                "return volume, and mixed tank readings may return oil_volume, "
                "water_volume, and total_volume calculated from tank bbl/ft. Use "
                "those returned volume fields directly; do not say tank charts or "
                "dimensions are required when volume fields are present. Use "
                "find_all_missing_readings when the user asks which readings are "
                "missing for a date without naming a specific reading type. "
                "Use search_readings for reading questions that ask for a date "
                "range, all entities, a specific entity name, or numeric filters "
                "such as pressure greater than a threshold. "
                "Use search_well_tests for well-test questions that ask for a "
                "date range, all wells, or numeric filters such as oil greater "
                "than a threshold. "
                "Use shutdown tools for well shutdown, downtime, shut-in, current "
                "long shutdown, downtime-code, and shutdown-cause summary questions. "
                "Use summarize_shutdown_causes when the user asks for the main, "
                "top, most common, or biggest cause/reason for shutdowns or downtime. "
                "For operational data questions, first prefer the most specific "
                "domain tool: report, reading, shutdown, timeline, work-order, or "
                "capability tools. If no specific domain tool fits or a direct "
                "database query is the clearer way to answer, use "
                "execute_operational_sql as the second priority. Use "
                "draft_operational_sql only when you need schema or validation "
                "feedback before execution. SQL queries must be SELECT-only, scoped "
                "with the `:site_id` bind parameter, and limited with a numeric LIMIT. "
                "If an operational SQL tool returns available_columns after a "
                "validation error, use those columns to repair the query directly; "
                "do not run SELECT * only for schema discovery. "
                "If the user gives a bare numeric well reference such as '5248' "
                "with shutdown/status language, treat it as a possible well name "
                "or well-name suffix for the selected site. "
                "Use summarize_work_order_costs for work order questions asking "
                "how much, total cost, total cost estimate, sum, amount, or other "
                "numeric cost statistics. Use metric='cost' for final_cost "
                "questions and metric='estimate' for cost_estimate questions. If "
                "final_cost has no recorded values but cost_estimate does, say "
                "the final cost is not recorded and give the cost-estimate total. Do not "
                "use search_operational_context to calculate work order totals "
                "when the structured work order cost tool can answer the question. "
                "Use search_operational_context for questions asking what happened, "
                "why something happened, summaries of operational notes/comments, "
                "work history, alarm context, shutdown explanations, or records "
                "mentioning a condition. For these answers, summarize only returned "
                "records. Start directly with a short interpretation in plain language "
                "with no heading. Do not include a 'Sources' section or source list "
                "unless the user explicitly asks for sources or detailed records. "
                "Do not add replacement record-list sections such as 'Summary of "
                "returned records'. Do not list each returned record by date unless "
                "the user explicitly asks for detailed records. Do not add default "
                "breakdowns such as counts by level, counts by type, dates with "
                "entries, or record totals unless the user asks for counts, dates, "
                "statistics, or detailed records. "
                "Do not use headings "
                "named 'Interpretation', 'Facts', or 'Inference'. "
                "Use the well timeline tool when the user asks for a timeline, "
                "sequence of events, or investigation for a specific well over a date range. "
                "For workflow guidance, explain the relevant Ometrics feature and "
                "do not query operational data unless the user asks for actual values. "
                "Keep workflow guidance limited to facts present in retrieved capability "
                "documents. Do not invent UI steps, menus, field names, filters, exports, "
                "pre-checklists, or options that are not in the capability tool result. "
                "For questions asking which report or feature to use, answer with only "
                "the report or feature name and a short purpose statement unless the "
                "user asks for details. If the matching report and date range are clear, "
                "ask whether the user wants you to run that report for that date range. "
                "For questions asking how to register, enter, or create a reading, "
                "answer with only the matching create action, such as 'Create a new "
                "LACT reading.', unless the user asks for details. "
                "Translate relative dates such as 'this month' into exact ISO dates. "
                "Use ISO YYYY-MM-DD dates only for tool arguments. In final answers "
                "shown to users, format dates as MM/DD/YYYY. "
                "Do not invent values or claim a report was run when no tool succeeded. "
                "Explain results clearly and include the exact date range. Do not mention "
                "units, missing unit labels, or unspecified units unless the user asks about units. "
                "Mention missing data when it affects the result. "
                "Do not mention report dimensions, breakdowns, filters, or labels such as "
                "'Battery = No Battery' unless they are explicitly present in the successful "
                "tool result and relevant to the user's question. "
                "When referring to field entities, use the entity display name or the "
                "equipment type plus name, such as 'Tank - 2-1 Float Over'. Do not refer "
                "to entities by database ID, and do not call them assets. "
                "Only say you can perform actions that are backed by the available tools. "
                "You do not have tools to send emails, create or export files, create records, "
                "update records, delete records, schedule tasks, acknowledge alarms, or control "
                "equipment. Do not offer to perform those actions. If the user asks for an "
                "unsupported action, say this version is read-only and explain what data you "
                "can retrieve instead. Do not add generic follow-up offers at the end of an "
                "answer."
            )
        )
    ]

    for item in history[-10:]:
        if item["role"] == "user":
            messages.append(HumanMessage(content=item["content"]))
        elif item["role"] == "assistant":
            messages.append(AIMessage(content=item["content"]))

    messages.append(HumanMessage(content=question))
    traces: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "total_seconds": 0.0,
        "model_seconds": 0.0,
        "tool_seconds": 0.0,
        "model_calls": 0,
        "tool_calls": [],
    }

    for _ in range(MAX_TOOL_ROUNDS):
        model_started_at = perf_counter()
        response = model_with_tools.invoke(messages)
        stats["model_seconds"] += perf_counter() - model_started_at
        stats["model_calls"] += 1
        messages.append(response)

        if not response.tool_calls:
            stats["total_seconds"] = perf_counter() - started_at
            return _clean_answer(_message_text(response.content)), traces, _rounded_stats(stats)

        for call in response.tool_calls:
            tool_name = call["name"]
            arguments = call.get("args", {})
            trace = {"tool": tool_name, "arguments": arguments}

            tool_started_at = perf_counter()
            tool = tool_map.get(tool_name)
            if tool is None:
                result = f"Unknown tool: {tool_name}"
            else:
                try:
                    result = tool.invoke(arguments)
                except Exception as exc:  # LangChain schema errors are user-facing here.
                    result = f"Tool validation failed: {exc}"
            tool_seconds = perf_counter() - tool_started_at
            stats["tool_seconds"] += tool_seconds
            stats["tool_calls"].append(
                {
                    "tool": tool_name,
                    "seconds": tool_seconds,
                }
            )

            trace["result"] = str(result)
            traces.append(trace)
            messages.append(
                ToolMessage(content=str(result), tool_call_id=call["id"])
            )

    stats["total_seconds"] = perf_counter() - started_at
    return (
        "I could not complete the request within the tool-call limit.",
        traces,
        _rounded_stats(stats),
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return str(content)


def _clean_answer(answer: str) -> str:
    answer = re.sub(
        r"(?im)^\s*the report did not specify units\.?\s*$\n?",
        "",
        answer,
    )
    answer = re.sub(
        r"(?i)(?:\n\s*)?the report did not specify units\.?\s*$",
        "",
        answer,
    )
    return answer.strip()


def _rounded_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_seconds": round(stats["total_seconds"], 3),
        "model_seconds": round(stats["model_seconds"], 3),
        "tool_seconds": round(stats["tool_seconds"], 3),
        "model_calls": stats["model_calls"],
        "tool_calls": [
            {
                "tool": tool_call["tool"],
                "seconds": round(tool_call["seconds"], 3),
            }
            for tool_call in stats["tool_calls"]
        ],
    }

from __future__ import annotations

import json
import re
from datetime import date
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from omai.agents.producing_wells_graph import (
    is_producing_well_test_coverage_request,
)
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE


MAX_TOOL_ROUNDS = 6
REASONING_EFFORT_BY_RESPONSE_MODE = {
    "fast": "medium",
    "intelligent": "high",
}


def build_model(
    api_key: str,
    model: str,
    base_url: str,
    reasoning_effort: str | None = None,
) -> ChatOpenAI:
    """Create the deterministic chat model used by the Omai agent.

    The caller owns provider-specific configuration and passes an OpenAI-compatible
    base URL. `reasoning_effort` is optional because not every OpenRouter model
    supports the same reasoning controls.
    """
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
    """Translate the public response-mode value into a model reasoning setting."""
    return REASONING_EFFORT_BY_RESPONSE_MODE.get(response_mode, "medium")


def answer_chat_question(
    model: ChatOpenAI,
    tools: list[BaseTool],
    site_id: int,
    history: list[dict[str, str]],
    question: str,
    site_name: str | None = None,
    authoritative_context: str | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Answer one user question with tool calling and bounded agent control flow.

    The function builds the complete system prompt, appends recent conversation
    history, lets the model call tools for at most `MAX_TOOL_ROUNDS`, and returns
    the final answer plus local traces and timing statistics. It also enforces a
    few agent-level invariants that are too important to leave only to prompting:
    SQL drafts are de-duplicated, valid SQL drafts are executed once
    automatically, and successful SQL execution forces a final natural-language
    answer instead of allowing more tool calls.
    """
    started_at = perf_counter()
    tool_map = {tool.name: tool for tool in tools}
    model_with_tools = model.bind_tools(tools, parallel_tool_calls=False)

    messages = [
        SystemMessage(
            content=(
                "You are an oil-field reporting and data-entry navigation assistant. "
                f"The selected site is {site_name or 'the selected site'}. "
                f"The internal site_id is {site_id}; use it only for tool calls and never mention it in answers. "
                f"Today is {date.today().isoformat()}. "
                "If the user asks anything outside Ometrics, oil-field operations, "
                "reports, readings, alarms, shutdowns, work orders, notes, "
                "production, injection, or supported software workflows, do not "
                f"answer the question. Reply only: \"{OUT_OF_DOMAIN_RESPONSE}\" "
                "Use search_ometrics_capabilities only for explicit Ometrics "
                "software-help questions where the user asks how to use the "
                "product, where to enter or find something, what a feature/report "
                "is for, or which feature/report they should use. Examples include "
                "'how do I register a down well', 'where can I enter a LACT "
                "reading', 'which report should I use for allocation', and 'what "
                "is the Production Allocation report for'. Do not use capability "
                "guidance for factual data requests that ask to show, compare, "
                "calculate, get, list, rank, return, summarize, or find actual "
                "operational values, top/bottom results, totals, averages, records, "
                "or date-range results. "
                "Use report tools for factual questions about calculated production, "
                "sales, gas, water, battery, injection, and allocation reports. "
                "Use summarize_report_by_month for month-by-month or monthly "
                "battery comparisons; do not call run_report separately for each "
                "month. Use summarize_well_allocation for questions asking how "
                "much oil, water, gas, or injection a specific well or well group "
                "contributed, produced, or injected. Use list_well_allocation for "
                "questions asking for per-well allocation rows, top/bottom/ranked "
                "wells, most/least producing wells, or daily averages per well. "
                "Do not use well tests for "
                "specific well or well-group production/injection contribution; "
                "well tests are samples, while allocation reports attribute measured "
                "production/injection. Well-group filters are case-insensitive and "
                "may use well attributes such as pump_type, onrr_code, battery, "
                "lact, direction, WOGCC class/status, prod_fm, monitored, or "
                "disable_reading. TA wells mean onrr_code = 'TA'. Rod wells mean "
                "wells.pump_type = 'ROD'; ESP "
                "wells mean 'ESP'; jet wells mean 'JET'; flowing/no-lift wells mean "
                "'flowing well no lift'. "
                "Only run report tools when the user clearly asks to run, show, "
                "calculate, get, compare, or return actual report values, or when "
                "the user accepts a previous offer to run a specific report for a "
                "specific date range. "
                "Use reading tools for raw daily readings such as LACT, tank, "
                "water plant, flow meter, well test, pump, treater, flare, or "
                "knock-out readings, including missing-reading questions. Use "
                "get_data_point_values for current telemetry value questions when "
                "the user provides a facility, optional device, and data-point name. "
                "When the user omits device_name, omit it from the answer because "
                "the tool defaults it from the resolved facility. Report every value "
                "when normalized data-point matching returns multiple closest matches, "
                "and include each returned received time. "
                "list_equipment for inventory questions that ask what/list/which "
                "equipment belongs to a battery or matches metadata without asking "
                "for reading values; do not require or invent a date for those "
                "questions. "
                "Use "
                "search_equipment_readings when a reading question includes "
                "battery relation, equipment metadata, arbitrary reading-field "
                "conditions, equipment type conditions, or tank computed-volume "
                "conditions. Do not infer equipment battery relation from names; "
                "use the actual relation returned by the tool. Pumps relate to "
                "batteries through their water plant. Use "
                "search_tank_readings for tank volume, tank stock, tank content, "
                "tank-in-battery, tanks containing oil/water, bottom-feet volume, "
                "or tank computed-volume filter questions when the generalized "
                "equipment search is not needed. Never classify tank contents "
                "from tank names alone. Mixed tank readings return "
                "oil_volume, water_volume, and total_volume calculated from tank "
                "bbl/ft. Use the returned persisted tank contents classification; "
                "never infer contents from tank type or name. Only tanks whose "
                "contents is oil or water-oil are oil-capable; never classify a "
                "water tank as containing oil. When a user asks for the volume of "
                "oil in tanks, report both oil_volume (gross oil) and "
                "recoverable_oil_volume, clearly labeled; when aggregating, report "
                "both totals. When a user asks which tanks contain oil without "
                "specifying a volume definition, do not call a tool yet: ask whether "
                "to use gross oil volume or recoverable oil volume. After the user "
                "chooses, require oil-capable contents and filter the selected field "
                "above zero: oil_volume > 0 for gross or recoverable_oil_volume > 0 "
                "for recoverable. Use recoverable_oil_volume when users ask for "
                "recoverable, usable, or available oil. Use returned volume fields "
                "directly; do not say tank charts or "
                "dimensions are required when volume fields are present. Use "
                "get_reading_for_entity when the user asks for a reading by object "
                "name and the reading type is missing or uncertain. Do not guess "
                "between flow meter, flare, tank, LACT, pump, treater, water plant, "
                "knock-out, or well reading types from object name alone; let the "
                "tool resolve the entity and ask for clarification when needed. Use "
                "find_all_missing_readings when the user asks which readings are "
                "missing for one date without naming a specific reading type. "
                "Use find_all_missing_readings_for_range when the user asks about "
                "missing readings, missing data, or missing data entry over a "
                "week, month, last calendar week, or any multi-day date range; do "
                "not call find_all_missing_readings separately for each date. "
                "Use search_readings for reading questions that ask for a date "
                "range, all entities, a specific entity name, or numeric filters "
                "such as pressure greater than a threshold, only when no battery "
                "or equipment metadata condition is involved. "
                "Use search_well_tests for well-test questions that ask for a "
                "date range, all wells, or numeric filters such as oil greater "
                "than a threshold. Use analyze_well_tests, not operational SQL, "
                "when the user asks to analyze, aggregate, or compare well tests "
                "per well, per battery, or for the selected site. Use "
                "analysis_mode='latest_previous' for latest-versus-prior-test "
                "comparisons and analysis_mode='range_summary' for grouped counts, "
                "sums, averages, minima, and maxima over a date range. Use "
                "analysis_mode='recent_tests' with test_count for requests to compare "
                "the latest N tests per well. Use analysis_mode='range_sequence' for "
                "requests to compare all tests in a date range per well; each test is "
                "compared with that well's preceding test inside the selected range. "
                "Use ONRR tools for questions asking what an ONRR code means, "
                "whether a well is active/producing/injection by ONRR code, or "
                "for ONRR-only well counts. ONRR code status must be resolved as "
                "of the requested date using well history before falling back to "
                "the current well code. "
                "Use shutdown tools for well shutdown, downtime, shut-in, current "
                "long shutdown, downtime-code, and shutdown-cause summary questions. "
                "Use get_active_wells for questions asking how many wells are active, "
                "inactive, online, or available on a date; do not infer active wells "
                "from well-test activity. Use get_producing_wells for questions asking "
                "how many wells are producing on a date; producing wells must use the "
                "ONRR code as of that date and must exclude ONRR injection wells "
                "(injection_well=true). Include ONRR code descriptions when they help "
                "explain why a well is or is not producing. If get_active_wells or "
                "get_producing_wells returns "
                "partial_shutdown_wells or partial_shutdown_well_names, mention "
                "those well names in the answer and say they are excluded from the "
                "returned counts. If partial_shutdown_count is 0, do not mention "
                "partial shutdowns at all. "
                "Never recreate active-well or producing-well membership with "
                "operational SQL. Those classifications require historical ONRR "
                "state and shutdown rules that exist only in the dedicated tools. "
                "Use summarize_shutdown_causes when the user asks for the main, "
                "top, most common, or biggest cause/reason for shutdowns or downtime. "
                "For operational data questions, first prefer the most specific "
                "domain tool: report, reading, shutdown, timeline, or work-order "
                "tools. If no specific domain tool fits or a direct database query "
                "is the clearer way to answer, use get_operational_sql_guidance "
                "before drafting SQL, then use execute_operational_sql as the "
                "second priority. Use capability tools only as the lowest-priority "
                "path for explicit software-help questions, not for data retrieval. "
                "Use "
                "draft_operational_sql only when you need schema or validation "
                "feedback before execution. SQL queries must be SELECT-only, scoped "
                "with the `:site_id` bind parameter, and limited with a numeric LIMIT. "
                "When writing SQL, do not reuse computed tool-result fields as "
                "database columns. For example, tank reading tool fields such as "
                "oil_volume, water_volume, and total_volume are not SQL columns; "
                "mixed tank oil volume must be calculated from mixed_tank_readings "
                "level fields and tanks.bbl_foot. "
                "Operational SQL runs on MySQL/MariaDB, not PostgreSQL: use "
                "LOWER(column) LIKE '%text%' instead of ILIKE, use DATE_FORMAT "
                "or YEAR/MONTH for monthly grouping instead of DATE_TRUNC, do not "
                "use PostgreSQL casts like ::date, and do not use DATE 'YYYY-MM-DD' "
                "literals. "
                "If an operational SQL tool returns available_columns after a "
                "validation error, use those columns to repair the query directly; "
                "do not run SELECT * only for schema discovery. "
                "After draft_operational_sql returns ok=true and executed=false, "
                "call execute_operational_sql next with the validated SQL if that "
                "query can answer the user. Do not draft alternate versions of an "
                "already valid SQL query unless the previous validation feedback "
                "shows a specific schema problem. "
                "After execute_operational_sql returns ok=true and executed=true, "
                "answer from those rows immediately. If row_count is 0, say no "
                "matching records were found instead of calling more tools. "
                "For short follow-up commands such as 'show them', 'break it down', "
                "or 'focus only on alarms', use the prior conversation context to "
                "resolve what 'them' or 'it' means and continue the same task. Do not "
                "restart schema discovery for follow-ups unless the previous context "
                "is insufficient. "
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
                "Use search_operational_context for general work-order questions "
                "asking to summarize, list, show, describe, or find work orders, "
                "comments, or notes. "
                "When talking about costs, final_cost, cost_estimate, estimates, "
                "amounts, totals, or other money values, format them as US dollars "
                "with a `$` prefix unless the user explicitly asks for another "
                "currency. When talking about oil, water, tank, production, sales, "
                "or injected volumes, use barrels unless the user explicitly asks "
                "for another volume unit. "
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
                "When the timeline question names specific topics or conditions "
                "such as chemical treatment, hot water, paraffin, failures, or "
                "alarms, pass those terms in the timeline tool's context_query "
                "so indexed operational context can be included. "
                "For workflow guidance, explain the relevant Ometrics feature and "
                "do not query operational data unless the user asks for actual values. "
                "Keep workflow guidance limited to facts present in retrieved capability "
                "documents. Do not invent UI steps, menus, field names, filters, exports, "
                "pre-checklists, or options that are not in the capability tool result. "
                "For questions asking which report or feature to use, answer with only "
                "the report or feature name and a short purpose statement unless the "
                "user asks for details. If the matching report and date range are clear, "
                "ask whether the user wants you to run that report for that date range. "
                "Whenever the user asks to create, add, enter, or record operational data, "
                "call prepare_data_entry. Never claim the record was created: the tool only "
                "prepares navigation to a form. If it returns needs_clarification, ask the "
                "specific clarification and list its candidates. If it returns duplicate, "
                "warn the user and do not suggest opening the form. If it returns ready, "
                "briefly confirm that the prefilled form is being opened. "
                "For example, a LACT request should prepare 'Create a new LACT reading' "
                "through the tool rather than merely describing that action. "
                "When the user's trimmed request starts with 'show me' (case-insensitive), "
                "call prepare_data_view instead of answering with the data. For text after "
                "'for', pass the text unchanged as search_text; it is a normal index search "
                "string, not an entity or battery lookup. Report views always prepare "
                "navigation and default missing dates to the last seven inclusive days. "
                "For existing data, if the tool returns no_results, say no matching records "
                "were found. If it returns ready, briefly say the requested page is opening. "
                "Translate relative dates such as 'this month' into exact ISO dates. "
                "Use ISO YYYY-MM-DD dates only for tool arguments. In final answers "
                "shown to users, format dates as MM/DD/YYYY. "
                "Do not invent values or claim a report was run when no tool succeeded. "
                "Explain results clearly and include the exact date range. Do not mention "
                "missing unit labels or unspecified unit disclaimers unless the user asks about units. "
                "Mention missing data when it affects the result. "
                "Do not mention report dimensions, breakdowns, filters, or labels such as "
                "'Battery = No Battery' unless they are explicitly present in the successful "
                "tool result and relevant to the user's question. "
                "When referring to field entities, use the entity display name or the "
                "equipment type plus name, such as 'Tank - 2-1 Float Over'. Do not refer "
                "to entities by database ID, and do not call them assets. "
                "Only say you can perform actions that are backed by the available tools. "
                "You may prepare and open data-entry forms, but you never submit them. "
                "You may also prepare report and existing-data page navigation when a request "
                "starts with 'show me'. "
                "You do not have tools to send emails, create or export files, create records, "
                "update records, delete records, schedule tasks, acknowledge alarms, or control "
                "equipment. Do not offer to perform those actions. If the user asks for an "
                "unsupported action, say this version is read-only and explain what data you "
                "can retrieve instead. Do not add generic follow-up offers at the end of an "
                "answer."
            )
        )
    ]

    if authoritative_context:
        messages.append(
            SystemMessage(
                content=(
                    "Authoritative domain data was fetched before this agent run. "
                    "Use it as the required factual input for the user's question. "
                    "Do not recreate, broaden, or replace it with SQL, current fields, "
                    "or inference. The tools that produced this context have already "
                    "run and are unavailable for this agent turn.\n\n"
                    f"{authoritative_context}"
                )
            )
        )

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
    force_final_response = False
    seen_sql_drafts: set[str] = set()
    failed_sql_attempts = 0
    block_coverage_sql = is_producing_well_test_coverage_request(
        question,
        history,
    )

    for _ in range(MAX_TOOL_ROUNDS):
        model_started_at = perf_counter()
        response = model.invoke(messages) if force_final_response else model_with_tools.invoke(messages)
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
            elif block_coverage_sql and tool_name in {
                "draft_operational_sql",
                "execute_operational_sql",
            }:
                result = json.dumps(
                    {
                        "ok": False,
                        "executed": False,
                        "error": (
                            "Operational SQL cannot classify producing-well test "
                            "coverage. Use get_producing_wells and an unfiltered "
                            "search_well_tests result for the same date range."
                        ),
                    },
                    separators=(",", ":"),
                )
                force_final_response = True
            elif _is_repeated_sql_draft(tool_name, arguments, seen_sql_drafts):
                # Repeated SQL drafts were a common source of tool-limit failures.
                # Return a structured failure so the model can stop or use prior
                # results instead of consuming another tool round.
                result = json.dumps(
                    {
                        "ok": False,
                        "executed": False,
                        "validation": {
                            "valid": False,
                            "error": "Repeated SQL draft. Execute the previous valid draft or answer from prior tool results.",
                        },
                    },
                    separators=(",", ":"),
                )
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
            if _is_valid_operational_sql_draft(tool_name, result):
                # A valid draft is already safe to run. Execute it here so the
                # model cannot spend additional rounds drafting equivalent SQL.
                execution_result = _execute_valid_sql_draft(
                    result,
                    tool_map,
                    stats,
                    traces,
                )
                if execution_result is not None:
                    messages.append(
                        SystemMessage(
                            content=(
                                "The valid SQL draft was automatically executed. "
                                f"Use this execution result for the final answer: {execution_result}"
                            ),
                        )
                    )
                    if _is_successful_operational_sql_result(
                        "execute_operational_sql",
                        execution_result,
                    ):
                        messages.append(
                            SystemMessage(
                                content=(
                                    "The operational SQL execution succeeded. Produce the "
                                    "final answer now using the returned rows. Do not call "
                                    "additional tools. If row_count is 0, say that no "
                                    "matching records were found."
                                )
                            )
                        )
                        force_final_response = True
                else:
                    messages.append(
                        SystemMessage(
                            content=(
                                "The operational SQL draft is valid. If this SQL answers "
                                "the user's question, call execute_operational_sql next "
                                "with the same SQL. Do not draft another SQL variant "
                                "unless there is a specific validation error to repair."
                            )
                        )
                )
            if _is_failed_operational_sql_result(tool_name, result):
                failed_sql_attempts += 1
                if failed_sql_attempts >= 2:
                    messages.append(
                        SystemMessage(
                            content=(
                                "The SQL fallback failed twice. Do not call more SQL tools. "
                                "Answer with what is known from successful tools, or say the "
                                "database query could not be completed."
                            )
                        )
                    )
                    force_final_response = True
            if _is_successful_operational_sql_result(tool_name, result):
                messages.append(
                    SystemMessage(
                        content=(
                            "The operational SQL execution succeeded. Produce the "
                            "final answer now using the returned rows. Do not call "
                            "additional tools. If row_count is 0, say that no "
                            "matching records were found."
                        )
                    )
                )
                force_final_response = True

    if force_final_response:
        model_started_at = perf_counter()
        response = model.invoke(messages)
        stats["model_seconds"] += perf_counter() - model_started_at
        stats["model_calls"] += 1
        stats["total_seconds"] = perf_counter() - started_at
        return _clean_answer(_message_text(response.content)), traces, _rounded_stats(stats)

    stats["total_seconds"] = perf_counter() - started_at
    return (
        "I could not complete the request within the tool-call limit.",
        traces,
        _rounded_stats(stats),
    )


def _message_text(content: Any) -> str:
    """Extract plain text from LangChain/OpenAI message content variants."""
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


def _is_successful_operational_sql_result(tool_name: str, result: Any) -> bool:
    """Return whether a tool result is a successful executed SQL payload."""
    if tool_name != "execute_operational_sql" or not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return False
    return payload.get("ok") is True and payload.get("executed") is True


def _is_failed_operational_sql_result(tool_name: str, result: Any) -> bool:
    """Return whether a SQL draft or execution tool returned structured failure."""
    if tool_name not in {"draft_operational_sql", "execute_operational_sql"}:
        return False
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return False
    return payload.get("ok") is False


def _is_valid_operational_sql_draft(tool_name: str, result: Any) -> bool:
    """Return whether a draft SQL tool call validated but did not execute SQL."""
    if tool_name != "draft_operational_sql" or not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return False
    return payload.get("ok") is True and payload.get("executed") is False


def _is_repeated_sql_draft(
    tool_name: str,
    arguments: dict[str, Any],
    seen_sql_drafts: set[str],
) -> bool:
    """Track SQL drafts and reject exact repeats within one agent turn."""
    if tool_name != "draft_operational_sql":
        return False
    normalized = _normalize_sql_for_loop_guard(str(arguments.get("sql", "")))
    if not normalized:
        return False
    if normalized in seen_sql_drafts:
        return True
    seen_sql_drafts.add(normalized)
    return False


def _execute_valid_sql_draft(
    draft_result: str,
    tool_map: dict[str, BaseTool],
    stats: dict[str, Any],
    traces: list[dict[str, Any]],
) -> str | None:
    """Execute a validated SQL draft through the normal SQL execution tool.

    This keeps SQL execution in the existing tool boundary, so validation,
    database permissions, tracing, and timing behave the same as a model-requested
    `execute_operational_sql` call. `None` means the execution tool is unavailable
    or the draft payload is malformed.
    """
    execute_tool = tool_map.get("execute_operational_sql")
    if execute_tool is None:
        return None
    try:
        payload = json.loads(draft_result)
    except (TypeError, ValueError):
        return None
    arguments = {
        "question": payload.get("question") or "",
        "sql": payload.get("sql") or "",
        "notes": payload.get("notes"),
    }
    if not arguments["sql"]:
        return None

    started_at = perf_counter()
    try:
        result = execute_tool.invoke(arguments)
    except Exception as exc:  # Keep parity with normal tool invocation handling.
        result = f"Tool validation failed: {exc}"
    elapsed = perf_counter() - started_at
    stats["tool_seconds"] += elapsed
    stats["tool_calls"].append(
        {
            "tool": "execute_operational_sql",
            "seconds": elapsed,
        }
    )
    traces.append(
        {
            "tool": "execute_operational_sql",
            "arguments": arguments,
            "result": str(result),
            "auto_executed": True,
        }
    )
    return str(result)


def _normalize_sql_for_loop_guard(sql: str) -> str:
    """Normalize SQL enough to detect exact repeated drafts in one turn."""
    return " ".join(sql.strip().rstrip(";").split()).lower()


def _clean_answer(answer: str) -> str:
    """Apply final text cleanup for recurring model phrasing problems."""
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
    """Round timing statistics while preserving tool-call structure."""
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

"""Static skill definitions and deterministic selection for the chat agent.

Skills are trusted application instructions, not user-provided documents.  They
group domain-specific tool guidance so the general agent does not need every
rule in its initial system message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence


MAX_INITIAL_SKILLS = 2
MAX_ACTIVATED_SKILLS = 2


@dataclass(frozen=True)
class ChatSkill:
    """One versioned instruction pack and the tools it may expose."""

    skill_id: str
    version: str
    summary: str
    tool_names: frozenset[str]
    instructions: str
    trigger_patterns: tuple[str, ...]


CHAT_SKILLS: tuple[ChatSkill, ...] = (
    ChatSkill(
        skill_id="reports_allocation",
        version="v1",
        summary="Calculated production, sales, gas, water, injection, report comparisons, and well allocation.",
        tool_names=frozenset({
            "list_available_reports", "run_report", "compare_report_periods",
            "summarize_report_by_month", "summarize_well_allocation",
            "list_well_allocation",
        }),
        instructions="""<reports_allocation_skill version="v1">
Use report tools for factual calculated production, sales, gas, water, battery, injection, and allocation questions.
Use summarize_report_by_month for month-by-month comparisons; do not run one report per month.
For why/reason/cause questions about production changes, establish the change with report data and use operational context for supporting evidence. Do not claim a cause from report numbers alone.
Use summarize_well_allocation for one well or a well group contribution. Use list_well_allocation for per-well rows, rankings, top/bottom producers, or daily averages per well.
Do not use well tests for production or injection contribution. Well-group filters are case-insensitive and may use well attributes such as pump_type, onrr_code, battery, lact, direction, WOGCC class/status, prod_fm, monitored, or disable_reading. TA means onrr_code='TA'; rod/ESP/jet wells use pump_type ROD/ESP/JET.
Only run a report when the user asks for actual values or accepts a prior offer to run it.
</reports_allocation_skill>""",
        trigger_patterns=(
            r"\b(report|production|produced|produce|contribut(?:e|ed|ion)|production allocation|allocation|oil sale|gas flared|flared|injection|water production)\b",
            r"\b(top|most|least|rank(?:ed|ing)?)\b.*\b(producing|production)\b",
        ),
    ),
    ChatSkill(
        skill_id="readings_equipment",
        version="v1",
        summary="Daily readings, missing readings, equipment inventory, tank levels and computed tank volumes.",
        tool_names=frozenset({
            "list_reading_types", "get_readings_for_date", "get_reading_for_entity",
            "compare_readings_between_dates", "find_missing_readings",
            "find_all_missing_readings", "find_all_missing_readings_for_range",
            "list_equipment", "search_readings", "search_equipment_readings",
            "search_tank_readings",
        }),
        instructions="""<readings_equipment_skill version="v1">
Use reading tools for raw daily LACT, tank, water plant, flow meter, pump, treater, flare, knock-out, and well-test readings, including missing-reading questions.
Use list_equipment for inventory questions about equipment belonging to a battery or matching metadata without asking for readings. Use search_equipment_readings when a question includes battery relation, metadata, or arbitrary reading-field conditions. Never infer battery relation from names; use the relation returned by tools. Pumps relate to batteries through their water plant.
Use search_tank_readings for tank volume, stock, contents, tank-in-battery, oil/water containing tanks, bottom-feet volume, or computed-volume filtering. Use persisted contents and returned volume fields, never tank names or type alone. Only contents oil or water-oil are oil-capable. For oil volume, label gross oil_volume and recoverable_oil_volume separately. Ask which definition is intended before listing oil-containing tanks when the request does not specify one.
For uncertain object type, use get_reading_for_entity rather than guessing. Use find_all_missing_readings_for_range for multi-day missing-reading questions, not repeated daily calls.
</readings_equipment_skill>""",
        trigger_patterns=(
            r"\b(readings?|missing data|missing entr|tank|lact|treater|flow meter|flare|knock.?out|water plant|equipment|oil volume|water volume)\b",
        ),
    ),
    ChatSkill(
        skill_id="data_points",
        version="v1",
        summary="Current facility telemetry values and historical data-point trend analysis.",
        tool_names=frozenset({"get_data_point_values", "analyze_data_point_trend"}),
        instructions="""<data_points_skill version="v1">
Use get_data_point_values for current telemetry values when facility, optional device, and data-point name are provided. If device is omitted, do not mention it in the answer because the tool resolves it from the facility. Report every closest matching data point and its received time.
Use analyze_data_point_trend for historical data-point trend requests. Ground the answer in returned samples and describe overall trend, variability, and material spikes or drops with their dates and sizes.
</data_points_skill>""",
        trigger_patterns=(r"\b(data[ _-]?point|telemetry|trend|current load|stroke length|pump fillage|min load|max load)\b",),
    ),
    ChatSkill(
        skill_id="well_tests",
        version="v1",
        summary="Raw well-test retrieval and per-well, battery, or site well-test analysis.",
        tool_names=frozenset({"search_well_tests", "analyze_well_tests"}),
        instructions="""<well_tests_skill version="v1">
Use search_well_tests for test ranges and numeric filters. Use analyze_well_tests, not operational SQL, for analyzed or compared well tests per well, battery, or site.
Use latest_previous for latest-versus-prior tests, range_summary for grouped date-range statistics, recent_tests for the latest N tests, and range_sequence to compare each selected test with the well's preceding selected test.
</well_tests_skill>""",
        trigger_patterns=(r"\b(well tests?|test results?|test oil|test water)\b",),
    ),
    ChatSkill(
        skill_id="well_status_shutdowns",
        version="v1",
        summary="ONRR status, active or producing wells, shutdown records, downtime codes, and shutdown causes.",
        tool_names=frozenset({
            "list_onrr_codes", "explain_onrr_code", "get_well_onrr_status_as_of_date",
            "count_wells_by_onrr_status", "list_downtime_codes", "get_well_shutdowns",
            "get_current_shutdowns", "get_active_wells", "get_producing_wells",
            "summarize_shutdown_causes",
        }),
        instructions="""<well_status_shutdowns_skill version="v1">
Use ONRR tools for code meanings and ONRR status as of a requested date. Use shutdown tools for shutdown, downtime, shut-in, current shutdown, downtime-code, and shutdown-cause questions.
Use get_active_wells for active/inactive/online/available counts. Active means effective ONRR is_active=true only; shutdown state is not considered. Use get_producing_wells for producing counts: it resolves historical ONRR, excludes injection wells, and excludes full-day shutdowns.
For a broader question containing active status, combine the returned active population with the relevant tool result. Do not recreate active or producing membership with SQL. Use summarize_shutdown_causes for main/top/common shutdown causes. Mention partial-shutdown well names only when the dedicated producing-well result returns them.
</well_status_shutdowns_skill>""",
        trigger_patterns=(r"\b(shutdown|shut.?in|downtime|down well|active wells?|inactive wells?|producing wells?|onrr|downtime code)\b",),
    ),
    ChatSkill(
        skill_id="operational_context",
        version="v1",
        summary="Operational notes, alarms, work history, chart notes, timelines, and evidence-based explanations.",
        tool_names=frozenset({"search_operational_context", "get_well_timeline", "summarize_work_order_costs"}),
        instructions="""<operational_context_skill version="v1">
Prefer a specific report, reading, shutdown, timeline, or work-order tool first. Use search_operational_context for what happened, why something happened, summaries of notes/comments, work history, alarm context, shutdown explanations, and records mentioning a condition.
Summarize only returned records. Start directly with a short interpretation and do not add default Sources, record-list, count, date, or breakdown sections unless requested. Use get_well_timeline for a specific well timeline or investigation. Pass named topics such as paraffin, chemical treatment, hot water, failures, or alarms through context_query.
Use summarize_work_order_costs for numeric work-order cost totals. Use operational context for general work-order summaries, comments, and notes.
</operational_context_skill>""",
        trigger_patterns=(r"\b(what happened|why|reasons?|causes?|notes?|history|alarms?|chart notes?|work orders?|paraffin|chemical|hot water|timeline)\b",),
    ),
    ChatSkill(
        skill_id="rod_pump",
        version="v1",
        summary="Single-well rod-pump health, dynograph diagnosis, operating condition, and paraffin-risk analysis.",
        tool_names=frozenset({"analyze_rod_pump"}),
        instructions="""<rod_pump_skill version="v1">
Use analyze_rod_pump for one specific rod-pump well health, dynograph, operating-condition, diagnosis, or paraffin-risk request.
Fleet-wide rankings are not available in chat. Ask for one exact well name, telemetry key, or complete field identifier. Ground results in returned measurements and chart-note events. Distinguish current diagnosis from prediction and preserve qualified wording. Dynographs alone never confirm paraffin. Tool diagnoses, severities, confidence, and recommendations are authoritative.
</rod_pump_skill>""",
        trigger_patterns=(r"\b(rod pump|pump health|dynograph|fluid pound|pump.?off|pump fillage|paraffin risk)\b",),
    ),
    ChatSkill(
        skill_id="workflow_navigation",
        version="v1",
        summary="Ometrics feature help and opening prefilled data-entry or existing-data views.",
        tool_names=frozenset({"search_ometrics_capabilities", "prepare_data_entry", "prepare_data_view"}),
        instructions="""<workflow_navigation_skill version="v1">
Use software-help guidance only when the user explicitly asks how to use Ometrics, where to enter/find something, what a feature or report is for, or which feature/report to use. It is the lowest-priority path and not for factual data retrieval.
For create/add/enter/record requests call prepare_data_entry. It prepares navigation only; never claim a record was created. For a request beginning with 'show me', call prepare_data_view instead of answering with data. Keep guidance limited to retrieved capability facts and do not invent UI steps.
</workflow_navigation_skill>""",
        trigger_patterns=(r"\b(how do i|where can i|what is .* report for|which report|create|add|enter|record|show me)\b",),
    ),
    ChatSkill(
        skill_id="operational_sql",
        version="v1",
        summary="Read-only, site-scoped operational SQL fallback after domain tools are insufficient.",
        tool_names=frozenset({"get_operational_sql_guidance", "draft_operational_sql", "execute_operational_sql"}),
        instructions="""<operational_sql_skill version="v1">
Use SQL only when a specific domain tool does not fit or direct database retrieval is clearer. First call get_operational_sql_guidance.
Queries must be SELECT-only, include :site_id, and use numeric LIMIT. This is MySQL/MariaDB: use LOWER(column) LIKE, DATE_FORMAT or YEAR/MONTH, and no PostgreSQL syntax. Never use computed tool-result fields as database columns.
Draft only when schema feedback is needed. Execute a valid draft next, repair from returned available_columns, do not use SELECT *, and answer immediately after successful execution. Do not recreate active/producing membership with SQL.
</operational_sql_skill>""",
        trigger_patterns=(r"\b(sql|database|table|cross.?table|aggregate|group by)\b",),
    ),
)

SKILLS_BY_ID = {skill.skill_id: skill for skill in CHAT_SKILLS}


def skill_catalog() -> str:
    """Return concise trusted metadata describing every skill to the model."""
    return "\n".join(
        f"- {skill.skill_id} ({skill.version}): {skill.summary}"
        for skill in CHAT_SKILLS
    )


def select_initial_skills(question: str, history: Sequence[dict[str, str]]) -> tuple[ChatSkill, ...]:
    """Select up to two high-confidence skills without another model call.

    Short referential follow-ups inherit the last user request's domain so a
    follow-up such as ``focus only on alarms`` retains operational context.
    """
    current = _normalize(question)
    candidates = _score_skills(current)
    if not candidates and _is_referential_follow_up(current):
        prior_questions = [
            _normalize(item.get("content", ""))
            for item in reversed(history)
            if item.get("role") == "user"
        ]
        for prior in prior_questions[:3]:
            candidates = _score_skills(prior)
            if candidates:
                break
    return tuple(skill for _, skill in candidates[:MAX_INITIAL_SKILLS])


def resolve_activated_skills(skill_ids: Iterable[str], active_skill_ids: Iterable[str]) -> tuple[ChatSkill, ...]:
    """Validate and cap model-requested skill activation deterministically."""
    active = set(active_skill_ids)
    resolved: list[ChatSkill] = []
    for skill_id in skill_ids:
        skill = SKILLS_BY_ID.get(str(skill_id))
        if skill is None or skill.skill_id in active or skill in resolved:
            continue
        resolved.append(skill)
        if len(resolved) == MAX_ACTIVATED_SKILLS:
            break
    return tuple(resolved)


def tool_names_for_skills(skills: Iterable[ChatSkill]) -> frozenset[str]:
    """Return the union of server-approved tool names for active skills."""
    return frozenset().union(*(skill.tool_names for skill in skills))


def _score_skills(text: str) -> list[tuple[int, ChatSkill]]:
    scored: list[tuple[int, ChatSkill]] = []
    for index, skill in enumerate(CHAT_SKILLS):
        score = sum(bool(re.search(pattern, text, flags=re.IGNORECASE)) for pattern in skill.trigger_patterns)
        if score:
            # Registry order makes ties deterministic and keeps multi-domain
            # requests predictable without an LLM classifier.
            scored.append((score * 100 - index, skill))
    return sorted(scored, key=lambda item: item[0], reverse=True)


def _is_referential_follow_up(text: str) -> bool:
    return len(text.split()) <= 12 and bool(re.search(
        r"\b(it|them|those|that|these|focus|break down|what about|and)\b", text,
    ))


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("_", " ").replace("-", " ").split())

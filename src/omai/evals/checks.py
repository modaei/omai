from __future__ import annotations

import re
from typing import Any

from omai.evals.models import DeterministicCheckResult, EvaluationCase
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE


DATE_ISO_PATTERN = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
SITE_ID_PATTERN = re.compile(r"\bsite[_ ]?id\b|\bsite\s+\d+\b", re.IGNORECASE)
DATABASE_ID_PATTERN = re.compile(r"\b(?:asset_id|entity_id|well_id|tank_id)\b", re.IGNORECASE)


def run_deterministic_checks(
    case: EvaluationCase,
    answer: str,
    tool_calls: list[dict[str, Any]],
) -> list[DeterministicCheckResult]:
    """Run hard checks that should not depend on another LLM judge."""

    checks = []
    tool_names = [str(call.get("tool")) for call in tool_calls if call.get("tool")]
    checks.extend(_required_tool_checks(case, tool_names))
    checks.extend(_forbidden_tool_checks(case, tool_names))
    checks.extend(_phrase_checks(case, answer))
    checks.extend(_global_answer_checks(case, answer))
    return checks


def _required_tool_checks(
    case: EvaluationCase, tool_names: list[str]
) -> list[DeterministicCheckResult]:
    return [
        DeterministicCheckResult(
            name=f"required_tool:{tool_name}",
            passed=tool_name in tool_names,
            message=(
                f"Required tool {tool_name!r} was used."
                if tool_name in tool_names
                else f"Required tool {tool_name!r} was not used. Used: {tool_names}"
            ),
        )
        for tool_name in case.required_tools
    ]


def _forbidden_tool_checks(
    case: EvaluationCase, tool_names: list[str]
) -> list[DeterministicCheckResult]:
    return [
        DeterministicCheckResult(
            name=f"forbidden_tool:{tool_name}",
            passed=tool_name not in tool_names,
            message=(
                f"Forbidden tool {tool_name!r} was not used."
                if tool_name not in tool_names
                else f"Forbidden tool {tool_name!r} was used."
            ),
        )
        for tool_name in case.forbidden_tools
    ]


def _phrase_checks(case: EvaluationCase, answer: str) -> list[DeterministicCheckResult]:
    normalized = answer.casefold()
    checks = []
    for phrase in case.required_phrases:
        found = phrase.casefold() in normalized
        checks.append(
            DeterministicCheckResult(
                name=f"required_phrase:{phrase}",
                passed=found,
                message=(
                    f"Required phrase {phrase!r} was present."
                    if found
                    else f"Required phrase {phrase!r} was missing."
                ),
            )
        )
    for phrase in case.forbidden_phrases:
        found = phrase.casefold() in normalized
        checks.append(
            DeterministicCheckResult(
                name=f"forbidden_phrase:{phrase}",
                passed=not found,
                message=(
                    f"Forbidden phrase {phrase!r} was absent."
                    if not found
                    else f"Forbidden phrase {phrase!r} was present."
                ),
            )
        )
    return checks


def _global_answer_checks(
    case: EvaluationCase, answer: str
) -> list[DeterministicCheckResult]:
    checks = [
        DeterministicCheckResult(
            name="no_site_id_leak",
            passed=SITE_ID_PATTERN.search(answer) is None,
            message="Answer does not expose site_id." if SITE_ID_PATTERN.search(answer) is None else "Answer exposes site_id/site number.",
        ),
        DeterministicCheckResult(
            name="no_database_id_labels",
            passed=DATABASE_ID_PATTERN.search(answer) is None,
            message="Answer does not expose database-only ID labels." if DATABASE_ID_PATTERN.search(answer) is None else "Answer exposes database-only ID labels.",
        ),
    ]
    if "guardrail" in case.category:
        checks.append(
            DeterministicCheckResult(
                name="out_of_domain_refusal",
                passed=answer.strip() == OUT_OF_DOMAIN_RESPONSE,
                message=(
                    "Out-of-domain refusal matched."
                    if answer.strip() == OUT_OF_DOMAIN_RESPONSE
                    else "Out-of-domain refusal did not match."
                ),
            )
        )
    if "data_entry" in case.category or "workflow" in case.category:
        lower = answer.casefold()
        record_created = any(
            phrase in lower
            for phrase in (
                "record has been created",
                "i created",
                "created the reading",
                "saved the reading",
                "submitted the reading",
            )
        )
        checks.append(
            DeterministicCheckResult(
                name="no_create_claim",
                passed=not record_created,
                message=(
                    "Answer does not claim a record was created."
                    if not record_created
                    else "Answer claims a record was created."
                ),
            )
        )
    if DATE_ISO_PATTERN.search(answer):
        checks.append(
            DeterministicCheckResult(
                name="no_iso_dates",
                passed=False,
                message="Answer contains ISO date formatting instead of MM/DD/YYYY.",
            )
        )
    return checks

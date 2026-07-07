from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omai.evals.models import EvaluationCase


DEFAULT_CASE_PATH = Path(__file__).resolve().parent / "cases" / "core.json"


class EvaluationCaseError(ValueError):
    """Raised when evaluation case files are missing or invalid."""


def load_cases(path: str | Path | None = None) -> list[EvaluationCase]:
    """Load evaluation cases from JSON for the local live evaluator."""

    case_path = Path(path) if path else DEFAULT_CASE_PATH
    if not case_path.exists():
        raise EvaluationCaseError(f"Evaluation case file not found: {case_path}")
    try:
        raw_cases = json.loads(case_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvaluationCaseError(f"Invalid evaluation case JSON: {exc}") from exc
    if not isinstance(raw_cases, list):
        raise EvaluationCaseError("Evaluation case file must contain a JSON list.")
    return [_case_from_dict(item, index) for index, item in enumerate(raw_cases)]


def filter_cases(
    cases: list[EvaluationCase],
    category: str | None = None,
    case_id: str | None = None,
    limit: int | None = None,
) -> list[EvaluationCase]:
    """Apply CLI filters without changing case ordering."""

    filtered = cases
    if category:
        filtered = [case for case in filtered if case.category == category]
    if case_id:
        filtered = [case for case in filtered if case.id == case_id]
    if limit is not None:
        filtered = filtered[:limit]
    return filtered


def _case_from_dict(raw: Any, index: int) -> EvaluationCase:
    if not isinstance(raw, dict):
        raise EvaluationCaseError(f"Case #{index + 1} must be an object.")
    required = {"id", "category", "question", "site_id"}
    missing = sorted(required - set(raw))
    if missing:
        raise EvaluationCaseError(
            f"Case #{index + 1} is missing required fields: {', '.join(missing)}"
        )
    try:
        site_id = int(raw["site_id"])
    except (TypeError, ValueError) as exc:
        raise EvaluationCaseError(
            f"Case {raw.get('id', index + 1)!r} has invalid site_id."
        ) from exc
    if site_id <= 0:
        raise EvaluationCaseError(
            f"Case {raw.get('id', index + 1)!r} site_id must be positive."
        )
    response_mode = str(raw.get("response_mode", "fast"))
    if response_mode not in {"fast", "intelligent"}:
        raise EvaluationCaseError(
            f"Case {raw['id']!r} response_mode must be fast or intelligent."
        )
    thresholds = raw.get("metric_thresholds", {})
    if thresholds is None:
        thresholds = {}
    if not isinstance(thresholds, dict):
        raise EvaluationCaseError(
            f"Case {raw['id']!r} metric_thresholds must be an object."
        )
    return EvaluationCase(
        id=str(raw["id"]),
        category=str(raw["category"]),
        question=str(raw["question"]),
        site_id=site_id,
        current_date=(
            str(raw["current_date"]) if raw.get("current_date") is not None else None
        ),
        response_mode=response_mode,
        required_tools=_tuple_of_strings(raw.get("required_tools", ()), raw["id"]),
        forbidden_tools=_tuple_of_strings(raw.get("forbidden_tools", ()), raw["id"]),
        required_phrases=_tuple_of_strings(raw.get("required_phrases", ()), raw["id"]),
        forbidden_phrases=_tuple_of_strings(raw.get("forbidden_phrases", ()), raw["id"]),
        metrics=_tuple_of_strings(raw.get("metrics", ("answer_relevancy",)), raw["id"]),
        metric_thresholds={str(key): float(value) for key, value in thresholds.items()},
        metadata=raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), dict) else {},
    )


def _tuple_of_strings(value: Any, case_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise EvaluationCaseError(f"Case {case_id!r} list field is invalid.")
    return tuple(str(item) for item in value)

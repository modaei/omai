from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI


logger = logging.getLogger(__name__)


@dataclass
class LocalLlmTrace:
    """Timing and fallback information for one local helper model call."""

    task: str
    seconds: float
    ok: bool
    fallback_reason: str | None = None


@dataclass
class LocalLlmUsage:
    """Collect local model timings so they can be stored in ai_messages.info."""

    calls: list[LocalLlmTrace] = field(default_factory=list)

    def add(self, task: str, seconds: float, ok: bool, fallback_reason: str | None = None) -> None:
        self.calls.append(
            LocalLlmTrace(
                task=task,
                seconds=round(seconds, 3),
                ok=ok,
                fallback_reason=fallback_reason,
            )
        )

    def info(self) -> dict[str, Any]:
        return {
            "local_llm_seconds": round(sum(item.seconds for item in self.calls), 3),
            "local_llm_calls": len(self.calls),
            "local_llm_tasks": [
                {
                    "task": item.task,
                    "seconds": item.seconds,
                    "ok": item.ok,
                    **(
                        {"fallback_reason": item.fallback_reason}
                        if item.fallback_reason
                        else {}
                    ),
                }
                for item in self.calls
            ],
        }

    def mark_last_failed(self, task: str, fallback_reason: str) -> None:
        """Mark the most recent completed call as unusable after validation."""
        for item in reversed(self.calls):
            if item.task == task:
                item.ok = False
                item.fallback_reason = fallback_reason
                return


class LocalLlmService:
    """Bounded helper around a local OpenAI-compatible chat model.

    This service never becomes the main Omai agent. Each method has a narrow
    prompt, validates the output shape, and returns None on any failure so the
    existing main-model path can continue unchanged.
    """

    def __init__(self, model: ChatOpenAI, usage: LocalLlmUsage | None = None):
        self.model = model
        self.usage = usage or LocalLlmUsage()

    def classify_request(
        self,
        *,
        question: str,
        history: list[dict[str, str]] | None,
        today: str,
    ) -> dict[str, Any] | None:
        """Classify the request for guard/routing hints; never answer the user."""

        payload = self._json_task(
            "intent_classification",
            system=(
                "You classify Ometrics assistant requests. Return only compact JSON. "
                "Do not answer the request."
            ),
            user={
                "today": today,
                "recent_history": (history or [])[-4:],
                "allowed_intents": [
                    "report_question",
                    "reading_question",
                    "shutdown_question",
                    "data_point_question",
                    "software_help_question",
                    "operational_context_question",
                    "sql_needed",
                    "unrelated",
                    "ambiguous",
                ],
                "question": question,
                "output_schema": {
                    "intent": "one allowed intent",
                    "is_ometrics_related": "boolean",
                    "confidence": "number from 0 to 1",
                },
            },
        )
        if not payload:
            return None
        if payload.get("intent") not in {
            "report_question",
            "reading_question",
            "shutdown_question",
            "data_point_question",
            "software_help_question",
            "operational_context_question",
            "sql_needed",
            "unrelated",
            "ambiguous",
        }:
            return None
        try:
            payload["confidence"] = float(payload.get("confidence", 0))
        except (TypeError, ValueError):
            return None
        payload["is_ometrics_related"] = bool(payload.get("is_ometrics_related"))
        return payload

    def rewrite_operational_context_query(
        self,
        *,
        question: str,
        site_name: str | None,
        today: str,
    ) -> str | None:
        """Rewrite an operational-context query while preserving the original terms."""

        payload = self._json_task(
            "rag_query_rewrite",
            system=(
                "Rewrite Ometrics operational-context search queries. Return only "
                "JSON. Keep original entity numbers and field terms. Do not invent "
                "facts, dates, or source records."
            ),
            user={
                "today": today,
                "site_name": site_name,
                "question": question,
                "output_schema": {
                    "rewritten_query": "short search query with useful synonyms",
                    "confidence": "number from 0 to 1",
                },
            },
        )
        if not payload:
            return None
        rewritten = str(payload.get("rewritten_query") or "").strip()
        try:
            confidence = float(payload.get("confidence", 0))
        except (TypeError, ValueError):
            return None
        if confidence < 0.55 or len(rewritten) < 3:
            return None
        return rewritten[:500]

    def summarize_operational_context(
        self,
        *,
        question: str,
        records: list[dict[str, Any]],
    ) -> str | None:
        """Summarize already-retrieved operational records without adding facts."""

        if not records:
            return None
        return self._text_task(
            "operational_context_summary",
            system=(
                "Summarize Ometrics operational records in plain language. Use only "
                "the provided records. Do not add headings, sources, causes, or "
                "counts unless the user asked for them."
            ),
            user=json.dumps(
                {
                    "question": question,
                    "records": records[:12],
                },
                default=str,
                separators=(",", ":"),
            ),
        )

    def format_safe_tool_result(
        self,
        *,
        question: str,
        current_answer: str,
        traces: list[dict[str, Any]],
    ) -> str | None:
        """Rephrase deterministic tool answers while preserving calculated facts."""

        compact_traces = [
            {
                "tool": item.get("tool"),
                "arguments": item.get("arguments"),
                "result": _truncate(str(item.get("result", "")), 4_000),
            }
            for item in traces[:4]
        ]
        return self._text_task(
            "safe_tool_result_formatting",
            system=(
                "Rewrite the answer for an Ometrics user. Preserve all values, "
                "dates, names, and calculations exactly. Do not add new facts, "
                "follow-up offers, headings, or unsupported actions."
            ),
            user=json.dumps(
                {
                    "question": question,
                    "current_answer": current_answer,
                    "tool_traces": compact_traces,
                },
                default=str,
                separators=(",", ":"),
            ),
        )

    def format_capability_answer(
        self,
        *,
        question: str,
        current_answer: str,
        traces: list[dict[str, Any]],
    ) -> str | None:
        """Format explicit software-help answers only from capability results."""

        return self._text_task(
            "capability_answer_formatting",
            system=(
                "Answer the Ometrics software-help question using only the provided "
                "capability result/current answer. Be very concise. Do not invent "
                "UI steps, options, reports, or checks."
            ),
            user=json.dumps(
                {
                    "question": question,
                    "current_answer": current_answer,
                    "tool_traces": traces[:3],
                },
                default=str,
                separators=(",", ":"),
            ),
        )

    def _json_task(self, task: str, *, system: str, user: Any) -> dict[str, Any] | None:
        content = self._invoke(task, system=system, user=json.dumps(user, default=str))
        if content is None:
            return None
        try:
            payload = json.loads(_strip_code_fence(content))
        except (TypeError, ValueError) as exc:
            self.usage.mark_last_failed(task, f"invalid_json:{exc}")
            return None
        if not isinstance(payload, dict):
            self.usage.mark_last_failed(task, "json_not_object")
            return None
        return payload

    def _text_task(self, task: str, *, system: str, user: str) -> str | None:
        content = self._invoke(task, system=system, user=user)
        if content is None:
            return None
        text = content.strip()
        return text if text else None

    def _invoke(self, task: str, *, system: str, user: str) -> str | None:
        started_at = perf_counter()
        try:
            response = self.model.invoke(
                [
                    SystemMessage(content=system),
                    HumanMessage(content=user),
                ]
            )
        except Exception as exc:
            seconds = perf_counter() - started_at
            logger.info("Local LLM task failed task=%s error=%s", task, exc)
            self.usage.add(task, seconds, False, str(exc))
            return None
        seconds = perf_counter() - started_at
        text = _message_text(response.content)
        self.usage.add(task, seconds, bool(text), None if text else "empty_response")
        return text or None


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content)


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."

from __future__ import annotations

import re


_INVESTIGATION_PATTERNS = (
    re.compile(r"\bwhy\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+(?:caused|explains?)\b", re.IGNORECASE),
    re.compile(r"\binvestigat(?:e|ion)\b", re.IGNORECASE),
    re.compile(r"\breason(?:s)?\s+for\b", re.IGNORECASE),
    re.compile(
        r"\b(?:explain|analy[sz]e)\b.*\b(?:increase|decrease|drop|rise|change|low|high|unusual|abnormal)\b",
        re.IGNORECASE,
    ),
)


def is_investigation_request(question: str) -> bool:
    """Return whether a question asks for causal, multi-source investigation."""
    normalized = " ".join(question.split())
    return any(pattern.search(normalized) for pattern in _INVESTIGATION_PATTERNS)

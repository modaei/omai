import json
from pathlib import Path

from omai.config.settings import Settings
from omai.evals.cases import filter_cases, load_cases
from omai.evals.checks import run_deterministic_checks
from omai.evals.models import EvaluationCase, EvaluationResult
from omai.evals.runner import (
    _configure_deepeval_environment,
    _report_payload,
    _write_report,
)
from omai.services.domain_guard import OUT_OF_DOMAIN_RESPONSE


def test_load_cases_parses_json_file(tmp_path: Path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            [
                {
                    "id": "case-1",
                    "category": "reports",
                    "question": "How much gas was flared?",
                    "site_id": 4,
                    "required_tools": ["run_report"],
                    "metric_thresholds": {"answer_relevancy": 0.8},
                }
            ]
        ),
        encoding="utf-8",
    )

    cases = load_cases(path)

    assert len(cases) == 1
    assert cases[0].id == "case-1"
    assert cases[0].required_tools == ("run_report",)
    assert cases[0].metric_thresholds == {"answer_relevancy": 0.8}


def test_filter_cases_applies_category_case_id_and_limit():
    cases = [
        EvaluationCase(id="a", category="rag", question="q", site_id=4),
        EvaluationCase(id="b", category="reports", question="q", site_id=4),
        EvaluationCase(id="c", category="rag", question="q", site_id=4),
    ]

    assert [case.id for case in filter_cases(cases, category="rag")] == ["a", "c"]
    assert [case.id for case in filter_cases(cases, case_id="b")] == ["b"]
    assert [case.id for case in filter_cases(cases, limit=2)] == ["a", "b"]


def test_deterministic_checks_validate_tools_and_phrases():
    case = EvaluationCase(
        id="case-1",
        category="reports",
        question="q",
        site_id=4,
        required_tools=("run_report",),
        forbidden_tools=("execute_operational_sql",),
        required_phrases=("gas flared",),
        forbidden_phrases=("site_id",),
    )

    checks = run_deterministic_checks(
        case,
        "Gas flared total was 123 barrels.",
        [{"tool": "run_report", "arguments": {}}],
    )

    assert all(check.passed for check in checks)


def test_deterministic_checks_reject_guardrail_answer_that_is_not_refusal():
    case = EvaluationCase(
        id="guardrail",
        category="guardrail",
        question="How old is Paris?",
        site_id=4,
    )

    checks = run_deterministic_checks(case, "Paris is very old.", [])

    assert any(
        check.name == "out_of_domain_refusal" and not check.passed
        for check in checks
    )


def test_deterministic_checks_accept_guardrail_refusal():
    case = EvaluationCase(
        id="guardrail",
        category="guardrail",
        question="How old is Paris?",
        site_id=4,
    )

    checks = run_deterministic_checks(case, OUT_OF_DOMAIN_RESPONSE, [])

    assert all(check.passed for check in checks)


def test_write_report_outputs_json(tmp_path: Path):
    result = EvaluationResult(
        case=EvaluationCase(id="case-1", category="reports", question="q", site_id=4),
        answer="answer",
        tool_calls=[{"tool": "run_report"}],
        stats={"total_seconds": 1.0},
        checks=[],
        metrics=[],
    )

    path = _write_report([result], tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert path.name.startswith("omai-eval-")
    assert payload["passed"] == 1
    assert payload["total"] == 1
    assert payload["results"][0]["case"]["id"] == "case-1"


def test_report_payload_marks_failed_results():
    result = EvaluationResult(
        case=EvaluationCase(id="case-1", category="reports", question="q", site_id=4),
        answer="site_id 4",
        tool_calls=[],
        stats={},
        checks=run_deterministic_checks(
            EvaluationCase(id="case-1", category="reports", question="q", site_id=4),
            "site_id 4",
            [],
        ),
        metrics=[],
    )

    payload = _report_payload([result])

    assert payload["passed"] == 0
    assert payload["results"][0]["passed"] is False


def test_configure_deepeval_environment_uses_provider_neutral_settings(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    settings = make_settings()

    _configure_deepeval_environment(settings)

    assert settings.llm_api_key
    assert settings.llm_base_url
    assert __import__("os").environ["OPENAI_API_KEY"] == settings.llm_api_key
    assert __import__("os").environ["OPENAI_BASE_URL"] == settings.llm_base_url
    assert __import__("os").environ["OPENAI_API_BASE"] == settings.llm_base_url


def make_settings() -> Settings:
    return Settings(
        llm_api_key="test-key",
        llm_model="gpt-5-mini",
        llm_base_url="https://api.openai.com/v1",
        omreports_api_url="http://127.0.0.1:50008/report/",
        omreports_timeout_seconds=30,
        max_report_days=366,
        db_host="127.0.0.1",
        db_port=3306,
        db_user="user",
        db_password="pass",
        db_name="ometrics",
        db_pool_size=5,
        db_max_overflow=5,
        db_pool_timeout=15,
        db_pool_recycle=1800,
        operational_sql_db_user="readonly",
        operational_sql_db_password="readonly-pass",
        operational_sql_max_rows=100,
        operational_sql_timeout_seconds=10,
        omai_max_concurrent=2,
        omai_slot_timeout=1,
        omai_conversation_history_limit=20,
        omai_conversation_ttl_hours=168,
        omai_daily_user_limit_enabled=True,
        omai_daily_user_limit_requests=100,
        timezone="UTC",
        vector_db_host="127.0.0.1",
        vector_db_port=5432,
        vector_db_user="ometrics",
        vector_db_password="",
        vector_db_name="ometrics",
        vector_db_pool_size=5,
        vector_db_max_overflow=5,
        vector_db_pool_timeout=15,
        vector_db_pool_recycle=1800,
        rag_embedding_model="text-embedding-3-small",
        rag_embedding_dimensions=1536,
        log_level="INFO",
    )

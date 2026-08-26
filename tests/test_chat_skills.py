from omai.skills.chat_skills import (
    resolve_activated_skills,
    select_initial_skills,
    skill_catalog,
    tool_names_for_skills,
)


def test_report_change_question_selects_reports_and_operational_context_skills():
    selected = select_initial_skills(
        "Why was Battery 6 oil production lower in June than in May?",
        [],
    )

    assert [skill.skill_id for skill in selected] == [
        "reports_allocation",
        "operational_context",
    ]


def test_short_follow_up_inherits_prior_user_skill():
    selected = select_initial_skills(
        "focus only on those",
        [{"role": "user", "content": "Summarize operational notes for 5823."}],
    )

    assert [skill.skill_id for skill in selected] == ["operational_context"]


def test_activation_ignores_unknown_and_already_active_skills_and_is_capped():
    activated = resolve_activated_skills(
        ["reports_allocation", "unknown", "data_points", "well_tests"],
        ["reports_allocation"],
    )

    assert [skill.skill_id for skill in activated] == ["data_points", "well_tests"]


def test_catalog_and_tool_allowlist_are_static_and_complete():
    catalog = skill_catalog()
    selected = select_initial_skills("How much oil did Well 5823 contribute?", [])

    assert "reports_allocation (v1)" in catalog
    assert "operational_sql (v1)" in catalog
    assert "summarize_well_allocation" in tool_names_for_skills(selected)

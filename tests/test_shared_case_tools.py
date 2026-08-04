from types import SimpleNamespace

from omai.services.chat_service import _tools_for_shared_case_turn


def test_shared_case_follow_up_removes_fresh_rod_pump_analysis_tool():
    tools = [SimpleNamespace(name="analyze_rod_pump"), SimpleNamespace(name="get_well_timeline")]

    result = _tools_for_shared_case_turn(
        tools,
        shared_case=True,
        read_only=True,
        has_history=True,
        case_reassessment_requested=False,
    )

    assert [tool.name for tool in result] == ["get_well_timeline"]


def test_opening_and_explicit_reassessment_keep_analysis_tool():
    tools = [SimpleNamespace(name="analyze_rod_pump")]

    opening_result = _tools_for_shared_case_turn(
        tools,
        shared_case=True,
        read_only=True,
        has_history=False,
        case_reassessment_requested=False,
    )
    reassessment_result = _tools_for_shared_case_turn(
        tools,
        shared_case=True,
        read_only=True,
        has_history=True,
        case_reassessment_requested=True,
    )

    assert opening_result == tools
    assert reassessment_result == tools

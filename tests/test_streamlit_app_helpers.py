from omai.ui.streamlit_app import display_tool_calls


def test_display_tool_calls_removes_result_payloads():
    tool_calls = [
        {
            "tool": "search_operational_context",
            "arguments": {"query": "what happened"},
            "result": '{"ok":true}',
        }
    ]

    assert display_tool_calls(tool_calls) == [
        {
            "tool": "search_operational_context",
            "arguments": {"query": "what happened"},
        }
    ]

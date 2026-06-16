from __future__ import annotations

import logging

import streamlit as st

from omai.agents.chat_agent import answer_report_question, build_model
from omai.clients.capability_client import CapabilityClient, UnavailableCapabilityClient
from omai.clients.reading_client import ReadingClient, UnavailableReadingClient
from omai.clients.report_client import ReportClient
from omai.clients.shutdown_client import ShutdownClient, UnavailableShutdownClient
from omai.clients.site_client import SiteClient
from omai.clients.well_timeline_client import (
    UnavailableWellTimelineClient,
    WellTimelineClient,
)
from omai.config.settings import Settings
from omai.tools.capability_tools import build_capability_tools
from omai.tools.reading_tools import build_reading_tools
from omai.tools.report_tools import build_report_tools
from omai.tools.shutdown_tools import build_shutdown_tools
from omai.tools.well_timeline_tools import build_well_timeline_tools


@st.cache_resource
def load_settings() -> Settings:
    return Settings.from_env()


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def format_response_statistics(stats: dict | None) -> str:
    if not stats:
        return ""

    lines = [
        "",
        "**Statistics**",
        f"- Total: {stats.get('total_seconds', 0):.3f}s",
        f"- Model: {stats.get('model_seconds', 0):.3f}s across {stats.get('model_calls', 0)} call(s)",
        f"- Tools: {stats.get('tool_seconds', 0):.3f}s across {len(stats.get('tool_calls', []))} call(s)",
    ]
    for tool_call in stats.get("tool_calls", []):
        lines.append(f"- Tool `{tool_call['tool']}`: {tool_call['seconds']:.3f}s")

    return "\n".join(lines)


def main() -> None:
    st.set_page_config(page_title="Ometrics AI", page_icon="AI", layout="wide")

    settings = load_settings()
    configure_logging(settings.log_level)

    st.title("Ometrics AI Report Assistant")
    st.caption(
        "Read-only answers generated from Ometrics capabilities, reports, and raw records."
    )

    with st.sidebar:
        st.header("Configuration")
        site_id = st.number_input("Site ID", min_value=1, value=1, step=1)
        st.text_input("Model", value=settings.llm_model, disabled=True)
        st.text_input("Report API", value=settings.omreports_api_url, disabled=True)
        if st.button("Clear conversation", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

        st.header("Example questions")
        st.markdown(
            "- Show oil production from the first of this month through today.\n"
            "- Compare water production this month with last month.\n"
            "- How much gas was flared during the last seven days?\n"
            "- Show LACT readings for 2026-06-10.\n"
            "- Compare linear tank readings on 2026-06-09 and 2026-06-10.\n"
            "- Which water plant readings are missing for 2026-06-10?\n"
            "- Show well shutdowns from 2026-05-01 to 2026-05-10.\n"
            "- Which wells are currently on long shutdown?\n"
            "- Build a timeline for Well 11-1-1 Oil from 2026-05-01 to 2026-05-10.\n"
            "- A well is down. How can I register that?\n"
            "- How can I know how much Well 6243 contributed to oil production?\n"
            "- List the available reports."
        )

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message.get("stats"):
                st.markdown(format_response_statistics(message["stats"]))
            if message.get("tool_calls"):
                with st.expander("Tool calls"):
                    st.json(message["tool_calls"])

    question = st.chat_input("Ask a question about Ometrics reports")
    if not question:
        return

    question = question.strip()
    if not question:
        st.stop()
    if len(question) > 2_000:
        st.error("Question is too long. Maximum length is 2,000 characters.")
        st.stop()

    try:
        settings.validate()
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

    prior_history = [
        {"role": item["role"], "content": item["content"]}
        for item in st.session_state.messages
    ]
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.status(
            "Analyzing the question and running tools...", expanded=True
        ) as status:
            try:
                client = ReportClient(
                    api_url=settings.omreports_api_url,
                    timeout_seconds=settings.omreports_timeout_seconds,
                    max_report_days=settings.max_report_days,
                )
                try:
                    reading_client = ReadingClient.from_settings(settings)
                except ValueError as exc:
                    reading_client = UnavailableReadingClient(str(exc))
                try:
                    shutdown_client = ShutdownClient.from_settings(settings)
                except ValueError as exc:
                    shutdown_client = UnavailableShutdownClient(str(exc))
                try:
                    well_timeline_client = WellTimelineClient.from_settings(settings)
                except ValueError as exc:
                    well_timeline_client = UnavailableWellTimelineClient(str(exc))
                try:
                    capability_client = CapabilityClient.from_default()
                except Exception as exc:
                    capability_client = UnavailableCapabilityClient(str(exc))
                try:
                    site_name = SiteClient.from_settings(settings).get_site_name(
                        int(site_id)
                    )
                except Exception:
                    logging.exception("Site lookup failed")
                    site_name = None

                tools = [
                    *build_capability_tools(capability_client),
                    *build_report_tools(client, int(site_id), site_name),
                    *build_reading_tools(reading_client, int(site_id)),
                    *build_shutdown_tools(shutdown_client, int(site_id)),
                    *build_well_timeline_tools(well_timeline_client, int(site_id)),
                ]
                model = build_model(
                    api_key=settings.llm_api_key,
                    model=settings.llm_model,
                    base_url=settings.llm_base_url,
                )
                answer, tool_calls, stats = answer_report_question(
                    model=model,
                    tools=tools,
                    site_id=int(site_id),
                    site_name=site_name,
                    history=prior_history,
                    question=question,
                )
                status.update(label="Complete", state="complete", expanded=False)
            except Exception as exc:
                logging.exception("Chat request failed")
                answer = f"The request failed: {exc}"
                tool_calls = []
                stats = {}
                status.update(label="Failed", state="error", expanded=True)

        st.markdown(answer)
        st.markdown(format_response_statistics(stats))
        if tool_calls:
            with st.expander("Tool calls"):
                st.json(tool_calls)

    st.session_state.messages.append(
        {
            "role": "assistant",
            "content": answer,
            "tool_calls": tool_calls,
            "stats": stats,
        }
    )

"""
app.py — SONiC Route Consistency Checker: Streamlit dashboard

Layout:
  Left  (60%): live inconsistency table, auto-refreshes every 10 s via st.fragment
  Right (40%): Claude AI agent chat

Environment variables:
    ANTHROPIC_API_KEY   Required for the right-panel chat
    CHECKER_API_URL     Base URL of the FastAPI server (default: http://127.0.0.1:8000)

Run:
    streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0
"""

import os
import time
from datetime import datetime
from typing import Optional

import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.environ.get("CHECKER_API_URL", "http://127.0.0.1:8000")
REFRESH_INTERVAL_S = 10

# Try to import the LangGraph agent; fall back with a warning if not available.
try:
    import sys, os as _os
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from agent.agent import run_agent_query, stream_agent_response
    _AGENT_AVAILABLE = True
except Exception as _agent_import_err:
    _AGENT_AVAILABLE = False
    _agent_import_err_msg = str(_agent_import_err)

SEV_BG = {
    "critical": "#3d1a1a",
    "warning":  "#3d2e00",
    "info":     "#0f2340",
}
SEV_BADGE = {
    "critical": ("🔴", "#ff4b4b"),
    "warning":  ("🟡", "#ffa500"),
    "info":     ("🔵", "#4b9eff"),
}

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="SONiC Route Consistency Checker",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_processed_query" not in st.session_state:
    st.session_state.last_processed_query = None
if "cached_data" not in st.session_state:
    st.session_state.cached_data = None
if "last_fetch_ts" not in st.session_state:
    st.session_state.last_fetch_ts = 0.0

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def fetch_inconsistencies(raw: bool = False) -> Optional[dict]:
    """Call /inconsistencies, return parsed JSON or None on error."""
    try:
        params = {"raw": "true"} if raw else {}
        r = requests.get(f"{API_BASE}/inconsistencies", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def fetch_health() -> Optional[dict]:
    try:
        r = requests.get(f"{API_BASE}/health", timeout=3)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Left panel helper: render one inconsistency card
# ---------------------------------------------------------------------------

def render_inconsistency_card(issue: dict) -> None:
    sev = issue.get("severity", "info")
    icon, color = SEV_BADGE.get(sev, ("⚪", "#888"))
    bg = SEV_BG.get(sev, "#1e1e1e")
    prefix = issue.get("prefix", "?")
    present = ", ".join(issue.get("present_in", []))
    missing = ", ".join(issue.get("missing_in", []))
    diagnosis = issue.get("diagnosis", "")
    mismatch = issue.get("nexthop_mismatch", {})

    st.markdown(
        f"""
<div style="
  background:{bg};
  border-left: 4px solid {color};
  border-radius: 6px;
  padding: 10px 14px;
  margin-bottom: 10px;
  font-family: monospace;
">
  <span style="color:{color}; font-weight:bold;">{icon} {sev.upper()}</span>
  &nbsp;
  <span style="font-size:1.05em; font-weight:bold; color:#e8e8e8;">{prefix}</span>
  <br/>
  <span style="color:#aaa; font-size:0.88em;">
    <b>present in:</b> {present if present else "—"}
    &nbsp;&nbsp;
    <b>missing from:</b> {missing if missing else "—"}
  </span>
  <br/>
  <span style="color:#ccc; font-size:0.85em;">{diagnosis}</span>
</div>
""",
        unsafe_allow_html=True,
    )

    if mismatch:
        with st.expander(f"Nexthop mismatch detail — {prefix}", expanded=False):
            for plane, nhs in mismatch.items():
                st.code(f"{plane}: {', '.join(nhs) if nhs else '(none)'}", language=None)


# ---------------------------------------------------------------------------
# Left panel: wrapped in @st.fragment so it refreshes independently of the
# chat panel. run_every triggers a re-run of only this fragment every 10 s
# without causing a full page rerun — so the chat panel is unaffected.
# ---------------------------------------------------------------------------

@st.fragment(run_every=REFRESH_INTERVAL_S)
def inconsistency_panel() -> None:
    # Controls row
    ctrl_left, ctrl_mid, ctrl_right = st.columns([2, 1, 1])
    with ctrl_left:
        raw_mode = st.toggle("Raw mode (include suppressed noise)", value=False)
    with ctrl_mid:
        auto_refresh = st.toggle("Auto-refresh (10 s)", value=True)
    with ctrl_right:
        manual_refresh = st.button("Refresh now")

    if manual_refresh:
        st.session_state.last_fetch_ts = 0.0

    # Fetch data: always on manual refresh or when cache is stale and
    # auto-refresh is enabled. When auto_refresh is off, serve the cache.
    now = time.time()
    cache_stale = (now - st.session_state.last_fetch_ts) > REFRESH_INTERVAL_S
    should_fetch = (
        manual_refresh
        or st.session_state.cached_data is None
        or (auto_refresh and cache_stale)
    )

    if should_fetch:
        data = fetch_inconsistencies(raw=raw_mode)
        st.session_state.cached_data = data
        st.session_state.last_fetch_ts = now
    else:
        data = st.session_state.cached_data

    # Health expander
    with st.expander("API / Redis health", expanded=False):
        health = fetch_health()
        if health:
            status = health.get("status", "unknown")
            col1, col2, col3 = st.columns(3)
            col1.metric("Overall", status.upper())
            col2.metric("APP_DB Redis", health.get("redis_app_db", "?"))
            col3.metric("ASIC_DB Redis", health.get("redis_asic_db", "?"))
            age = health.get("snapshot_age_seconds")
            if age is not None:
                st.caption(f"Snapshot age: {age:.1f} s")
        else:
            st.error(f"Cannot reach checker API at {API_BASE}")

    st.divider()

    if data is None:
        st.error(
            f"Cannot reach the checker API at `{API_BASE}/inconsistencies`. "
            "Start it with:\n```\nuvicorn checker.api:app --host 0.0.0.0 --port 8000\n```"
        )
        return

    ts = data.get("snapshot_timestamp", 0)
    ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total", data.get("total", 0))
    m2.metric("🔴 Critical", data.get("critical", 0))
    m3.metric("🟡 Warning",  data.get("warning", 0))
    m4.metric("🔵 Info",     data.get("info", 0))
    st.caption(
        f"Snapshot: {ts_str}"
        + ("  |  raw mode ON" if raw_mode else "")
    )

    issues = data.get("inconsistencies", [])
    if not issues:
        st.success("All routing planes are consistent — no issues detected.")
    else:
        for issue in issues:
            render_inconsistency_card(issue)


# ---------------------------------------------------------------------------
# Main layout
# ---------------------------------------------------------------------------

left, right = st.columns([3, 2], gap="large")

# ===========================================================================
# LEFT PANEL — Inconsistency table (fragment, refreshes independently)
# ===========================================================================

with left:
    st.title("SONiC Route Consistency Checker")
    inconsistency_panel()

# ===========================================================================
# RIGHT PANEL — Claude AI agent chat
# ===========================================================================

with right:
    st.subheader("AI Diagnostic Assistant")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        st.warning(
            "Set `ANTHROPIC_API_KEY` environment variable to enable the AI assistant.\n\n"
            "```\nexport ANTHROPIC_API_KEY=sk-ant-...\n```"
        )
    elif not _AGENT_AVAILABLE:
        st.warning(
            f"LangGraph agent not available ({_agent_import_err_msg}). "
            "Install dependencies: `pip install langgraph langchain-anthropic langchain-core`"
        )
    else:
        st.caption("Powered by LangGraph ReAct agent + Claude")

        # Display chat history
        chat_container = st.container(height=480, border=False)
        with chat_container:
            for msg in st.session_state.messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

        # Chat input — st.chat_input only triggers a rerun on submit,
        # not on every keystroke, which avoids spurious reruns.
        if prompt := st.chat_input("Ask about routing state or request RCA…"):
            # BUG 1 fix: guard against processing the same query twice across
            # reruns (e.g. caused by fragment auto-refresh firing mid-response).
            if st.session_state.last_processed_query != prompt:
                st.session_state.last_processed_query = prompt
                st.session_state.messages.append({"role": "user", "content": prompt})

                with chat_container:
                    with st.chat_message("user"):
                        st.markdown(prompt)

                try:
                    with chat_container:
                        with st.chat_message("assistant"):
                            # Live tool-call status — updates as each tool
                            # fires, cleared once text starts streaming.
                            tool_status = st.empty()
                            placeholder = st.empty()

                            tool_calls_seen: list[str] = []
                            full_response = ""

                            for event_type, content in stream_agent_response(
                                query=prompt,
                                conversation_history=[
                                    m for m in st.session_state.messages[:-1]
                                ],
                            ):
                                if event_type == "tool_call":
                                    tool_calls_seen.append(content)
                                    tool_status.caption(
                                        "🔧 " + " → ".join(f"`{t}`" for t in tool_calls_seen)
                                    )
                                elif event_type == "token":
                                    full_response += content
                                    placeholder.markdown(full_response + "▌")

                            tool_status.empty()

                            # Fallback: if no text was streamed, surface a
                            # brief summary so the user never sees a blank bubble.
                            if not full_response.strip() and tool_calls_seen:
                                full_response = (
                                    f"I investigated using {len(tool_calls_seen)} tool(s): "
                                    f"{', '.join(tool_calls_seen)}. "
                                    "The agent completed but did not produce a text summary — "
                                    "try rephrasing your question for a more detailed answer."
                                )

                            placeholder.markdown(full_response)

                            if tool_calls_seen:
                                with st.expander(
                                    f"Tools used ({len(tool_calls_seen)}): "
                                    + ", ".join(tool_calls_seen),
                                    expanded=False,
                                ):
                                    for tc in tool_calls_seen:
                                        st.code(tc, language=None)

                    st.session_state.messages.append(
                        {"role": "assistant", "content": full_response}
                    )

                except Exception as exc:
                    err = f"Agent error: {exc}"
                    with chat_container:
                        with st.chat_message("assistant"):
                            st.error(err)
                    st.session_state.messages.append({"role": "assistant", "content": err})

        # Clear button
        if st.session_state.messages:
            if st.button("Clear conversation", use_container_width=True):
                st.session_state.messages = []
                st.session_state.last_processed_query = None
                st.rerun()

        st.caption(
            "The agent calls live checker tools (get_inconsistencies, get_route_detail, "
            "get_orchagent_logs, etc.) to investigate routing state. Ask it to explain "
            "a diagnosis, perform RCA, or suggest remediation steps."
        )

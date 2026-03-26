"""
agent.py — LangGraph ReAct agent for SONiC route root cause analysis.

The agent uses a standard ReAct loop (reason → act → observe) backed by
Claude claude-sonnet-4-6. It calls tools defined in tools.py to gather
routing state from the FastAPI checker endpoints.

Usage:
    # Run interactively
    python -m agent.agent

    # Invoke programmatically
    from agent.agent import run_rca
    report = run_rca("Why is 10.1.0.0/24 missing from the ASIC?")
    print(report)

Environment variables:
    ANTHROPIC_API_KEY   Required — your Anthropic API key
    CHECKER_API_URL     Checker FastAPI base URL (default: http://127.0.0.1:8000)
"""

import os
from typing import Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import (
    HumanMessage, AIMessage, AIMessageChunk, BaseMessage, SystemMessage,
)
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from typing_extensions import TypedDict, Annotated

from .prompts import SYSTEM_PROMPT
from .tools import TOOLS


# ---------------------------------------------------------------------------
# Agent state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_agent():
    """Construct and compile the LangGraph ReAct agent."""
    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0,
        max_tokens=4096,
    )
    llm_with_tools = llm.bind_tools(TOOLS)

    def call_model(state: AgentState) -> AgentState:
        """Invoke the LLM with the current message history."""
        messages = state["messages"]

        # Prepend system prompt if not already present
        if not messages or not isinstance(messages[0], SystemMessage):
            messages = [SystemMessage(content=SYSTEM_PROMPT)] + list(messages)

        response = llm_with_tools.invoke(messages)
        return {"messages": [response]}

    def should_continue(state: AgentState) -> Literal["tools", "__end__"]:
        """Route to tool execution or end based on whether tools were called."""
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "__end__"

    tool_node = ToolNode(tools=TOOLS)

    graph = StateGraph(AgentState)
    graph.add_node("agent", call_model)
    graph.add_node("tools", tool_node)

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", should_continue)
    graph.add_edge("tools", "agent")

    return graph.compile()


# Singleton — compiled once at import time
_agent = None


def get_agent():
    global _agent
    if _agent is None:
        _agent = build_agent()
    return _agent


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_rca(query: str, stream: bool = False):
    """
    Run the RCA agent with a user query.

    Args:
        query:  Natural language question, e.g.
                "Why is 10.1.0.0/24 missing from the ASIC?"
                "Are there any critical routing inconsistencies right now?"
        stream: If True, yield (event_type, content) tuples as the agent
                streams its response. If False, return the final text.

    Returns:
        str  — final agent response (if stream=False)
        generator — yields (type, content) tuples (if stream=True)
    """
    agent = get_agent()
    initial_state = {"messages": [HumanMessage(content=query)]}

    _cfg = {"recursion_limit": 25}

    if stream:
        def _stream():
            for event in agent.stream(initial_state, config=_cfg, stream_mode="values"):
                last = event["messages"][-1]
                if isinstance(last, AIMessage):
                    if last.tool_calls:
                        for tc in last.tool_calls:
                            yield ("tool_call", tc["name"])
                    elif last.content:
                        yield ("response", last.content)
        return _stream()
    else:
        final_state = agent.invoke(initial_state, config=_cfg)
        last = final_state["messages"][-1]
        return last.content if isinstance(last, AIMessage) else str(last)


def run_agent_query(query: str, conversation_history: list | None = None) -> dict:
    """
    Run a single query through the agent and return a structured result.

    This is the interface used by the Streamlit dashboard.

    Args:
        query:                Natural language question or instruction.
        conversation_history: Optional list of prior {"role", "content"} dicts
                              to maintain multi-turn context.

    Returns:
        {
          "answer":     str,          # final agent response text
          "tool_calls": list[str],    # tool names called (in order)
          "messages":   list[dict],   # full message thread as {role, content}
        }
    """
    agent = get_agent()
    history = conversation_history or []

    # Build LangChain message list from conversation history
    lc_messages: list[BaseMessage] = []
    for m in history:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))

    lc_messages.append(HumanMessage(content=query))

    initial_state = {"messages": lc_messages}
    final_state = agent.invoke(initial_state, config={"recursion_limit": 25})

    # Collect tool calls and build output message list
    tool_calls_made: list[str] = []
    output_messages: list[dict] = list(history)  # start from provided history

    for msg in final_state["messages"][len(lc_messages) - 1:]:
        if isinstance(msg, AIMessage):
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls_made.append(tc["name"])
            elif msg.content:
                output_messages.append({"role": "assistant", "content": msg.content})
        elif isinstance(msg, HumanMessage):
            # This is the query we added — already in history
            pass

    last = final_state["messages"][-1]
    answer = last.content if isinstance(last, AIMessage) else str(last)

    return {
        "answer": answer,
        "tool_calls": tool_calls_made,
        "messages": output_messages,
    }


def stream_agent_response(
    query: str,
    conversation_history: list | None = None,
):
    """
    Stream the agent's response as (event_type, content) tuples.

    Uses LangGraph stream_mode="messages" so text tokens arrive as they are
    generated rather than waiting for the full response.

    Yields:
        ("tool_call", tool_name)  — each time a new tool is invoked
        ("token",     text)       — each text token of the final answer
    """
    agent = get_agent()
    history = conversation_history or []

    lc_messages: list[BaseMessage] = []
    for m in history:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "user":
            lc_messages.append(HumanMessage(content=content))
        elif role == "assistant":
            lc_messages.append(AIMessage(content=content))
    lc_messages.append(HumanMessage(content=query))

    initial_state = {"messages": lc_messages}
    seen_tools: set[str] = set()

    for chunk, _metadata in agent.stream(
        initial_state, config={"recursion_limit": 25}, stream_mode="messages"
    ):
        if not isinstance(chunk, AIMessageChunk):
            continue
        # Report each newly-named tool call once as it starts streaming in
        for tc_chunk in (chunk.tool_call_chunks or []):
            name = tc_chunk.get("name") or ""
            if name and name not in seen_tools:
                seen_tools.add(name)
                yield ("tool_call", name)

        # Extract text — content is always a list of blocks from Claude,
        # e.g. [{'type': 'text', 'text': 'hello', 'index': 0}].
        # Skip chunks that are still building tool calls (tcc > 0) or are
        # ToolMessages (have tool_call_id).
        if chunk.tool_call_chunks or getattr(chunk, "tool_call_id", None):
            continue

        if isinstance(chunk.content, str):
            text = chunk.content
        elif isinstance(chunk.content, list):
            text = "".join(
                block.get("text", "")
                for block in chunk.content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = ""

        if text:
            yield ("token", text)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="SONiC Route RCA Agent — diagnose routing inconsistencies"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--diagnose",
        action="store_true",
        help="Run automatic diagnosis: fetch current inconsistencies and produce an RCA report.",
    )
    group.add_argument(
        "--query",
        metavar="QUESTION",
        help="Run a single query and print the answer, then exit.",
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    print("SONiC Route RCA Agent")
    print("=" * 60)

    if args.diagnose:
        print("Running automatic diagnosis...\n")
        q = (
            "Fetch the current routing inconsistencies, then investigate each one "
            "in detail. For any critical or warning issues, also check daemon status "
            "and relevant logs. Produce a full RCA report with remediation steps."
        )
        for event_type, content in run_rca(q, stream=True):
            if event_type == "tool_call":
                print(f"  [tool: {content}]", flush=True)
            elif event_type == "response":
                print(content, flush=True)
        print()
        sys.exit(0)

    if args.query:
        for event_type, content in run_rca(args.query, stream=True):
            if event_type == "tool_call":
                print(f"  [tool: {content}]", flush=True)
            elif event_type == "response":
                print(content, flush=True)
        print()
        sys.exit(0)

    # Interactive mode
    print("Type your question (Ctrl+C or empty line to exit)\n")
    while True:
        try:
            query = input("You> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break

        if not query:
            break

        print("\nAgent> ", end="", flush=True)
        try:
            for event_type, content in run_rca(query, stream=True):
                if event_type == "tool_call":
                    print(f"\n  [tool: {content}]", flush=True)
                elif event_type == "response":
                    print(content, flush=True)
        except Exception as exc:
            print(f"\nError: {exc}")
        print()

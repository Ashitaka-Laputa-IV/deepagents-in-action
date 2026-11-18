"""Chapter 1 - the agent harness: `create_agent` vs `create_deep_agent`.

Run from the repository root:

    python demo/ch01.py

What to look for:
  * both agents run the same echo tool call loop;
  * `create_deep_agent` additionally exposes the virtual filesystem and task
    tools (write_file, read_file, ls, grep, ...);
  * a successful tool call is linked by `tool_call_id` to a ToolMessage whose
    status is "success".
"""

from __future__ import annotations

from _shared import (
    banner,
    check,
    get_model,
    last_ai_text,
    run,
    section,
    show_trace,
    tool_calls,
    tool_results,
)

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from deepagents import create_deep_agent

ECHO_TEXT = "hello deepagents"


@tool
def echo(text: str) -> str:
    """Return the input text unchanged (prefixed with 'echo:')."""
    return f"echo: {text}"


def list_tools(agent) -> list[str]:
    """Read the tool names bound to the graph's `tools` node."""
    node = agent.nodes["tools"]
    tools_by_name = getattr(node, "tools_by_name", None)
    if tools_by_name is None:
        tools_by_name = getattr(getattr(node, "bound", None), "tools_by_name", {})
    return sorted(tools_by_name)


def assert_echo(messages, expected_text: str) -> None:
    calls = [
        call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    expected_ids = {
        call["id"]
        for call in calls
        if call["name"] == "echo" and call["args"] == {"text": expected_text}
    }
    check(expected_ids, "the model emitted an echo call with the exact arguments")
    successful = [
        message
        for message in messages
        if isinstance(message, ToolMessage)
        and message.name == "echo"
        and message.tool_call_id in expected_ids
        and message.status == "success"
        and message.content == f"echo: {expected_text}"
    ]
    check(successful, "the echo result is linked back by tool_call_id and succeeded")
    check(
        isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls,
        "the run ends with a final assistant message (no pending tool calls)",
    )


def main() -> None:
    banner("Chapter 1 - one harness, two levels of default tooling")
    model = get_model()
    prompt = "You are a concise assistant. Always call tools when asked, then summarise the result in one sentence."
    question = (
        f"Call the `echo` tool with text exactly '{ECHO_TEXT}' and nothing else. "
        "Do not use write_file. Then tell me what the tool returned."
    )

    section("1. Plain LangChain agent")
    plain_agent = create_agent(model=model, tools=[echo], system_prompt=prompt)
    print("graph nodes:", list(plain_agent.get_graph().nodes))
    plain_result = plain_agent.invoke({"messages": [{"role": "user", "content": question}]})
    show_trace(plain_result["messages"])
    assert_echo(plain_result["messages"], ECHO_TEXT)

    section("2. Deep agent (same tool, extra harness)")
    deep_agent = create_deep_agent(
        model=model, tools=[echo], system_prompt=prompt, name="ch01-demo"
    )
    print("graph nodes:", list(deep_agent.get_graph().nodes))
    deep_result = deep_agent.invoke({"messages": [{"role": "user", "content": question}]})
    show_trace(deep_result["messages"])
    assert_echo(deep_result["messages"], ECHO_TEXT)

    section("3. Tools added by the deep-agent harness")
    plain_tools = list_tools(plain_agent)
    deep_tools = list_tools(deep_agent)
    extra = [name for name in deep_tools if name not in plain_tools]
    print("create_agent tools      :", plain_tools)
    print("create_deep_agent tools :", deep_tools)
    print("harness extras          :", extra)

    for expected in ("read_file", "write_file", "ls", "grep", "task"):
        check(expected in deep_tools, f"harness exposes `{expected}`")
    check("write_file" not in plain_tools, "the plain agent has no filesystem tools")

    section("4. Final answers")
    print("plain :", last_ai_text(plain_result["messages"])[:300])
    print("deep  :", last_ai_text(deep_result["messages"])[:300])
    print(
        "\nTakeaway: `create_deep_agent` is still a LangGraph tool-calling loop; "
        "the difference is the middleware-provided tool surface."
    )
    print("All chapter-1 checks passed.")


if __name__ == "__main__":
    run(main)

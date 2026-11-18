"""Chapter 5 - subagent delegation with the `task` tool.

Run from the repository root:

    python demo/ch05.py

Key observation: the child agent's tool calls (lookup_case, write_file) never
appear in the parent transcript. Only a single `task` ToolMessage comes back.
The virtual filesystem, however, is shared: the parent must call read_file to
see the child's report.
"""

from __future__ import annotations

from _shared import banner, check, get_model, run, section, show_trace

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

CASE_ID = "cache-plan"
REPORT_PATH = "/research/cache-choice.md"
CASE_MATERIAL = """[A] real-time aggregation: p95 latency 420 ms, error rate 0.4% over 1000 requests.
[B] cache refreshed every 5 minutes: p95 latency 95 ms, error rate 0.6% over the same requests.
[constraint] p95 must be < 150 ms; results may be at most 5 minutes stale; error rate must be < 1%."""

lookup_trace: list[dict[str, str]] = []


@tool
def lookup_case(case_id: str) -> str:
    """Read the fixed cache-plan research material. case_id should be cache-plan."""
    result = CASE_MATERIAL if case_id == CASE_ID else f"unknown case: {case_id}"
    lookup_trace.append({"case_id": case_id, "result": result})
    return result


def calls(messages, name: str) -> list[dict]:
    return [
        call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == name
    ]


def tool_results(messages, name: str) -> list[ToolMessage]:
    return [
        message
        for message in messages
        if isinstance(message, ToolMessage) and message.name == name
    ]


def main() -> None:
    banner("Chapter 5 - delegate with `task`, share through files")
    model = get_model()

    researcher = {
        "name": "researcher",
        "description": f"Research the local cache-plan case, compare A and B, and write the evidence to {REPORT_PATH}.",
        "system_prompt": f"""You are a researcher. For case {CASE_ID}:
1. Call lookup_case(case_id="{CASE_ID}") to get the material.
2. Compare A and B against the constraints.
3. Use write_file to save a short report to {REPORT_PATH}.
4. Keep the labels [A], [B] and [constraint], include the p95 numbers, and state the conclusion.
5. Reply with only one or two sentences and the file path; do not paste the raw material.""",
        "tools": [lookup_case],
    }

    agent = create_deep_agent(
        model=model,
        tools=[],
        system_prompt=(
            "You are a coordinator. For cache-plan research you MUST call task with "
            "subagent_type='researcher'. The researcher owns lookup_case. Do not call "
            "lookup_case yourself and do not read files in this first turn; just relay "
            "the task result. The user may ask you to read the file in a later turn."
        ),
        subagents=[researcher],
        name="ch05-coordinator",
    )

    section("1. First turn: delegate")
    question = (
        "Give the cache-plan case to the researcher: compare A and B, save an evidence-based "
        f"short report to {REPORT_PATH}, and tell me the conclusion."
    )
    first = agent.invoke({"messages": [{"role": "user", "content": question}]})
    messages = first["messages"]
    print("parent message count:", len(messages))
    show_trace(messages, max_text=320)

    task_calls = calls(messages, "task")
    check(task_calls, "the parent called the task tool")
    check(
        any(call["args"].get("subagent_type") == "researcher" for call in task_calls),
        "the task targets subagent_type='researcher'",
    )
    task_results = tool_results(messages, "task")
    check(task_results and str(task_results[-1].content).strip(), "task returned an observable result")
    check(lookup_trace, "the child actually executed lookup_case")
    print("task description:", task_calls[0]["args"].get("description", "")[:300])
    print("task return:", str(task_results[-1].content)[:300])

    section("2. Context isolation: what the parent did NOT see")
    parent_tool_names = [message.name for message in messages if isinstance(message, ToolMessage)]
    parent_call_names = [
        call["name"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]
    print("parent tool results:", parent_tool_names)
    print("parent tool calls  :", parent_call_names)
    check("task" in parent_tool_names, "the parent sees the task result")
    check("lookup_case" not in parent_tool_names, "the child's lookup_case result is not merged into the parent")
    check("write_file" not in parent_tool_names, "the child's write_file result is not merged into the parent")
    check("lookup_case" not in parent_call_names, "the parent never called the child-only tool")
    check("read_file" not in parent_call_names, "the parent did not read the shared file on the first turn")

    section("3. Shared filesystem: the report exists in state")
    files = first.get("files", {})
    check(REPORT_PATH in files, f"the child wrote {REPORT_PATH} into shared state")
    report_text = files[REPORT_PATH]["content"]
    for token in ("[A]", "[B]", "[constraint]", "420", "95"):
        check(token in report_text, f"the report keeps `{token}`")
    print("report:\n" + report_text)

    section("4. Second turn: the parent explicitly reads the file")
    second = agent.invoke(
        {
            "messages": [
                *messages,
                {
                    "role": "user",
                    "content": f"Use read_file to read {REPORT_PATH}, then state its conclusion briefly.",
                },
            ],
            "files": files,
        }
    )
    new_messages = second["messages"][len(messages):]
    show_trace(new_messages, max_text=320)
    reads = tool_results(new_messages, "read_file")
    check(reads, "the parent called read_file only when explicitly asked")
    read_text = str(reads[-1].content)
    check(report_text.strip() in read_text, "read_file returns exactly the report stored in state")
    print("\nTakeaway: messages are isolated per agent; the filesystem is the shared channel.")
    print("All chapter-5 checks passed.")


if __name__ == "__main__":
    run(main)

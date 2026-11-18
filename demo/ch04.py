"""Chapter 4 - task planning with `TodoListMiddleware`.

Run from the repository root:

    python demo/ch04.py

The demo shows four things:
  1. the middleware, not `create_deep_agent` itself, adds `write_todos` + state;
  2. a real plan -> search -> submit loop, with the todo list streamed;
  3. `write_todos` replaces the whole list (omitted items disappear);
  4. a completed todo list is not evidence: the report must still pass the
     artifact/source check.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Literal

from _shared import (
    ScriptedChatModel,
    banner,
    check,
    get_model,
    run,
    section,
    show_trace,
)

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

FRAMEWORKS = ("Deep Agents", "Claude Agent SDK", "Codex SDK")
DIMENSIONS = ("positioning", "tools_files")

FACTS = {
    "Deep Agents": {
        "url": "https://docs.langchain.com/oss/python/deepagents/overview",
        "positioning": "An agent harness on top of LangChain and LangGraph; planning is opt-in.",
        "tools_files": "Built-in file tools and subagents, plus application-defined tools.",
    },
    "Claude Agent SDK": {
        "url": "https://platform.claude.com/docs/en/agent-sdk/overview",
        "positioning": "Exposes Claude Code's agent loop and tools to application developers.",
        "tools_files": "Built-in file read/edit tools and custom tool extensions.",
    },
    "Codex SDK": {
        "url": "https://developers.openai.com/codex/sdk/",
        "positioning": "Brings Codex's coding-task execution into applications and workflows.",
        "tools_files": "The SDK drives Codex; file access depends on the runtime configuration.",
    },
}

REQUEST = (
    "Compare Deep Agents, Claude Agent SDK and Codex SDK on exactly two dimensions: "
    "positioning and tools_files. Do not include integration. Call internet_search at "
    "most once per framework (three searches total), then call submit_report with "
    "comparisons keyed by the full framework names and a conclusion. Every item must "
    "cite a URL returned by this run's search. Use write_todos to plan the work."
)

SYSTEM = """You are a comparison-research agent. Use internet_search, submit_report and write_todos.
Do not use task delegation, shell or file tools.
Call internet_search at most once per framework, then submit. Never invent URLs.
Keep the todo list complete and current. Submitting the report is the deliverable;
do not stop at a plan or a prose answer. If submit_report returns issues, fix them and
resubmit until accepted is true. Finish with one short sentence.
"""


class ResearchContext:
    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []
        self.submissions: list[dict[str, Any]] = []


class LimitSearchCalls(AgentMiddleware):
    """Hide the search tool after the budget so the run must submit."""

    def __init__(self, max_searches: int = 4):
        super().__init__()
        self.max_searches = max_searches

    def wrap_model_call(self, request, handler):
        searches = sum(
            1
            for message in request.messages
            if isinstance(message, ToolMessage) and message.name == "internet_search"
        )
        if searches >= self.max_searches:
            request = request.override(
                tools=[
                    item
                    for item in request.tools
                    if getattr(item, "name", None) != "internet_search"
                ]
            )
        return handler(request)


def make_tools(context: ResearchContext):
    def internet_search(query: str, max_results: int = 5) -> dict:
        """Search framework documentation and return up to three rows."""
        names = [name for name in FRAMEWORKS if name.casefold() in query.casefold()]
        rows = [
            {
                "title": name + " documentation",
                "url": FACTS[name]["url"],
                "content": " ".join(FACTS[name][key] for key in DIMENSIONS),
            }
            for name in names
        ][: max(1, min(max_results, 3))]
        response = {"query": query, "results": rows}
        context.searches.append(deepcopy(response))
        return response

    def submit_report(report: dict) -> dict:
        """Submit the comparison report.

        Args:
            report: {"comparisons": {name: {positioning, tools_files, sources}}, "conclusion": str}
        """
        if isinstance(report, str):
            report = json.loads(report)
        issues: list[str] = []
        rows = report.get("comparisons") if isinstance(report, dict) else None
        if not isinstance(rows, dict):
            issues.append("comparisons must be a dict keyed by framework name")
            rows = {}
        usable_urls = {
            row["url"]
            for search in context.searches
            for row in search.get("results", [])
            if isinstance(row, dict) and row.get("url")
        }
        for name in FRAMEWORKS:
            item = rows.get(name)
            if not isinstance(item, dict):
                issues.append(f"missing framework: {name}")
                continue
            for key in DIMENSIONS:
                if not isinstance(item.get(key), str) or not item[key].strip():
                    issues.append(f"{name} is missing {key}")
            if "integration" in item:
                issues.append(f"{name} must not include integration in this round")
            sources = item.get("sources")
            if not isinstance(sources, list) or not sources:
                issues.append(f"{name} has no sources")
            elif any(url not in usable_urls for url in sources):
                issues.append(f"{name} cites a URL that this run did not return")
        conclusion = report.get("conclusion") if isinstance(report, dict) else None
        if not isinstance(conclusion, str) or not conclusion.strip():
            issues.append("missing conclusion")
        result = {"accepted": not issues, "issues": issues}
        context.submissions.append(
            {"report": deepcopy(report), "issues": issues, "accepted": not issues}
        )
        return result

    return [internet_search, submit_report]


def todo_updates(messages) -> list[list[dict[str, Any]]]:
    calls = {
        call["id"]: call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["name"] == "write_todos"
    }
    return [
        deepcopy(calls[message.tool_call_id]["args"]["todos"])
        for message in messages
        if isinstance(message, ToolMessage)
        and message.name == "write_todos"
        and message.status == "success"
        and message.tool_call_id in calls
    ]


def has_todo_support(agent) -> tuple[bool, bool]:
    tools = agent.nodes["tools"].bound.tools_by_name
    fields = agent.get_output_jsonschema().get("properties", {})
    return "write_todos" in tools, "todos" in fields


def part_registration() -> None:
    section("1. Who adds the todo tool and state?")
    model = get_model()
    plain = create_deep_agent(model=model, system_prompt="You are concise.")
    planning = create_deep_agent(
        model=model, middleware=[TodoListMiddleware()], system_prompt="You are concise."
    )
    print("plain   (write_todos, todos):", has_todo_support(plain))
    print("planning(write_todos, todos):", has_todo_support(planning))
    check(has_todo_support(plain) == (False, False), "without the middleware there is no planning tool/state")
    check(has_todo_support(planning) == (True, True), "TodoListMiddleware adds both the tool and the state field")


def part_real_plan() -> None:
    section("2. Real run: plan, search, submit, revise if needed")
    context = ResearchContext()
    agent = create_deep_agent(
        model=get_model(),
        tools=make_tools(context),
        middleware=[TodoListMiddleware(), LimitSearchCalls()],
        system_prompt=SYSTEM,
        checkpointer=InMemorySaver(),
        name="ch04-planner",
    )
    config = {"configurable": {"thread_id": "ch04-comparison"}, "recursion_limit": 60}
    plans: list[list[dict[str, Any]]] = []
    final_state = None
    for state in agent.stream(
        {"messages": [{"role": "user", "content": REQUEST}]},
        config=config,
        stream_mode="values",
    ):
        final_state = state
        todos = state.get("todos", [])
        if todos and (not plans or todos != plans[-1]):
            plans.append(deepcopy(todos))
            print("\ntodos:")
            for item in todos:
                print(f"  [{item['status']}] {item['content']}")
    check(final_state is not None, "the stream produced a final state")

    messages = final_state["messages"]
    show_trace(messages, max_text=260)
    check(context.searches, "internet_search was actually executed")
    check(context.submissions, "submit_report was actually called")
    accepted = [item for item in context.submissions if item["accepted"]]
    check(accepted, "at least one submission passed the source/coverage check")
    check(context.submissions[-1]["accepted"], "the final submission is accepted")
    check(plans, "the model used write_todos during the run")

    updates = todo_updates(messages)
    if updates:
        check(
            final_state.get("todos") == updates[-1],
            "state['todos'] equals the last successful write_todos payload",
        )
    print("\naccepted report:")
    print(json.dumps(context.submissions[-1]["report"], ensure_ascii=False, indent=2)[:1200])
    print("\nsearches:", [(item["query"], len(item["results"])) for item in context.searches])
    print("todo updates:", len(updates), "| revisions:", len(context.submissions))


def part_replacement() -> None:
    section("3. write_todos replaces the whole list")
    first = [
        {"content": "check Deep Agents", "status": "pending"},
        {"content": "check Claude Agent SDK", "status": "pending"},
        {"content": "check Codex SDK", "status": "pending"},
    ]
    replacement = [{"content": "check Claude Agent SDK", "status": "in_progress"}]
    agent = create_deep_agent(
        model=ScriptedChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "write_todos", "args": {"todos": first}, "id": "plan-1"}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "write_todos", "args": {"todos": replacement}, "id": "plan-2"}],
                ),
                AIMessage(content="two plans written"),
            ]
        ),
        middleware=[TodoListMiddleware()],
    )
    state = agent.invoke({"messages": [{"role": "user", "content": "write two plans"}]})
    updates = todo_updates(state["messages"])
    check(updates == [first, replacement], "each call is a full replacement, not a patch")
    check(state["todos"] == replacement, "omitted tasks disappear from state")
    print("after the second call:", state["todos"])
    print("Takeaway: submit the complete list; omission does not mean 'keep it'.")


def part_completed_is_not_evidence() -> None:
    section("4. 'All completed' is not an acceptance criterion")
    context = ResearchContext()
    fake_report = {
        "comparisons": {
            name: {
                "positioning": "claimed",
                "tools_files": "claimed",
                "sources": ["https://example.com/not-returned"],
            }
            for name in FRAMEWORKS
        },
        "conclusion": "done",
    }
    completed = [{"content": f"check {name}", "status": "completed"} for name in FRAMEWORKS]
    agent = create_deep_agent(
        model=ScriptedChatModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[{"name": "write_todos", "args": {"todos": completed}, "id": "plan-done"}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "submit_report", "args": {"report": fake_report}, "id": "fake-submit"}
                    ],
                ),
                AIMessage(content="reported"),
            ]
        ),
        tools=make_tools(context),
        middleware=[TodoListMiddleware()],
    )
    state = agent.invoke({"messages": [{"role": "user", "content": "submit"}]})
    check(state["todos"] == completed, "the todo list claims everything is done")
    check(context.submissions and not context.submissions[-1]["accepted"], "the artifact is still rejected")
    print("todos:", state["todos"])
    print("issues:", context.submissions[-1]["issues"][:3])
    print("Takeaway: verify tool outputs/artifacts, not the model's self-reported plan status.")


def main() -> None:
    banner("Chapter 4 - planning with a todo list")
    part_registration()
    part_real_plan()
    part_replacement()
    part_completed_is_not_evidence()
    print("\nAll chapter-4 checks passed.")


if __name__ == "__main__":
    run(main)

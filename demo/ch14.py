"""Chapter 14 demo: typed projections, raw events, and late subscription.

Run:
    python demo/ch14.py

Both the parent and the researcher are live OpenCode Go models. The parent
delegates with `task`, the child calls a tool that emits explicit progress via
`get_stream_writer`, and the demo projects the same run four ways:

* `interleave(...)` for typed, scope-aware streams
* raw events for `seq` and `namespace`
* a tool's custom writer events
* late subscription to prove handles are not replayed
"""

from __future__ import annotations

import json
import sys
import warnings
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _shared import banner, check, get_model, last_ai_text, section
from langchain.messages import AIMessage, HumanMessage, ToolMessage
from langchain.tools import tool
from langgraph.config import get_stream_writer
from langgraph.stream import CustomTransformer

from deepagents import create_deep_agent

warnings.filterwarnings(
    "ignore", message="The v3 streaming protocol on Pregel is experimental.*"
)

TOPIC = "streaming"
NOTES = [
    "Typed projections keep parent and child Agent streams separate.",
    "Raw seq orders events; namespace identifies the event path.",
    "Custom progress is emitted by a tool, not written by the model.",
]

DELEGATION = (
    "Call inspect_notes(topic='streaming') exactly once, then summarize the "
    "returned notes in one sentence."
)
REQUEST = {
    "messages": [
        HumanMessage(content="Delegate to researcher and summarize the streaming notes.")
    ]
}


def make_notes_tool(executions: list[dict]):
    @tool
    def inspect_notes(topic: str) -> str:
        """Read the fixed teaching notes for one topic and report progress."""
        writer = get_stream_writer()
        writer({"phase": "starting", "topic": topic, "count": 0})
        if topic != TOPIC:
            raise ValueError("Only the streaming topic is available.")
        result = {"topic": topic, "count": len(NOTES), "notes": list(NOTES)}
        executions.append({"topic": topic, "result": result})
        writer({"phase": "complete", "topic": topic, "count": len(NOTES)})
        return json.dumps(result, ensure_ascii=False)

    return inspect_notes


def make_agent():
    executions: list[dict] = []
    agent = create_deep_agent(
        model=get_model(temperature=0),
        system_prompt=(
            "You coordinate a researcher. You must call task once with "
            "subagent_type='researcher' and the exact description you are given. "
            "After the task result arrives, summarize it briefly. Do not call "
            "other tools."
        ),
        subagents=[
            {
                "name": "researcher",
                "description": "Reads fixed teaching notes and summarizes them.",
                "system_prompt": (
                    "Call inspect_notes(topic='streaming') exactly once, then "
                    "summarize the returned notes in one sentence. Do not call "
                    "the tool again."
                ),
                "model": get_model(temperature=0),
                "tools": [make_notes_tool(executions)],
            }
        ],
    )
    return agent, executions


def project_run():
    agent, executions = make_agent()
    children, parent_calls, child_calls = [], [], []
    parent_texts, child_texts, progress = [], [], []

    with agent.stream_events(
        REQUEST,
        config={"recursion_limit": 30},
        version="v3",
        transformers=[CustomTransformer],
    ) as run:
        for kind, item in run.interleave("messages", "tool_calls", "subagents"):
            if kind == "messages":
                text = str(item.text).strip()
                if text:
                    parent_texts.append(text)
            elif kind == "tool_calls":
                parent_calls.append(item)
            else:
                children.append(item)
                for child_kind, detail in item.interleave(
                    "messages", "tool_calls", "custom"
                ):
                    if child_kind == "messages":
                        text = str(detail.text).strip()
                        if text:
                            child_texts.append(text)
                    elif child_kind == "tool_calls":
                        child_calls.append(detail)
                    else:
                        progress.append(detail)
        final_state = run.output
        child_states = [child.output for child in children]

    return {
        "agent": agent,
        "executions": executions,
        "children": children,
        "parent_calls": parent_calls,
        "child_calls": child_calls,
        "parent_texts": parent_texts,
        "child_texts": child_texts,
        "progress": progress,
        "final_state": final_state,
        "child_states": child_states,
    }


def raw_events():
    agent, executions = make_agent()
    with agent.stream_events(
        REQUEST,
        config={"recursion_limit": 30},
        version="v3",
        transformers=[CustomTransformer],
    ) as run:
        # Subscribe before consuming the iterator, or the delegation is replayed
        # into a handle that no longer exists.
        child_cursor = iter(run.subagents)
        events = list(run)
        children = list(child_cursor)
        output = run.output
    return agent, executions, events, children, output


def belongs_to(namespace, path) -> bool:
    return tuple(namespace[: len(path)]) == tuple(path)


def main() -> None:
    banner("Chapter 14 - stream one run, project it for each audience")

    section("1. Typed projections separate the parent and the child")
    first = project_run()
    print(f"  parent texts: {len(first['parent_texts'])}")
    print(f"  child texts: {len(first['child_texts'])}")
    print(f"  progress: {first['progress']}")
    check(len(first["parent_calls"]) == 1, "the parent delegated once")
    check(first["parent_calls"][0].tool_name == "task" and first["parent_calls"][0].input.get("subagent_type") == "researcher", "the call is task for researcher")
    check(len(first["children"]) == 1, "one child handle arrived")
    child = first["children"][0]
    check(child.name == "researcher" and child.status == "completed", "the child completed")
    check(bool(child.path), "the child handle has a path")
    inspect_calls = [item for item in first["child_calls"] if item.tool_name == "inspect_notes"]
    check(bool(inspect_calls), "the child called inspect_notes")
    call = inspect_calls[0]
    check(call.completed and call.error is None, "the child tool finished successfully")
    check(isinstance(call.output, ToolMessage) and call.output.status == "success", "the projected output is a successful ToolMessage")
    check(len(first["executions"]) >= 1, "the tool executed")
    check(first["progress"][:2] == [{"phase": "starting", "topic": TOPIC, "count": 0}, {"phase": "complete", "topic": TOPIC, "count": len(NOTES)}], "custom progress arrived in order")
    check(isinstance(first["final_state"]["messages"][-1], AIMessage) and bool(last_ai_text(first["final_state"]["messages"]).strip()), "the final state ends with an answer")
    check(any(isinstance(message, ToolMessage) and message.name == "inspect_notes" for message in first["child_states"][0]["messages"]), "the child state has the tool result")
    print(f"  final answer: {last_ai_text(first['final_state']['messages'])[:160]}")

    section("2. Raw events expose seq, namespace, and tool lifecycle")
    _, executions, events, children, output = raw_events()
    sequences = [event["seq"] for event in events]
    counts = Counter(event["method"] for event in events)
    print(f"  raw events: {len(events)} {dict(counts)}")
    check(len(events) > 0, "raw events exist")
    check(all(left < right for left, right in zip(sequences, sequences[1:])), "seq is strictly increasing")
    check(all(isinstance(event["params"]["namespace"], list) for event in events), "every event carries a namespace list")
    check(len(children) == 1 and children[0].status == "completed", "the early subscription saw the child")
    path = children[0].path
    print(f"  child namespace path: {path}")
    custom = [event for event in events if event["method"] == "custom" and belongs_to(event["params"]["namespace"], path)]
    check(bool(custom), "custom events belong to the child namespace")
    check([event["params"]["data"] for event in custom[:2]] == [{"phase": "starting", "topic": TOPIC, "count": 0}, {"phase": "complete", "topic": TOPIC, "count": len(NOTES)}], "custom payloads match the writer calls")
    tool_events = [
        event
        for event in events
        if event["method"] == "tools" and belongs_to(event["params"]["namespace"], path)
    ]
    started = [
        event
        for event in tool_events
        if event["params"]["data"]["event"] == "tool-started"
        and event["params"]["data"]["tool_name"] == "inspect_notes"
    ]
    start_id = started[0]["params"]["data"]["tool_call_id"] if started else None
    finished = [
        event
        for event in tool_events
        if event["params"]["data"]["event"] == "tool-finished"
        and event["params"]["data"]["tool_call_id"] == start_id
    ]
    check(len(started) >= 1, "the child tool started")
    check(len(finished) == 1, "the child tool finished")
    check(started[0]["params"]["data"]["tool_name"] == "inspect_notes" and started[0]["params"]["data"]["input"] == {"topic": TOPIC}, "start identifies inspect_notes and its input")
    check(finished[0]["params"]["data"]["tool_call_id"] == started[0]["params"]["data"]["tool_call_id"], "finish points at the same tool call")
    check(started[0]["seq"] < custom[0]["seq"] < custom[1]["seq"] < finished[0]["seq"], "start < progress < finish in raw seq")
    check(bool(last_ai_text(output["messages"]).strip()) and len(executions) >= 1, "the run still completed")
    for event in events:
        if event["method"] not in {"tools", "custom"}:
            continue
        owner = "researcher" if belongs_to(event["params"]["namespace"], path) else "parent"
        data = event["params"]["data"]
        print(f"  #{event['seq']:02d} [{owner}] {event['method']}/{data.get('event', data.get('phase'))}")

    section("3. Late subscription does not replay old child handles")
    late_agent, late_executions = make_agent()
    with late_agent.stream_events(
        REQUEST, config={"recursion_limit": 30}, version="v3"
    ) as late_run:
        late_output = late_run.output
        late_children = list(late_run.subagents)
    check(len(late_executions) >= 1, "the child really ran")
    check(late_children == [], "the late subscriber got zero handles")
    check(bool(last_ai_text(late_output["messages"]).strip()), "the final state is still available")

    section("4. The model states the contract")
    answer = get_model(temperature=0).invoke(
        "In two sentences, explain why a streaming UI should subscribe to child "
        "handles before consuming the parent run, and why raw seq/namespace are "
        "useful even when typed projections exist."
    )
    print(f"  {answer.text}")
    check(bool(answer.text.strip()), "the model returned an explanation")

    print("\nAll chapter-14 checks passed.")


if __name__ == "__main__":
    main()

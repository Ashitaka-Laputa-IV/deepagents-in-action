"""Chapter 8 - long-term memory: a Store-backed `/memories/` route.

Run from the repository root:

    python demo/ch08.py

The demo uses a real OpenCode Go model and a `CompositeBackend`:

  * `/memories/**` lives in a shared `InMemoryStore`, namespaced by user id;
  * every other path lives in per-thread state;
  * `memory=["/memories/preferences.json"]` injects the current user's file
    into the system prompt before the first model call.

It then proves the three boundaries from the chapter: a new thread still sees
the user's preferences, a new thread cannot see another thread's draft, and a
different user sees a different namespace.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from _shared import (
    banner,
    check,
    content_to_text,
    get_model,
    run,
    section,
    show_trace,
    tool_calls,
    tool_results,
)

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import create_file_data
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

MEMORY_PATH = "/memories/preferences.json"
DRAFT_PATH = "/workspace/draft.txt"
STORE_KEY = "/preferences.json"
ALICE = {"comment_language": "zh", "variable_language": "en"}
BOB = {"comment_language": "en", "variable_language": "zh"}
DRAFT = "draft 42: only this thread can see it"


@dataclass(frozen=True)
class UserContext:
    user_id: str


class FirstRequestRecorder(BaseCallbackHandler):
    """Capture the exact messages of each model request."""

    def __init__(self) -> None:
        self.requests: list[list] = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self.requests.append(list(messages[0]))


def build_agent(store: InMemoryStore):
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": StoreBackend(
                namespace=lambda runtime: (runtime.context.user_id, "memories")
            )
        },
    )
    return create_deep_agent(
        model=get_model(),
        backend=backend,
        store=store,
        checkpointer=InMemorySaver(),
        context_schema=UserContext,
        memory=[MEMORY_PATH],
        system_prompt=(
            "You are a careful file assistant. Use read_file before editing. "
            f"Never put memory files into state: {MEMORY_PATH} is Store-backed. "
            "When asked to save preferences, update exactly the two JSON fields "
            "the user names, then write the draft to the requested path. "
            "Report actual tool results; never claim a file exists if a tool failed."
        ),
    )


def invoke(agent, text: str, thread_id: str, user_id: str):
    recorder = FirstRequestRecorder()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": text}]},
        config={"configurable": {"thread_id": thread_id}, "callbacks": [recorder]},
        context=UserContext(user_id=user_id),
    )
    return result, recorder.requests


def calls_and_results(result):
    calls = tool_calls(result["messages"])
    results = {m.tool_call_id: m for m in tool_results(result["messages"])}
    return calls, results


def read_text(reply) -> str:
    """Strip the `@@ lines x-y of n @@` header that read_file prepends."""
    text = content_to_text(reply.content)
    lines = text.splitlines()
    if lines and re.fullmatch(r"@@ lines \d+-\d+ of \d+ @@", lines[0]):
        return "\n".join(lines[1:])
    return text


def memory_block(requests) -> str:
    system = "\n".join(
        content_to_text(m.content) for m in requests[0] if isinstance(m, SystemMessage)
    )
    match = re.search(r"<agent_memory>\n(.*?)\n\n</agent_memory>", system, re.DOTALL)
    return match.group(1) if match else ""


def main() -> None:
    banner("Chapter 8 - shared memories, isolated threads")

    store = InMemoryStore()
    store.put(("alice", "memories"), STORE_KEY, create_file_data("{}"))
    store.put(("bob", "memories"), STORE_KEY, create_file_data(json.dumps(BOB)))
    check(store.get(("alice", "memories"), STORE_KEY) is not None, "Alice has an empty preference file")
    check(store.get(("bob", "memories"), STORE_KEY) is not None, "Bob has his own preference file")

    agent = build_agent(store)

    section("1. Save Alice's preferences and a thread-local draft")
    saved, requests = invoke(
        agent,
        (
            f"Please remember my preferences. First read {MEMORY_PATH}, then use edit_file "
            f"to replace its complete content with {json.dumps(ALICE)} (exactly these two "
            "fields, no date or extra fields). Do not call ls and do not write the draft "
            f"until the memory edit succeeds. After that write the draft {json.dumps(DRAFT)} "
            f"to {DRAFT_PATH}."
        ),
        "alice-main",
        "alice",
    )
    show_trace(saved["messages"], max_text=260)
    calls, results = calls_and_results(saved)
    names = [call["name"] for call in calls]
    check("read_file" in names, "the model read the memory file before changing it")
    preference_writes = [
        call
        for call in calls
        if call["name"] in {"edit_file", "write_file"}
        and call["args"].get("file_path") == MEMORY_PATH
    ]
    check(bool(preference_writes), "the model wrote the memory file itself")
    draft_writes = [
        call
        for call in calls
        if call["name"] == "write_file" and call["args"].get("file_path") == DRAFT_PATH
    ]
    check(bool(draft_writes), "the model wrote the thread-local draft")
    check(DRAFT_PATH in saved.get("files", {}), "the draft is in this thread's state files")
    check(MEMORY_PATH not in saved.get("files", {}), "the memory file is not duplicated into state")

    record = store.get(("alice", "memories"), STORE_KEY)
    stored = json.loads(record.value["content"])
    check(stored == ALICE, "the Store holds exactly Alice's two preferences")

    read_back = [
        reply
        for call in calls
        if call["name"] == "read_file" and call["args"].get("file_path") == MEMORY_PATH
        for reply in [results.get(call["id"])]
        if reply is not None
    ]
    check(bool(read_back), "there is a read_file result for the memory file")
    check(json.loads(read_text(read_back[0])) == {}, "the first read saw the original empty object")

    section("2. A new Alice thread sees preferences, not the old draft")
    fresh, fresh_requests = invoke(
        agent,
        f"Read {MEMORY_PATH} and {DRAFT_PATH}. Report the actual results for both paths.",
        "alice-fresh",
        "alice",
    )
    show_trace(fresh["messages"], max_text=260)
    fresh_calls, fresh_results = calls_and_results(fresh)
    by_path = {
        call["args"].get("file_path"): fresh_results.get(call["id"])
        for call in fresh_calls
        if call["name"] == "read_file"
    }
    check(MEMORY_PATH in by_path and by_path[MEMORY_PATH].status == "success", "the new thread reads Alice's preferences")
    check(json.loads(read_text(by_path[MEMORY_PATH])) == ALICE, "the shared Store returns the updated object")
    check(DRAFT_PATH in by_path and by_path[DRAFT_PATH].status == "error", "the draft is invisible in the new thread")
    check(DRAFT_PATH not in fresh.get("files", {}), "the new thread's state has no old draft")
    block = memory_block(fresh_requests)
    check(MEMORY_PATH in block and '"comment_language": "zh"' in block, "the preferences are injected via <agent_memory>")
    print("injected memory block:\n" + block)

    section("3. Bob's namespace is isolated")
    bob, bob_requests = invoke(
        agent,
        f"Read your own preference file {MEMORY_PATH} and tell me what it contains.",
        "bob-main",
        "bob",
    )
    show_trace(bob["messages"], max_text=260)
    bob_calls, bob_results = calls_and_results(bob)
    bob_read = next(
        (
            bob_results.get(call["id"])
            for call in bob_calls
            if call["name"] == "read_file" and call["args"].get("file_path") == MEMORY_PATH
        ),
        None,
    )
    check(bob_read is not None and bob_read.status == "success", "Bob can read his own memory file")
    check(json.loads(read_text(bob_read)) == BOB, "Bob's Store namespace returns Bob's object")
    bob_block = memory_block(bob_requests)
    check('"variable_language": "zh"' in bob_block, "Bob's memory injection contains Bob's values")
    check('"comment_language": "zh"' not in bob_block, "Bob never sees Alice's values")

    section("4. The old thread still has its draft")
    old, _ = invoke(agent, f"Read the draft at {DRAFT_PATH}.", "alice-main", "alice")
    show_trace(old["messages"], max_text=260)
    old_calls, old_results = calls_and_results(old)
    draft_read = next(
        (
            old_results.get(call["id"])
            for call in old_calls
            if call["name"] == "read_file" and call["args"].get("file_path") == DRAFT_PATH
        ),
        None,
    )
    check(draft_read is not None and draft_read.status == "success", "the original thread still reads its draft")
    check(DRAFT in read_text(draft_read), "the draft content is unchanged")

    state = agent.get_state({"configurable": {"thread_id": "alice-main"}})
    check(DRAFT_PATH in state.values["files"], "the checkpoint still owns the draft")

    print("\nTakeaway: Store = cross-thread facts, state = thread-scoped files,")
    print("namespace = the user boundary. `memory=[...]` only injects the current user's file.")
    print("All chapter-8 checks passed.")


if __name__ == "__main__":
    run(main)

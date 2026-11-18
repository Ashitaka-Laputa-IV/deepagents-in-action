"""Chapter 11 - filesystem permissions: allow, deny and interrupt.

Run from the repository root:

    python demo/ch11.py

The demo replays the notebook's cases on a temporary virtual filesystem:

  1. a global write deny keeps notes readable but not writable;
  2. a workspace-only allow also permits writes *outside* the workspace;
  3. an allow followed by a catch-all deny fixes that (order matters);
  4. a specific deny must come before a broader allow;
  5. writes to `/review/**` can pause for approval, while other writes are denied.

Cases 1 and 2 use the real OpenCode Go model; the remaining cases are scripted
so the tool order and arguments are exact.  The last section asks the real model
to state the ordering rule in its own words.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from _shared import (
    ScriptedChatModel,
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

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

SEED = {
    "/workspace/notes.txt": "original notes\n",
    "/workspace/private.txt": "private demo data\n",
    "/outside.txt": "outside original\n",
    "/review/report.txt": "old unpublished report\n",
}
READ_NOTES = {"name": "read_file", "args": {"file_path": "/workspace/notes.txt"}, "id": "read-notes"}
WRITE_NOTES = {
    "name": "write_file",
    "args": {"file_path": "/workspace/notes.txt", "content": "revised notes\n"},
    "id": "write-notes",
}
WRITE_OUTSIDE = {
    "name": "write_file",
    "args": {"file_path": "/outside.txt", "content": "outside changed\n"},
    "id": "write-outside",
}
READ_PRIVATE = {
    "name": "read_file",
    "args": {"file_path": "/workspace/private.txt"},
    "id": "read-private",
}
WRITE_REVIEW = {
    "name": "write_file",
    "args": {"file_path": "/review/report.txt", "content": "approved new report\n"},
    "id": "write-review",
}

READ_ONLY = [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]
WORKSPACE_ALLOW = FilesystemPermission(
    operations=["read", "write"], paths=["/workspace/**"], mode="allow"
)
DENY_ALL = FilesystemPermission(operations=["read", "write"], paths=["/**"], mode="deny")
PRIVATE_DENY = FilesystemPermission(
    operations=["read", "write"], paths=["/workspace/private.txt"], mode="deny"
)


class LimitVisibleTools(AgentMiddleware):
    """Keep only the tools a case is allowed to use."""

    def __init__(self, allowed):
        super().__init__()
        self.allowed = set(allowed)

    def wrap_model_call(self, request, handler):
        kept = [tool for tool in request.tools if tool.name in self.allowed]
        return handler(request.override(tools=kept))


def make_agent(backend, rules, calls, *, checkpointer=None, visible=None, model=None):
    responses = [AIMessage(content="", tool_calls=[call]) for call in calls]
    responses.append(AIMessage(content="Case finished; inspect the tool results."))
    return create_deep_agent(
        model=model or ScriptedChatModel(responses=responses),
        backend=backend,
        permissions=rules,
        checkpointer=checkpointer,
        middleware=[] if visible is None else [LimitVisibleTools(visible)],
        system_prompt=(
            "Call exactly the tools listed in the user message, in order, with the "
            "given arguments. Do not plan, delegate or use another tool. If a call "
            "is denied, report it and stop; never retry a denied path."
        ),
    )


def task_input(calls):
    return {
        "messages": [
            {
                "role": "user",
                "content": "Run these tool requests in order:\n"
                + json.dumps(
                    [{"name": c["name"], "args": c["args"]} for c in calls],
                    ensure_ascii=False,
                    indent=2,
                ),
            }
        ]
    }


def prepare(root: Path) -> None:
    for path, text in SEED.items():
        file = root / path.lstrip("/")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    return {
        "/" + file.relative_to(root).as_posix(): file.read_text(encoding="utf-8")
        for file in root.rglob("*")
        if file.is_file()
    }


def run_case(label, rules, calls, *, visible=None, model=None):
    print(f"\n=== {label} ===")
    with TemporaryDirectory(prefix="ch11-") as folder:
        root = Path(folder)
        prepare(root)
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        agent = make_agent(
            backend, rules, calls, visible=visible, model=model
        )
        result = agent.invoke(task_input(calls), config={"recursion_limit": 24})
        show_trace(result["messages"], max_text=220)
        files = snapshot(root)
    check(not root.exists(), "the temporary case directory is cleaned up")
    return result, files


def reply_for(result, name: str, path: str):
    calls = tool_calls(result["messages"])
    replies = {m.tool_call_id: m for m in tool_results(result["messages"])}
    for call in calls:
        if call["name"] == name and call["args"].get("file_path") == path:
            return replies.get(call["id"])
    return None


def run_live_boundary() -> tuple[dict, dict[str, str]]:
    """Let a live model choose the calls, then inspect the permission boundary."""
    print("\n=== live model against the workspace boundary ===")
    with TemporaryDirectory(prefix="ch11-live-") as folder:
        root = Path(folder)
        prepare(root)
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        agent = create_deep_agent(
            model=get_model(),
            backend=backend,
            permissions=[WORKSPACE_ALLOW, DENY_ALL],
            middleware=[LimitVisibleTools({"read_file", "write_file"})],
            system_prompt=(
                "Use only read_file and write_file. Do not plan, delegate or explain "
                "before acting. If a tool is denied, report it and stop; never retry "
                "the denied path."
            ),
        )
        result = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "First read /workspace/notes.txt. Then overwrite "
                            "/outside.txt with exactly the text outside changed. "
                            "Report both tool outcomes in one sentence."
                        ),
                    }
                ]
            },
            config={"recursion_limit": 24},
        )
        show_trace(result["messages"], max_text=220)
        files = snapshot(root)
    check(not root.exists(), "the live boundary directory is cleaned up")
    return result, files


def main() -> None:
    banner("Chapter 11 - permissions are ordered rules")

    section("1. Global write deny: read works, write is denied (real model)")
    real_prompt_calls = [READ_NOTES, WRITE_NOTES]
    result, files = run_case(
        "read-only",
        READ_ONLY,
        real_prompt_calls,
        visible={"read_file", "write_file"},
        model=get_model(),
    )
    read = reply_for(result, "read_file", "/workspace/notes.txt")
    write = reply_for(result, "write_file", "/workspace/notes.txt")
    check(read is not None and read.status == "success", "reading notes is allowed")
    check("original notes" in content_to_text(read.content), "the read returns the original notes")
    check(write is not None and write.status == "error", "writing notes is denied")
    check("permission denied" in content_to_text(write.content).lower(), "the denial names permission")
    check(files == SEED, "the denied write left every file unchanged")

    section("2. A live model hits the workspace boundary")
    result, files = run_live_boundary()
    read = reply_for(result, "read_file", "/workspace/notes.txt")
    outside = reply_for(result, "write_file", "/outside.txt")
    check(read is not None and read.status == "success", "the live model read the allowed notes")
    check(outside is not None and outside.status == "error", "the live model was denied outside the workspace")
    check("permission denied" in content_to_text(outside.content).lower(), "the live denial names permission")
    check(files == SEED, "the live boundary case changed no file")

    section("3. An allow rule without a fallback deny is not a boundary")
    _, files = run_case("allow-only", [WORKSPACE_ALLOW], [WRITE_OUTSIDE])
    check(files["/outside.txt"] == WRITE_OUTSIDE["args"]["content"], "a workspace allow alone still permits /outside.txt")

    section("4. Allow first, catch-all deny second")
    result, files = run_case(
        "workspace-allow + deny-all", [WORKSPACE_ALLOW, DENY_ALL],
        [WRITE_NOTES, WRITE_OUTSIDE],
    )
    check(files["/workspace/notes.txt"] == WRITE_NOTES["args"]["content"], "the workspace write succeeds")
    check(files["/outside.txt"] == SEED["/outside.txt"], "the outside write stays denied")

    section("5. Reversing the order denies the workspace too")
    result, files = run_case("deny-all + workspace-allow", [DENY_ALL, WORKSPACE_ALLOW], [WRITE_NOTES])
    denied = reply_for(result, "write_file", "/workspace/notes.txt")
    check(denied is not None and denied.status == "error", "the first matching rule wins")
    check(files["/workspace/notes.txt"] == SEED["/workspace/notes.txt"], "the file is untouched")

    section("6. A specific deny must precede a broad allow")
    result, files = run_case(
        "private deny first", [PRIVATE_DENY, WORKSPACE_ALLOW, DENY_ALL], [READ_PRIVATE]
    )
    denied = reply_for(result, "read_file", "/workspace/private.txt")
    check(denied is not None and denied.status == "error", "the private read is denied")
    check("private demo data" not in content_to_text(denied.content), "denied content is not leaked")
    result, files = run_case(
        "workspace allow first", [WORKSPACE_ALLOW, PRIVATE_DENY, DENY_ALL], [READ_PRIVATE]
    )
    exposed = reply_for(result, "read_file", "/workspace/private.txt")
    check(exposed is not None and exposed.status == "success", "the broad allow wins when it is first")
    check("private demo data" in content_to_text(exposed.content), "the same file is now readable")

    section("7. Interrupt mode: pause, approve, then write")
    with TemporaryDirectory(prefix="ch11-review-") as folder:
        root = Path(folder)
        prepare(root)
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        rules = [
            FilesystemPermission(operations=["write"], paths=["/review/**"], mode="interrupt"),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ]
        agent = make_agent(
            backend, rules, [WRITE_REVIEW],
            checkpointer=InMemorySaver(), visible={"write_file"},
        )
        cfg = {"configurable": {"thread_id": "ch11-review"}, "recursion_limit": 24}
        paused = agent.invoke(task_input([WRITE_REVIEW]), config=cfg, version="v2")
        check(len(paused.interrupts) == 1, "writing /review/report.txt pauses for approval")
        check((root / "review/report.txt").read_text(encoding="utf-8") == SEED["/review/report.txt"], "nothing was written before approval")
        check(not tool_results(paused.value["messages"]), "no tool ran before approval")
        action = paused.interrupts[0].value["action_requests"][0]
        check(action["name"] == "write_file", "the pending action is the reviewed write")
        approved = agent.invoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=cfg,
            version="v2",
        )
        check(not approved.interrupts, "the write completes after approval")
        check((root / "review/report.txt").read_text(encoding="utf-8") == WRITE_REVIEW["args"]["content"], "the approved content is on disk")
        check(not agent.get_state(cfg).next, "the graph has no pending node")

    section("8. A wrong path is denied, not interrupted")
    result, files = run_case(
        "wrong review path",
        [
            FilesystemPermission(operations=["write"], paths=["/review/**"], mode="interrupt"),
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny"),
        ],
        [{**WRITE_REVIEW, "args": {"file_path": "/report/report.txt", "content": "wrong\n"}, "id": "wrong"}],
    )
    wrong = reply_for(result, "write_file", "/report/report.txt")
    check(wrong is not None and wrong.status == "error", "a non-review path is rejected by the fallback deny")
    check("permission denied" in content_to_text(wrong.content).lower(), "the wrong path is denied explicitly")
    check(files == SEED, "no file changed")

    section("9. The model states the rule")
    reply = get_model().invoke(
        "In two short Chinese sentences, explain why FilesystemPermission rules are "
        "first-match-wins and why a catch-all deny should normally come last. "
        "Mention the /workspace allow and /outside.txt example."
    )
    summary = content_to_text(reply.content).strip()
    check(bool(summary), "the OpenCode Go model returned an explanation")
    print(summary)

    print("\nTakeaway: permissions are ordered, first-match-wins rules over a backend;")
    print("mode='interrupt' reuses the same pause/resume machinery as chapter 9.")
    print("All chapter-11 checks passed.")


if __name__ == "__main__":
    run(main)

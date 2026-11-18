"""Chapter 10 - a sandbox backend: execute code and move files.

Run from the repository root:

    python demo/ch10.py

Preferred path: the chapter's `DockerSandbox` (network off, read-only root,
no host mounts, dropped capabilities, memory/CPU/PID limits).  If Docker is not
running, the demo falls back to `LocalShellBackend` and prints a loud warning:
that fallback is *not* a security boundary, it only keeps the experiment
runnable.  The assertions cover upload -> execute -> read/download and a
non-zero exit code.

A scripted pass first pins the upload -> execute -> read/download mechanics and
the exit-code contract. A second pass gives the live model only the goal, so it
must choose `execute` and `read_file` itself. A final OpenCode Go call
summarizes the observed isolation properties.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

from _shared import (
    ScriptedChatModel,
    banner,
    check,
    content_to_text,
    get_model,
    last_ai_text,
    run,
    section,
    show_trace,
    tool_calls,
    tool_results,
)

from deepagents import create_deep_agent
from deepagents.backends import LocalShellBackend
from langchain_core.messages import AIMessage, ToolMessage

ROOT = Path(__file__).resolve().parents[1]
CHAPTER_DIR = ROOT / "notebooks" / "ch10"
if str(CHAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(CHAPTER_DIR))

ORDERS = [
    {"item": "paper", "quantity": 2, "price": 10},
    {"item": "pen", "quantity": 3, "price": 5},
    {"item": "book", "quantity": 1, "price": 40},
]
PROGRAM = dedent(
    """
    import json, os
    from pathlib import Path

    base = Path(os.environ.get("DEMO_WORKSPACE", "/workspace"))
    rows = json.loads((base / "orders.json").read_text(encoding="utf-8"))
    report = {
        "orders": len(rows),
        "total": sum(row["quantity"] * row["price"] for row in rows),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (base / "report.json").write_text(text, encoding="utf-8")
    print(text)
    """
)
FAIL_PROGRAM = b"print('planned failure')\nraise SystemExit(7)\n"


def docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def make_agent(backend, planned_calls):
    responses = [
        AIMessage(content="", tool_calls=[call]) for call in planned_calls
    ]
    responses.append(AIMessage(content="Finished; check the tool results."))
    return create_deep_agent(
        model=ScriptedChatModel(responses=responses),
        backend=backend,
        system_prompt=(
            "Call exactly the listed tools in order, with the given arguments. "
            "Do not plan, delegate, install packages or retry failures."
        ),
    )


def task_input(calls):
    return {
        "messages": [
            {
                "role": "user",
                "content": "Run these tools in order:\n"
                + json.dumps(
                    [{"name": c["name"], "args": c["args"]} for c in calls],
                    ensure_ascii=False,
                    indent=2,
                ),
            }
        ]
    }


def run_agent(backend, calls):
    agent = make_agent(backend, calls)
    result = agent.invoke(task_input(calls), config={"recursion_limit": 24})
    replies = tool_results(result["messages"])
    check(len(replies) == len(calls), "every requested tool produced one result")
    for call, reply in zip(tool_calls(result["messages"]), replies):
        check(reply.tool_call_id == call["id"], "tool_call_id links every result")
        check(reply.status == "success", f"{call['name']} did not report a transport error")
    return result, replies


def live_selection(backend, command: str) -> None:
    """Let the live model choose execute/read_file instead of scripting them."""
    agent = create_deep_agent(
        model=get_model(temperature=0),
        backend=backend,
        system_prompt=(
            "You operate a sandbox. Follow the two steps exactly. Do not use "
            "write_file, edit_file, task, or any planning tool."
        ),
    )
    prompt = (
        "Do exactly two steps, in order:\n"
        f"1. Call execute with command exactly: {command}\n"
        "2. Call read_file with file_path exactly: /workspace/report.json\n"
        "Then reply with the report's total in one short sentence."
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        config={"recursion_limit": 30},
    )
    show_trace(result["messages"], max_text=320)
    names = [call["name"] for call in tool_calls(result["messages"])]
    check("execute" in names, "the live model chose execute")
    check("read_file" in names, "the live model chose read_file")

    replies = tool_results(result["messages"])
    execute_ok = any(
        reply.name == "execute"
        and isinstance(reply.artifact, dict)
        and reply.artifact.get("exit_code") == 0
        for reply in replies
    )
    check(execute_ok, "the live execute call produced exit code 0")
    read_text = "\n".join(
        content_to_text(reply.content) for reply in replies if reply.name == "read_file"
    )
    check('"total": 75' in read_text, "the live read_file call returned the report")
    final = last_ai_text(result["messages"])
    check("75" in final, "the live model reports the total from the tool results")
    print("  live final answer:", final[:200])


def report_from_download(backend) -> dict:
    downloads = backend.download_files(
        ["/workspace/report.json", "/workspace/missing.json"]
    )
    check(downloads[0].error is None and downloads[0].content, "the report downloads from the backend")
    check(downloads[1].content is None and downloads[1].error == "file_not_found", "a missing file is a per-item error")
    report = json.loads(downloads[0].content.decode("utf-8"))
    check(report == {"orders": 3, "total": 75}, "the report totals three orders and 75")
    return report


def exercise(backend, *, docker: bool) -> None:
    if docker:
        check(backend.id and not backend.id.startswith("local-"), "a Docker container backs the tool calls")
        info = backend.inspect()
        host = info["HostConfig"]
        check(host["NetworkMode"] == "none", "container networking is disabled")
        check(host["ReadonlyRootfs"] and info["Mounts"] == [], "the root filesystem is read-only with no host mounts")
        check(not any(v.startswith(("MODEL_", "SILICONFLOW_", "OPENCODE_GO_")) for v in info["Config"]["Env"]), "model secrets are not in the container environment")
        command_execute = "python /workspace/analyze.py"
        command_fail = "python /workspace/fail.py"
    else:
        print("[WARN] Docker is unavailable: using LocalShellBackend, which is NOT isolated.")
        check(backend.id.startswith("local-"), "the fallback backend identifies itself as local")
        command_execute = f'"{sys.executable}" workspace/analyze.py'
        command_fail = f'"{sys.executable}" workspace/fail.py'

    uploads = backend.upload_files(
        [
            (
                "/workspace/orders.json",
                json.dumps(ORDERS, ensure_ascii=False).encode("utf-8"),
            ),
            ("/workspace/analyze.py", PROGRAM.encode("utf-8")),
            ("/workspace/fail.py", FAIL_PROGRAM),
        ]
    )
    check(all(item.error is None for item in uploads), "all input files upload successfully")

    calls = [
        {"name": "execute", "args": {"command": command_execute}, "id": "ch10-execute"},
        {"name": "read_file", "args": {"file_path": "/workspace/report.json"}, "id": "ch10-read"},
    ]
    result, replies = run_agent(backend, calls)
    show_trace(result["messages"], max_text=320)
    check(replies[0].artifact["exit_code"] == 0, "the analysis program exits 0")
    check("75" in content_to_text(replies[0].content), "the program prints the total")
    read_back = content_to_text(replies[1].content)
    check('"total": 75' in read_back, "the agent reads the generated report")
    report = report_from_download(backend)
    check(json.loads(read_back.partition("\n")[2]) == report, "the agent read and the host download agree")

    fail_calls = [
        {"name": "execute", "args": {"command": command_fail}, "id": "ch10-failure"}
    ]
    _, fail_replies = run_agent(backend, fail_calls)
    check(fail_replies[0].artifact["exit_code"] == 7, "a non-zero exit code is preserved")
    check("planned failure" in content_to_text(fail_replies[0].content), "stderr/stdout from the failure is visible")

    section("2. Live model chooses the sandbox tools")
    live_selection(backend, command_execute)


def main() -> None:
    banner("Chapter 10 - execute and move files inside a sandbox")
    fallback_path = None
    with tempfile.TemporaryDirectory(prefix="ch10-fallback-") as directory:
        use_docker = docker_available()
        if use_docker:
            print("Docker is available; starting the pinned container.")
            from docker_sandbox import DockerSandbox, container_exists

            with DockerSandbox() as backend:
                name = backend.id
                section("1. Container properties and the analysis run")
                exercise(backend, docker=True)
            section("3. The container is removed on exit")
            check(not backend.active, "the sandbox marks itself inactive")
            check(not container_exists(name), "the container no longer exists")
            print("container name:", name)
        else:
            print("Docker daemon is not reachable; running the documented local fallback.")
            env = os.environ.copy()
            env["DEMO_WORKSPACE"] = str(Path(directory) / "workspace")
            backend = LocalShellBackend(
                root_dir=directory,
                virtual_mode=True,
                inherit_env=True,
                env=env,
            )
            section("1. Local fallback: upload -> execute -> read/download")
            exercise(backend, docker=False)
            fallback_path = Path(directory)

    if fallback_path is not None:
        section("3. Cleanup")
        check(not fallback_path.exists(), "the temporary fallback directory is removed on exit")

    section("4. What the real model says about the trade-off")
    prompt = (
        "In two short Chinese sentences, explain why a Docker sandbox with "
        "network=none, a read-only root filesystem, no host mounts and dropped "
        "capabilities is safer than a LocalShellBackend for agent code execution."
    )
    reply = get_model().invoke(prompt)
    summary = content_to_text(reply.content).strip()
    check(bool(summary), "the OpenCode Go model returned an explanation")
    print(summary)

    print("\nTakeaway: the sandbox is a backend. `execute` returns an exit code and")
    print("output; upload/download are explicit host<->sandbox file transfers.")
    print("All chapter-10 checks passed.")


if __name__ == "__main__":
    run(main)

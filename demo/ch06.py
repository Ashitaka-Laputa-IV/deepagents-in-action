"""Chapter 6 - the async-subagent lifecycle on a real local Agent Server.

Run from the repository root:

    python demo/ch06.py

The chapter notebook starts `langgraph dev` against `notebooks/ch06`.  This
standalone demo recreates the same two graphs in a temporary directory:

  * `researcher` is a deliberately slow graph (12 s) that returns a report;
  * `supervisor` is a deep agent with `AsyncSubAgent(graph_id="researcher")`.

The supervisor is a live OpenCode Go model. A tiny command protocol
(`START|...`, `CHECK|id`, `LIST`, `UPDATE|id|...`, `CANCEL|id`) makes the
lifecycle deterministic while still requiring the model to choose and call the
async-subagent tools itself. The demo asserts the server-side thread/run state:
started -> running -> success, update -> new run, cancel -> interrupted.

Requires the optional server dependencies:

    uv sync --project notebooks --locked --extra server
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _shared import banner, check, content_to_text, get_model, run, section

from langgraph_sdk import get_client

RESEARCH_SECONDS = 12
RESEARCHER_SOURCE = '''
"""A deliberately slow graph so background execution is observable."""

import asyncio

from langgraph.graph import END, START, MessagesState, StateGraph


async def research(state: MessagesState):
    request = next(
        (message.content for message in reversed(state["messages"]) if message.type == "human"),
        "",
    )
    await asyncio.sleep(int(__import__("os").environ.get("DEMO_RESEARCH_SECONDS", "6")))
    return {"messages": [{"role": "ai", "content": f"Research completed: {request}"}]}


builder = StateGraph(MessagesState)
builder.add_node("research", research)
builder.add_edge(START, "research")
builder.add_edge("research", END)
graph = builder.compile()
'''

SUPERVISOR_SOURCE = '''
"""Live supervisor: map a tiny command protocol to async-subagent tools."""

from _shared import get_model
from deepagents import AsyncSubAgent, create_deep_agent

SYSTEM_PROMPT = """You are a protocol adapter for an async-subagent experiment.
Each user message starts with one of these commands. Issue exactly one matching
tool call, then stop; never answer with a tool name in prose.

- START|<description> -> start_async_task with subagent_type="researcher" and
  description set to the text after the first "|".
- CHECK|<task_id> -> check_async_task with task_id exactly as given.
- LIST -> list_async_tasks with status_filter="all".
- UPDATE|<task_id>|<message> -> update_async_task with task_id and message set to
  the text after the second "|".
- CANCEL|<task_id> -> cancel_async_task with task_id exactly as given.

Never invent or alter a task id. Never call write_todos, task, filesystem or
shell tools. After a tool result arrives, reply with the single word: ok."""

graph = create_deep_agent(
    model=get_model(temperature=0, timeout=120),
    system_prompt=SYSTEM_PROMPT,
    subagents=[
        AsyncSubAgent(
            name="researcher",
            description="A slow local research graph for observing background tasks.",
            graph_id="researcher",
        )
    ],
)
'''

LANGGRAPH_JSON = """
{
  "dependencies": ["./"],
  "graphs": {
    "supervisor": "./graphs/supervisor.py:graph",
    "researcher": "./graphs/researcher.py:graph"
  }
}
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LocalServer:
    """Own a temporary `langgraph dev` process and clean it up on exit."""

    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="ch06-async-")
        self.workdir = Path(self.directory.name)
        self.log = tempfile.NamedTemporaryFile(
            mode="w", prefix="ch06-server-", suffix=".log", delete=False
        )
        self.log_path = Path(self.log.name)
        self.process: subprocess.Popen | None = None
        self.client = None
        self.thread_ids: list[str] = []

    async def __aenter__(self) -> "LocalServer":
        graphs = self.workdir / "graphs"
        graphs.mkdir()
        (graphs / "researcher.py").write_text(RESEARCHER_SOURCE, encoding="utf-8")
        (graphs / "supervisor.py").write_text(SUPERVISOR_SOURCE, encoding="utf-8")
        (self.workdir / "langgraph.json").write_text(LANGGRAPH_JSON, encoding="utf-8")
        cli = Path(sys.executable).with_name("langgraph.exe" if os.name == "nt" else "langgraph")
        check(cli.is_file(), "langgraph CLI is installed (uv sync --project notebooks --locked --extra server)")
        port = free_port()
        self.client = get_client(url=f"http://127.0.0.1:{port}")
        env = os.environ.copy()
        env.update(
            PYTHONPATH=os.pathsep.join(
                [str(Path(__file__).resolve().parent), env.get("PYTHONPATH", "")]
            ),
            DEMO_RESEARCH_SECONDS=str(RESEARCH_SECONDS),
            LANGSMITH_TRACING="false",
            LANGCHAIN_TRACING_V2="false",
        )
        self.process = subprocess.Popen(
            [
                str(cli),
                "dev",
                "--host",
                "127.0.0.1",
                "--no-browser",
                "--no-reload",
                "--port",
                str(port),
                "--n-jobs-per-worker",
                "4",
                "--config",
                "langgraph.json",
            ],
            cwd=self.workdir,
            env=env,
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        try:
            await self._wait_ready()
        except BaseException:
            print("Server startup failed; diagnostic log:", self.log_path)
            if self.process.poll() is None:
                await asyncio.to_thread(self._stop_process_tree)
            self.log.close()
            raise
        return self

    async def _wait_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Agent Server exited early; see {self.log_path}")
            try:
                assistants = await asyncio.wait_for(self.client.assistants.search(), 2)
                names = {item["graph_id"] for item in assistants}
                if {"supervisor", "researcher"} <= names:
                    print("Agent Server is ready on 127.0.0.1.")
                    return
            except Exception:
                pass
            await asyncio.sleep(0.3)
        raise TimeoutError(f"Agent Server was not ready in {timeout}s; see {self.log_path}")

    async def new_thread(self) -> str:
        thread_id = (await self.client.threads.create())["thread_id"]
        self.thread_ids.append(thread_id)
        return thread_id

    async def ask(self, thread_id: str, text: str) -> dict:
        return await asyncio.wait_for(
            self.client.runs.wait(
                thread_id,
                "supervisor",
                input={"messages": [{"role": "user", "content": text}]},
            ),
            timeout=90,
        )

    async def close(self) -> None:
        try:
            if self.process is not None and self.process.poll() is None:
                for thread_id in self.thread_ids:
                    try:
                        await self.client.threads.delete(thread_id)
                    except Exception:
                        pass
                await asyncio.to_thread(self._stop_process_tree)
        finally:
            self.directory.cleanup()
            self.log.close()
            self.log_path.unlink(missing_ok=True)

    def _stop_process_tree(self) -> None:
        import psutil

        try:
            parent = psutil.Process(self.process.pid)
        except psutil.NoSuchProcess:
            return
        processes = [*reversed(parent.children(recursive=True)), parent]
        for process in processes:
            try:
                process.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(processes, timeout=10)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=5)

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        await self.close()
        if exc_type is None:
            print("Server, threads and temporary graphs cleaned up.")
        return False


def latest_tool_text(state: dict) -> str:
    tools = [m for m in state["messages"] if m.get("type") == "tool"]
    return content_to_text(tools[-1]["content"]) if tools else ""


async def wait_for_task(server: LocalServer, thread_id: str, task_id: str) -> tuple[dict, dict]:
    deadline = time.monotonic() + 45
    last_status = None
    while time.monotonic() < deadline:
        state = await server.ask(thread_id, f"CHECK|{task_id}")
        task = state["async_tasks"][task_id]
        if task["status"] != last_status:
            print(f"  check_async_task -> {task['status']}")
            last_status = task["status"]
        if task["status"] == "success":
            run = await server.client.runs.get(task_id, task["run_id"])
            check(run["status"] == "success", "the server run finished successfully")
            return state, task
        if task["status"] in {"error", "cancelled", "interrupted", "timeout"}:
            raise RuntimeError(f"background task failed: {task['status']}")
        await asyncio.sleep(1)
    raise TimeoutError(f"task {task_id} did not finish")


async def main() -> None:
    banner("Chapter 6 - async subagents: start, update, cancel")
    async with LocalServer() as server:
        parent = await server.new_thread()

        section("1. Start -> parent returns while the child is still running")
        state = await server.ask(parent, "START|总结异步任务的三个生命周期阶段")
        check(len(state["async_tasks"]) == 1, "exactly one async task is tracked")
        task_id = next(iter(state["async_tasks"]))
        task = state["async_tasks"][task_id]
        check(task["status"] == "running", "the tool returns while the background task is running")
        run_before = await server.client.runs.get(task_id, task["run_id"])
        check(run_before["status"] in {"pending", "running"}, "the child run is live on the server")
        check(task_id in latest_tool_text(state), "start_async_task reports the task id")
        print(f"  task_id={task_id}  status={task['status']}  run={task['run_id']}")

        section("2. List and wait for completion")
        listed = await server.ask(parent, "LIST")
        check(task_id in latest_tool_text(listed), "list_async_tasks includes the task")
        done_state, done_task = await wait_for_task(server, parent, task_id)
        result_text = json.loads(latest_tool_text(done_state))["result"]
        check("Research completed" in result_text, "the child report is visible through check_async_task")
        print("  result:", result_text)

        section("3. Update -> same task id, new run")
        before = set(done_state["async_tasks"])
        state = await server.ask(parent, "START|撰写一份简短摘要")
        task_id_2 = (set(state["async_tasks"]) - before).pop()
        old_run = state["async_tasks"][task_id_2]["run_id"]
        state = await server.ask(
            parent, f"UPDATE|{task_id_2}|追加要求：使用三条要点"
        )
        updated = state["async_tasks"][task_id_2]
        check(updated["run_id"] != old_run, "update created a new run id")
        check(updated["status"] in {"running", "pending"}, "the updated task is running again")
        final_state, _ = await wait_for_task(server, parent, task_id_2)
        updated_result = json.loads(latest_tool_text(final_state))["result"]
        check("追加要求" in updated_result, "the update message reached the child graph")
        print("  new run:", updated["run_id"])

        section("4. Cancel -> server confirms interruption")
        before = set(final_state["async_tasks"])
        state = await server.ask(parent, "START|准备一个会被取消的任务")
        task_id_3 = (set(state["async_tasks"]) - before).pop()
        cancelled = state["async_tasks"][task_id_3]
        check(cancelled["status"] == "running", "the cancellation target is still running")
        for attempt in range(3):
            state = await server.ask(parent, f"CANCEL|{task_id_3}")
            cancelled = state["async_tasks"][task_id_3]
            print("  cancel tool result:", latest_tool_text(state)[:200])
            if cancelled["status"] == "cancelled":
                break
            print(f"  cancel was not applied (attempt {attempt + 1}); retrying the command")
        check(cancelled["status"] == "cancelled", "cancel_async_task marks the task cancelled")
        deadline = time.monotonic() + 10
        while True:
            cancelled_run = await server.client.runs.get(task_id_3, cancelled["run_id"])
            if cancelled_run["status"] in {"interrupted", "cancelled"}:
                break
            if time.monotonic() > deadline:
                raise TimeoutError("the server did not acknowledge cancellation")
            await asyncio.sleep(0.25)
        check(
            cancelled_run["status"] in {"interrupted", "cancelled"},
            "the server run is interrupted or cancelled",
        )
        print("  server run status:", cancelled_run["status"])

        section("5. The real OpenCode Go model reads the observations")
        summary_prompt = (
            "You are reviewing a local experiment. Summarise these async subagent "
            "observations in three short Chinese bullet points: start returned before "
            "the child finished; update kept the task id but created a new run; cancel "
            "stopped the run. Observations:\n"
            f"- first task {task_id} finished with: {result_text}\n"
            f"- updated task {task_id_2} produced: {updated_result}\n"
            f"- cancelled task {task_id_3} ended as: {cancelled['status']} / "
            f"{cancelled_run['status']}"
        )
        reply = get_model().invoke(summary_prompt)
        summary = content_to_text(reply.content).strip()
        check(bool(summary), "the OpenCode Go model returned a summary")
        print(summary)

    print("\nTakeaway: an async subagent is a separate thread + run on the server.")
    print("The parent only keeps handles; polling, updating and cancelling are explicit.")
    print("All chapter-6 checks passed.")


if __name__ == "__main__":
    run(lambda: asyncio.run(main()))

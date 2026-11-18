"""Chapter 9 - human-in-the-loop approvals with pause and resume.

Run from the repository root:

    python demo/ch09.py

Five experiments share the same tools and checkpointer:

  1. approve  - the email runs once, with the original arguments;
  2. edit     - the reviewer changes the recipient before it runs;
  3. reject   - the tool never runs and the reason becomes an error ToolMessage;
  4. respond  - a question is answered by the human, not by the tool;
  5. batch    - one decision per action is mandatory; a short list is rejected.

The first four use the real OpenCode Go model.  The batch case is scripted so
it can emit two tool calls in one message deterministically.
"""

from __future__ import annotations

import json

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
)

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

ORIGINAL = {
    "to": "all@example.com",
    "subject": "Release plan",
    "body": "Ship tomorrow after tests pass.",
}
EDITED = {**ORIGINAL, "to": "team@example.com"}
QUESTION = "Should the report be monthly or quarterly?"
ANSWER = "Quarterly, excluding test data."
REJECT_REASON = "Do not send to all; keep it as a draft and do not retry."


def make_tools(records: list[dict]):
    @tool
    def send_email(to: str, subject: str, body: str) -> str:
        """Record a teaching email; nothing is actually sent."""
        args = {"to": to, "subject": subject, "body": body}
        records.append({"name": "send_email", "args": args})
        return json.dumps(args, ensure_ascii=False)

    @tool
    def ask_user(question: str) -> str:
        """Ask the human a question; the answer arrives through HITL respond."""
        records.append({"name": "ask_user", "args": {"question": question}})
        return "placeholder - the human answer replaces this result"

    return [send_email, ask_user]


def build_agent(records: list[dict], model=None):
    return create_deep_agent(
        model=model or get_model(),
        tools=make_tools(records),
        checkpointer=InMemorySaver(),
        interrupt_on={
            "send_email": {"allowed_decisions": ["approve", "edit", "reject"]},
            "ask_user": {"allowed_decisions": ["respond"]},
        },
        system_prompt=(
            "You are a teaching agent for a human-in-the-loop experiment. "
            "Use exactly the tool and arguments in the user's request, copy them "
            "verbatim, and emit the requested call immediately. Do not use file, "
            "task or shell tools. After a tool result, summarise it and stop; do not "
            "call the tool again."
        ),
    )


def config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def paused_action(agent, prompt: str, thread_id: str) -> tuple[object, dict]:
    result = agent.invoke(
        {"messages": [{"role": "user", "content": prompt}]},
        config=config(thread_id),
        version="v2",
    )
    check(len(result.interrupts) == 1, f"{thread_id}: one interrupt group was created")
    payload = result.interrupts[0].value
    check(len(payload["action_requests"]) >= 1, f"{thread_id}: the interrupt lists the pending action")
    action = payload["action_requests"][0]
    return result, action


def resume(agent, thread_id: str, decisions: list[dict]):
    return agent.invoke(
        Command(resume={"decisions": decisions}),
        config=config(thread_id),
        version="v2",
    )


def tool_reply(result, call_id: str) -> ToolMessage:
    matches = [
        message
        for message in result.value["messages"]
        if isinstance(message, ToolMessage) and message.tool_call_id == call_id
    ]
    check(len(matches) == 1, f"exactly one ToolMessage for {call_id}")
    return matches[0]


def main() -> None:
    banner("Chapter 9 - approve, edit, reject, respond")

    records: list[dict] = []
    agent = build_agent(records)

    section("1. Approve: the tool runs once, with the original arguments")
    prompt = (
        "Call send_email exactly once with these arguments and nothing else: "
        + json.dumps(ORIGINAL, ensure_ascii=False)
    )
    paused, action = paused_action(agent, prompt, "ch09-approve")
    call_id = paused.value["messages"][-1].tool_calls[0]["id"]
    check(action["name"] == "send_email", "the pending action is send_email")
    check(action["args"] == ORIGINAL, "the pending arguments match the request verbatim")
    check(records == [], "the tool did not run before approval")
    print("pending:", action["name"], action["args"])
    approved = resume(agent, "ch09-approve", [{"type": "approve"}])
    check(records == [{"name": "send_email", "args": ORIGINAL}], "approval executed the email exactly once")
    reply = tool_reply(approved, call_id)
    check(reply.status == "success" and json.loads(content_to_text(reply.content)) == ORIGINAL, "the tool result carries the executed arguments")
    check(not approved.interrupts, "the run completed after approval")
    print("final:", last_ai_text(approved.value["messages"])[:300])

    section("2. Edit: the reviewer changes the recipient before execution")
    records.clear()
    paused, _ = paused_action(agent, prompt, "ch09-edit")
    call_id = paused.value["messages"][-1].tool_calls[0]["id"]
    edited = resume(
        agent,
        "ch09-edit",
        [{"type": "edit", "edited_action": {"name": "send_email", "args": EDITED}}],
    )
    check(records == [{"name": "send_email", "args": EDITED}], "the edited action is what actually ran")
    check(records[0]["args"]["to"] == "team@example.com", "the original recipient was replaced")
    reply = tool_reply(edited, call_id)
    edited_text = content_to_text(reply.content)
    print("edit feedback:", repr(edited_text[:300]))
    check(
        reply.status == "success" and "team@example.com" in edited_text,
        "the model sees the edited result",
    )
    print("executed:", records[0])

    section("3. Reject: nothing runs and the reason is fed back")
    records.clear()
    paused, _ = paused_action(agent, prompt, "ch09-reject")
    call_id = paused.value["messages"][-1].tool_calls[0]["id"]
    rejected = resume(
        agent, "ch09-reject", [{"type": "reject", "message": REJECT_REASON}]
    )
    check(records == [], "a rejected action never executes")
    reply = tool_reply(rejected, call_id)
    check(reply.status == "error", "rejection becomes an error ToolMessage")
    check(REJECT_REASON in content_to_text(reply.content), "the rejection reason reaches the model")
    print("feedback:", content_to_text(reply.content))

    section("4. Respond: the human answer replaces the tool result")
    records.clear()
    question_prompt = (
        "Call ask_user exactly once with question "
        + json.dumps(QUESTION, ensure_ascii=False)
        + " and nothing else."
    )
    paused, action = paused_action(agent, question_prompt, "ch09-respond")
    check(action["name"] == "ask_user", "the pending action is ask_user")
    call_id = paused.value["messages"][-1].tool_calls[0]["id"]
    responded = resume(
        agent, "ch09-respond", [{"type": "respond", "message": ANSWER}]
    )
    check(records == [], "respond does not execute ask_user")
    reply = tool_reply(responded, call_id)
    check(reply.status == "success" and content_to_text(reply.content) == ANSWER, "the human answer is the tool result")
    print("answer:", content_to_text(reply.content))

    section("5. Batch: one decision per action is required")
    batch_records: list[dict] = []
    batch_calls = [
        {"name": "send_email", "args": EDITED, "id": "batch-team"},
        {"name": "send_email", "args": ORIGINAL, "id": "batch-all"},
    ]
    scripted = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=batch_calls),
            AIMessage(content="Batch complete."),
        ]
    )
    batch_agent = build_agent(batch_records, model=scripted)
    paused = batch_agent.invoke(
        {"messages": [{"role": "user", "content": "Run the two teaching emails."}]},
        config=config("ch09-batch"),
        version="v2",
    )
    check(len(paused.interrupts[0].value["action_requests"]) == 2, "both calls are in one approval group")
    try:
        batch_agent.invoke(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config("ch09-batch"),
            version="v2",
        )
    except ValueError as error:
        check("does not match" in str(error), "a decision count mismatch is rejected")
        print("expected error:", error)
    else:
        raise AssertionError("one decision for two actions should fail")
    check(batch_records == [], "no tool ran after the invalid decision count")

    valid_records: list[dict] = []
    valid_agent = build_agent(
        valid_records,
        model=ScriptedChatModel(
            responses=[
                AIMessage(content="", tool_calls=batch_calls),
                AIMessage(content="Batch complete."),
            ]
        ),
    )
    valid = valid_agent.invoke(
        {"messages": [{"role": "user", "content": "Run the two teaching emails."}]},
        config=config("ch09-batch-valid"),
        version="v2",
    )
    check(len(valid.interrupts[0].value["action_requests"]) == 2, "a fresh batch pauses with two actions")
    ok = valid_agent.invoke(
        Command(
            resume={
                "decisions": [
                    {"type": "approve"},
                    {"type": "reject", "message": REJECT_REASON},
                ]
            }
        ),
        config=config("ch09-batch-valid"),
        version="v2",
    )
    check(valid_records == [{"name": "send_email", "args": EDITED}], "the approved action runs and the rejected one does not")
    check(not ok.interrupts, "the batch completed")

    print("\nTakeaway: `interrupt_on` pauses before execution; the paused state is a")
    print("checkpoint, so `Command(resume=...)` continues the same graph run.")
    print("All chapter-9 checks passed.")


if __name__ == "__main__":
    run(main)

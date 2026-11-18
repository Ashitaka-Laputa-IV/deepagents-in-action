"""Chapter 7 - skills and progressive disclosure.

Run from the repository root:

    python demo/ch07.py

The demo writes a real skill directory to a temporary folder, points a deep
agent at it with `skills=["/skills/"]`, and records every model request.  It
verifies the disclosure order:

    1st request: catalog entry only (name + description + SKILL.md path)
    2nd request: SKILL.md body is present, references/ is not
    3rd request: references/checks.md is present

It also removes the reference file and checks that the agent reports the
missing path instead of inventing checklist items.
"""

from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory

from _shared import (
    banner,
    check,
    content_to_text,
    get_model,
    run,
    section,
    show_trace,
)

from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

SKILL_PATH = "/skills/release-check/SKILL.md"
REFERENCE_PATH = "/skills/release-check/references/checks.md"
DESCRIPTION = "Use this skill when the user needs a pre-release checklist."
BODY_MARKER = "List only the items in the reference checklist"
REFERENCE_MARKER = "unit tests pass"
SECOND_CHECK = "no debug logging in the build artifact"

SKILL_TEXT = f"""---
name: release-check
description: {DESCRIPTION}
---
# Release check skill
1. Use read_file to read `{REFERENCE_PATH}`.
2. {BODY_MARKER}; do not add requirements that are not in the reference.
3. If the read fails, report the missing path and stop; do not guess items.
"""
REFERENCE_TEXT = f"""# Pre-release checklist
- {REFERENCE_MARKER}
- {SECOND_CHECK}
"""


class ObserveRequests(BaseCallbackHandler):
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        model_messages = messages[0]
        system = "\n".join(
            content_to_text(m.content) for m in model_messages if isinstance(m, SystemMessage)
        )
        context = "\n".join(content_to_text(m.content) for m in model_messages)
        tool_ids = {m.tool_call_id for m in model_messages if isinstance(m, ToolMessage)}
        self.requests.append({"system": system, "context": context, "tool_ids": tool_ids})


def read_pairs(result):
    calls: dict[str, dict] = {}
    pairs = []
    for message in result["messages"]:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                check(call["id"] not in calls, "tool call ids are unique")
                calls[call["id"]] = call
        elif isinstance(message, ToolMessage):
            check(message.tool_call_id in calls, "every tool result has a matching call")
            call = calls.pop(message.tool_call_id)
            check(message.name == call["name"], "tool result name matches the call")
            if call["name"] == "read_file":
                pairs.append((call, message))
    check(not calls, "every tool call received a result")
    return pairs


def run_agent(root: Path):
    observer = ObserveRequests()
    agent = create_deep_agent(
        model=get_model(),
        backend=FilesystemBackend(root_dir=str(root), virtual_mode=True),
        skills=["/skills/"],
        system_prompt=(
            "Complete the local skill task in Chinese. Do not browse, delegate or write files. "
            "When a skill applies, follow its instructions exactly and read only the files it names."
        ),
    )
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Use the release-check skill to list the pre-release checks. "
                        "First read the skill body, then read the reference file it names. "
                        "If the reference is missing, report the path and stop; do not guess."
                    ),
                }
            ]
        },
        config={"recursion_limit": 12, "callbacks": [observer]},
    )
    return result, observer.requests


def verify(result, requests, *, resource_exists: bool) -> None:
    pairs = read_pairs(result)
    paths = [call["args"]["file_path"] for call, _ in pairs]
    check(SKILL_PATH in paths and REFERENCE_PATH in paths, "both the skill body and its reference were read")
    skill_index, ref_index = paths.index(SKILL_PATH), paths.index(REFERENCE_PATH)
    check(skill_index < ref_index, "the skill body is read before the reference")
    skill_call, skill_reply = pairs[skill_index]
    ref_call, ref_reply = pairs[ref_index]
    check(skill_reply.status == "success", "SKILL.md was read successfully")
    check(
        all(line in content_to_text(skill_reply.content) for line in SKILL_TEXT.splitlines()),
        "the full SKILL.md text came back from the tool",
    )

    first = requests[0]
    for value in ("release-check", DESCRIPTION, SKILL_PATH):
        check(value in first["system"], f"the first request's system prompt contains `{value}`")
    check(BODY_MARKER not in first["context"], "the skill body is absent from the first request")
    check(REFERENCE_MARKER not in first["context"], "the reference is absent from the first request")

    for request in requests:
        if skill_call["id"] not in request["tool_ids"]:
            check(BODY_MARKER not in request["context"], "the body cannot appear before its tool result")
        if ref_call["id"] not in request["tool_ids"]:
            check(REFERENCE_MARKER not in request["context"], "the reference cannot appear before its tool result")

    after_skill = next(r for r in requests if skill_call["id"] in r["tool_ids"])
    check(BODY_MARKER in after_skill["context"], "the body appears in the request after the SKILL.md result")
    check(ref_call["id"] not in after_skill["tool_ids"], "the two reads happen in separate graph steps")

    after_ref = next((r for r in requests if ref_call["id"] in r["tool_ids"]), None)
    final = result["messages"][-1]
    check(isinstance(final, AIMessage) and not final.tool_calls, "the run ends with a final answer")

    if resource_exists:
        check(ref_reply.status == "success", "the reference was read successfully")
        check(
            all(line in content_to_text(ref_reply.content) for line in REFERENCE_TEXT.splitlines()),
            "the full reference came back from the tool",
        )
        check(after_ref is not None and REFERENCE_MARKER in after_ref["context"], "the reference appears after its result")
        final_text = content_to_text(final.content)
        has_unit_tests = REFERENCE_MARKER in final_text.lower() or "单元测试" in final_text
        has_no_debug = SECOND_CHECK in final_text.lower() or "调试日志" in final_text
        check(has_unit_tests and has_no_debug, "the answer uses the real checklist")
    else:
        check(ref_reply.status == "error", "a missing reference is a tool error, not a silent success")
        error_text = content_to_text(ref_reply.content)
        check(REFERENCE_PATH in error_text and "not found" in error_text.lower(), "the error names the missing path")
        final_text = content_to_text(final.content)
        check(REFERENCE_PATH in final_text, "the final answer reports the missing path")
        check(REFERENCE_MARKER not in final_text and SECOND_CHECK not in final_text, "the agent does not invent checklist items")
        check(all(REFERENCE_MARKER not in r["context"] for r in requests), "the missing content never enters context")

    print("\nrequest stages:")
    for index, request in enumerate(requests, 1):
        if REFERENCE_MARKER in request["context"]:
            stage = "catalog + skill body + reference"
        elif BODY_MARKER in request["context"]:
            stage = "catalog + skill body"
        else:
            stage = "catalog only"
        print(f"  request {index}: {stage}")


def main() -> None:
    banner("Chapter 7 - skills are disclosed in layers")
    with TemporaryDirectory(prefix="ch07-skills-") as directory:
        root = Path(directory)
        skill_file = root / SKILL_PATH.lstrip("/")
        reference_file = root / REFERENCE_PATH.lstrip("/")
        reference_file.parent.mkdir(parents=True)
        skill_file.write_text(SKILL_TEXT, encoding="utf-8")
        reference_file.write_text(REFERENCE_TEXT, encoding="utf-8")

        section("1. Reference exists")
        success, requests = run_agent(root)
        show_trace(success["messages"], max_text=280)
        verify(success, requests, resource_exists=True)
        check(any(BODY_MARKER in r["context"] for r in requests), "the body was disclosed after the first read")
        check(any(REFERENCE_MARKER in r["context"] for r in requests), "the reference was disclosed after the second read")
        catalog = [
            line for line in requests[0]["system"].splitlines() if "release-check" in line
        ]
        print("catalog snippet:\n" + "\n".join(catalog))
        print("[PASS] progressive disclosure verified for existing files")

        section("2. Reference is missing")
        reference_file.unlink()
        missing, missing_requests = run_agent(root)
        show_trace(missing["messages"], max_text=280)
        verify(missing, missing_requests, resource_exists=False)
        print("[PASS] missing reference is reported, not hallucinated")

    check(not root.exists(), "the temporary skill directory was cleaned up")
    print("\nTakeaway: the model only sees name/description/path first; file content enters")
    print("context exactly when a tool returns it. This is progressive disclosure.")
    print("All chapter-7 checks passed.")


if __name__ == "__main__":
    run(main)

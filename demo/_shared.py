"""Shared helpers for the `deepagents-in-action` chapter demos.

Every demo talks to the same OpenAI-compatible endpoint (OpenCode Go) and uses
the `deepseek-v4.1-flash` model.  Configure it with environment variables:

    OPENCODE_GO_API_KEY   required; also read from the repo-root .env
    OPENCODEGOAPIKEY      alias accepted for convenience
    OPENCODE_GO_BASE_URL  default: https://opencode.ai/inference/go/v1
    OPENCODE_GO_MODEL     default: deepseek-v4.1-flash

The endpoint requires a stable `x-opencode-session` header on every request.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import uuid
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

BASE_URL = os.getenv("OPENCODE_GO_BASE_URL", "https://opencode.ai/inference/go/v1")
MODEL = os.getenv("OPENCODE_GO_MODEL", "deepseek-v4.1-flash")
API_KEY = os.getenv("OPENCODE_GO_API_KEY") or os.getenv("OPENCODEGOAPIKEY")
if not API_KEY:
    raise RuntimeError(
        "缺少 OPENCODE_GO_API_KEY。请复制 .env.example 为仓库根目录的 .env 并填写 Key，"
        "或在进程环境中设置该变量。"
    )
SESSION_ID = os.getenv("OPENCODE_GO_SESSION") or f"deepagents-demo-{uuid.uuid4().hex[:12]}"


def get_model(**kwargs: Any):
    """Return a ChatOpenAI client wired to the OpenCode Go gateway."""
    from langchain_openai import ChatOpenAI

    headers = {
        "x-opencode-session": SESSION_ID,
        "User-Agent": "deepagents-course-demo/1.0",
    }
    headers.update(kwargs.pop("default_headers", {}) or {})
    return ChatOpenAI(
        model=MODEL,
        api_key=API_KEY,
        base_url=BASE_URL,
        temperature=kwargs.pop("temperature", 0),
        timeout=kwargs.pop("timeout", 180),
        max_retries=kwargs.pop("max_retries", 2),
        default_headers=headers,
        **kwargs,
    )


# --------------------------------------------------------------------------
# console helpers
# --------------------------------------------------------------------------
WIDTH = 78


def banner(title: str) -> None:
    print("\n" + "=" * WIDTH)
    print(title)
    print("=" * WIDTH)


def section(title: str) -> None:
    print(f"\n--- {title} " + "-" * max(0, WIDTH - len(title) - 4))


def check(condition: Any, message: str) -> None:
    """Assertion with a readable message; demos are meant to fail loudly."""
    if not condition:
        raise AssertionError(message)
    print(f"[PASS] {message}")


def content_to_text(content: Any) -> str:
    """Normalise LangChain message content (str or content blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
        return "\n".join(parts)
    return "" if content is None else str(content)


def ai_text(message: Any) -> str:
    return content_to_text(getattr(message, "content", ""))


def last_ai_text(messages: Iterable[Any]) -> str:
    for message in reversed(list(messages)):
        if getattr(message, "type", "") == "ai" or message.__class__.__name__ == "AIMessage":
            text = ai_text(message).strip()
            if text:
                return text
    return ""


def tool_calls(messages: Iterable[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in messages:
        for call in getattr(message, "tool_calls", []) or []:
            calls.append(call)
    return calls


def tool_results(messages: Iterable[Any]) -> list[Any]:
    return [m for m in messages if getattr(m, "type", "") == "tool"]


def show_trace(messages: Iterable[Any], *, max_text: int = 400) -> None:
    """Print a compact, human-readable transcript of an agent run."""
    for message in messages:
        role = getattr(message, "type", message.__class__.__name__)
        if role == "ai":
            text = ai_text(message).strip()
            calls = getattr(message, "tool_calls", []) or []
            if text:
                print(f"[assistant] {text[:max_text]}")
            for call in calls:
                print(f"[tool call] {call['name']}({json.dumps(call.get('args', {}), ensure_ascii=False)})")
        elif role == "tool":
            text = content_to_text(getattr(message, "content", ""))
            print(f"[tool result] {text[:max_text]}")
        elif role == "human":
            print(f"[user] {content_to_text(getattr(message, 'content', ''))[:max_text]}")


def show_files(files: dict[str, Any], *, max_chars: int = 300) -> None:
    for path, data in sorted(files.items()):
        content = data.get("content", "") if isinstance(data, dict) else str(data)
        print(f"  {path} ({len(content)} chars): {content[:max_chars]!r}")


def ensure_source_tree_on_path() -> None:
    """Allow `python demo/chXX.py` from the repository root."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def run(main: Any) -> None:
    """Run a demo's `main()` and turn Ctrl+C into a clean exit."""
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130)


def boxed(text: str) -> str:
    return textwrap.indent(textwrap.dedent(text).strip(), "  ")


class ScriptedChatModel(BaseChatModel):
    """A tiny deterministic chat model for error-path / structural demos.

    It returns the supplied AIMessages in order and ignores `bind_tools`; the
    agent still executes the requested tools. Use it only for mechanics, never
    to pretend that a real model produced a result.
    """

    responses: list[AIMessage]
    index: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-demo"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = self.responses[min(self.index, len(self.responses) - 1)]
        self.index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools, **kwargs):  # the agent passes its tool list; we ignore it
        return self


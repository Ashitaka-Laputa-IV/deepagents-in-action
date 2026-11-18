"""Chapter 2 - quickstart + research assistant on top of `create_deep_agent`.

Run from the repository root:

    python demo/ch02.py

The demo covers:
  1. how Python type hints/docstrings become a tool schema;
  2. a real tool-calling loop (weather);
  3. dependent tool calls (currency conversion -> calculation);
  4. tool-argument validation errors vs. exceptions inside a tool;
  5. a small research agent that plans, searches, writes a note and reports.

The search tool uses fixed sample results so the demo needs no Tavily key.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from _shared import (
    ScriptedChatModel,
    banner,
    check,
    content_to_text,
    get_model,
    last_ai_text,
    run,
    section,
    show_files,
    show_trace,
    tool_calls,
)

from deepagents import create_deep_agent
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_core.tools import tool

NOTES_PATH = "/research/langgraph.md"


def scripted_agent(call: dict[str, Any], tools: list[Any]):
    model = ScriptedChatModel(
        responses=[
            AIMessage(content="", tool_calls=[call]),
            AIMessage(content="done"),
        ]
    )
    return create_deep_agent(model=model, tools=tools)

SAMPLE_RESULTS = [
    {
        "title": "LangGraph overview",
        "url": "https://docs.langchain.com/oss/python/langgraph/overview",
        "content": "LangGraph is a low-level orchestration framework and runtime for long-running, stateful agents.",
        "score": 0.92,
    },
    {
        "title": "langchain-ai/langgraph",
        "url": "https://github.com/langchain-ai/langgraph",
        "content": "LangGraph models workflows as graphs: nodes do work, edges choose the next step, state flows between steps.",
        "score": 0.87,
    },
    {
        "title": "Deep Agents overview",
        "url": "https://docs.langchain.com/oss/python/deepagents/overview",
        "content": "Deep Agents builds on LangGraph and adds a filesystem, subagents and planning for multi-step tasks.",
        "score": 0.74,
    },
]


def get_weather(city: str) -> str:
    """Get weather for a given city."""
    return f"It's always sunny in {city}!"


def calculate(expression: str) -> float:
    """Evaluate a math expression and return the result.

    Args:
        expression: A math expression, e.g. "1 + 2 * 3".
    """
    if not re.fullmatch(r"[\d\s.+\-*/()]+", expression) or "**" in expression:
        raise ValueError(f"only numbers, parentheses and + - * / are supported: {expression!r}")
    return eval(expression)  # guarded by the regex above


def convert_currency(amount: float, from_currency: str, to_currency: str = "CNY") -> dict:
    """Convert an amount from one currency to another.

    Args:
        amount: The amount to convert.
        from_currency: The source currency code, e.g. "USD".
        to_currency: The target currency code, defaults to "CNY".
    """
    rates = {"USD": 7.2, "CNY": 1.0, "EUR": 7.8}
    cny = amount * rates[from_currency]
    return {"amount": round(cny / rates[to_currency], 2), "currency": to_currency}


search_log: list[dict[str, Any]] = []


@tool
def internet_search(
    query: str,
    max_results: int = 5,
    topic: Literal["general", "news", "finance"] = "general",
    include_raw_content: bool = False,
) -> dict:
    """Run a web search for the given query.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return.
        topic: The topic category for the search.
        include_raw_content: Whether to include raw page content.
    """
    response = {"query": query, "results": SAMPLE_RESULTS[:max_results]}
    search_log.append({"query": query, "results": len(response["results"]), "topic": topic})
    return response


def calls_of(messages, name: str) -> list[dict[str, Any]]:
    return [call for call in tool_calls(messages) if call["name"] == name]


def successful_results(messages, name: str) -> list[ToolMessage]:
    call_ids = {call["id"] for call in calls_of(messages, name)}
    return [
        message
        for message in messages
        if isinstance(message, ToolMessage)
        and message.name == name
        and message.tool_call_id in call_ids
        and message.status == "success"
    ]


def part_schema() -> None:
    section("1. Type hints + docstrings become the tool schema")
    for fn in (calculate, convert_currency):
        schema = convert_to_openai_tool(fn)["function"]
        params = schema["parameters"]
        print(f"{schema['name']}: {schema['description']}")
        print("  required:", params.get("required", []))
        for name, spec in params["properties"].items():
            default = f", default={spec['default']!r}" if "default" in spec else ""
            print(f"  - {name}: {spec['type']}{default}")
        if schema["name"] == "convert_currency":
            check(
                params["properties"]["from_currency"]["type"] == "string",
                "`from_currency: str` becomes a string property",
            )
            check(
                params["properties"]["to_currency"]["default"] == "CNY",
                "the default value is part of the schema and therefore optional",
            )
            check(
                params["required"] == ["amount", "from_currency"],
                "parameters with defaults are not required",
            )


def part_weather() -> None:
    section("2. A real tool-calling loop")
    agent = create_deep_agent(
        model=get_model(),
        tools=[get_weather],
        system_prompt=(
            "You are a helpful assistant. You MUST use the get_weather tool for every "
            "weather question; never invent weather. Then answer in one sentence."
        ),
    )
    result = agent.invoke({"messages": [{"role": "user", "content": "What is the weather in Beijing today?"}]})
    messages = result["messages"]
    show_trace(messages)

    checks = calls_of(messages, "get_weather")
    check(checks, "the model requested get_weather instead of answering from memory")
    city = str(checks[-1]["args"].get("city", "")).casefold()
    check(city in {"beijing", "北京"}, "the city argument is Beijing / 北京")
    answers = successful_results(messages, "get_weather")
    check(answers and "sunny" in content_to_text(answers[-1].content), "the tool result came back successfully")
    check(last_ai_text(messages), "the agent produced a final answer after the tool call")
    print("state keys:", sorted(result))


def part_chained_calls() -> None:
    section("3. Dependent calls: convert first, then calculate")
    agent = create_deep_agent(
        model=get_model(),
        tools=[calculate, convert_currency],
        system_prompt=(
            "You are a calculation assistant. You MUST call tools for arithmetic and currency "
            "conversion; never compute mentally. When a task depends on an earlier result, "
            "wait for that tool result before calling the next tool."
        ),
    )
    question = (
        "Convert 100 USD to CNY using convert_currency with to_currency='CNY'. "
        "After you receive the converted amount, call calculate to multiply exactly that "
        "amount by 1.08. Report both results."
    )
    result = agent.invoke({"messages": [{"role": "user", "content": question}]})
    messages = result["messages"]
    show_trace(messages)

    convert_calls = calls_of(messages, "convert_currency")
    check(convert_calls, "convert_currency was called")
    check(
        any(
            float(call["args"].get("amount", -1)) == 100
            and str(call["args"].get("from_currency", "")).upper() == "USD"
            for call in convert_calls
        ),
        "the conversion used 100 USD",
    )
    converted = successful_results(messages, "convert_currency")
    check(converted, "the conversion succeeded")
    amount = json.loads(content_to_text(converted[-1].content))["amount"]
    check(abs(amount - 720.0) < 0.01, "100 USD converted to 720 CNY with the demo rate")

    calc_calls = calls_of(messages, "calculate")
    check(calc_calls, "calculate was called after the conversion result was available")
    expression = str(calc_calls[-1]["args"].get("expression", ""))
    check("720" in expression, f"the expression reuses the converted amount (got {expression!r})")
    calc_results = successful_results(messages, "calculate")
    check(calc_results, "the calculation succeeded")
    check(abs(float(content_to_text(calc_results[-1].content)) - 777.6) < 0.01, "720 x 1.08 = 777.6")


def part_tool_errors() -> None:
    section("4. Validation errors vs. exceptions inside a tool")
    # 4a. A schema mismatch is caught by the ToolNode and returned to the model as
    # ToolMessage(status="error"); the graph can continue and the model may retry.
    bad_type = scripted_agent(
        {
            "name": "convert_currency",
            "id": "ch02-bad-type",
            "args": {"amount": "a lot", "from_currency": "USD"},
        },
        tools=[convert_currency],
    ).invoke({"messages": [{"role": "user", "content": "convert"}]})
    error_message = next(m for m in bad_type["messages"] if isinstance(m, ToolMessage))
    print("status :", error_message.status)
    print("content:", content_to_text(error_message.content)[:300])
    check(error_message.status == "error", "bad arguments become a ToolMessage with status='error'")
    check("amount" in content_to_text(error_message.content), "the error names the offending field")

    # 4b. An exception raised *inside* the function body is not a validation error;
    # it propagates and aborts invoke(). This is why tools should validate inputs.
    bad_currency = scripted_agent(
        {
            "name": "convert_currency",
            "id": "ch02-unknown-currency",
            "args": {"amount": 1, "from_currency": "JPY"},
        },
        tools=[convert_currency],
    )
    try:
        bad_currency.invoke({"messages": [{"role": "user", "content": "convert"}]})
    except KeyError as error:
        print("\nKeyError inside the function aborts invoke():", error)
        check("JPY" in str(error), "the KeyError carries the unsupported currency")
    else:
        raise AssertionError("expected rates['JPY'] to raise KeyError")


def part_research() -> None:
    section("5. Research assistant: plan, search, write notes, report")
    instructions = """You are a professional researcher.
Your job is to research a question and write a complete report.

Workflow you MUST follow:
1. Call write_todos first with three items: search, write notes, write report.
2. Call internet_search to gather information.
3. Call write_file to save your notes to /research/langgraph.md.
4. Update the todo list as work progresses; mark at least one item completed.
5. Write the final report in your last message.

Use the internet_search tool for facts. Call it at most twice, then stop searching.
Keep notes concise and include source URLs.
"""
    agent = create_deep_agent(
        model=get_model(),
        tools=[internet_search],
        system_prompt=instructions,
        middleware=[TodoListMiddleware()],
    )
    registered = sorted(agent.nodes["tools"].bound.tools_by_name)
    print("registered tools:", registered)
    check({"internet_search", "write_todos", "write_file"} <= set(registered), "planning + search + file tools are registered")

    search_log.clear()
    result = agent.invoke({"messages": [{"role": "user", "content": "What is LangGraph and how does it relate to Deep Agents?"}]})
    messages = result["messages"]
    show_trace(messages, max_text=500)

    check(search_log, "internet_search actually executed (the log is outside the model)")
    check(successful_results(messages, "internet_search"), "the search result was returned as a successful ToolMessage")
    final = last_ai_text(messages)
    check(len(final) >= 80, "the agent produced a substantive final report")
    print("\nFinal report (first 400 chars):\n", final[:400])

    todos = result.get("todos")
    files = result.get("files", {})
    section("6. State written by the research run")
    check(todos, "the research agent exercised write_todos")
    for item in todos:
        print(f"  [{item['status']}] {item['content']}")
    check(
        all(item["status"] in {"pending", "in_progress", "completed"} for item in todos),
        "every todo carries a valid status",
    )
    check(any(item["status"] == "completed" for item in todos), "the plan reflects completed work")
    check(files, "the research agent wrote state files")
    show_files(files)
    check(NOTES_PATH in files, f"the notes were written to {NOTES_PATH}")
    check("LangGraph" in files[NOTES_PATH]["content"], "the note mentions LangGraph")
    check(search_log, "the search log proves tool execution beyond the message list")


def main() -> None:
    banner("Chapter 2 - tools, planning and a research assistant")
    part_schema()
    part_weather()
    part_chained_calls()
    part_tool_errors()
    part_research()
    print("\nAll chapter-2 checks passed.")


if __name__ == "__main__":
    run(main)

"""The Ollama tool-calling loop for the free local GitHub research agent.

The agent talks to a LOCAL Ollama server's native function-calling endpoint
(``POST {ollama_url}/api/chat`` with a ``tools`` array). qwen2.5 and llama3.1
both support this. The design keeps the actual chat call **injectable** so the
whole loop is unit-testable offline with a scripted fake model — no network.

Loop shape (bounded by ``max_steps``):
    system+user  ->  model may emit tool_calls  ->  we execute each tool  ->
    feed the results back as role="tool" messages  ->  repeat until the model
    answers in prose or we hit the step cap.

The system prompt instructs the model to *first search GitHub, then read the top
findings, then give the user concrete guidance* — i.e. it researches what it
finds and instructs the user.

If the model never makes a usable tool call (weak/older model, malformed calls),
we DEGRADE gracefully to a single ``search_repos(question)`` and summarize the
results deterministically, so the user always gets something useful.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from . import tools as agent_tools

# A "chat" is any callable (messages, tool_schemas) -> response dict shaped like
# Ollama's /api/chat reply: {"message": {"content": str, "tool_calls": [...]}}.
ChatFn = Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]]

MAX_STEPS_DEFAULT = 6
CHAT_TIMEOUT = 180.0
TAGS_TIMEOUT = 1.5
RESULT_SUMMARY_CAP = 400

SYSTEM_PROMPT = (
    "You are GHPulse's GitHub research assistant. You help a non-technical user "
    "discover and evaluate open-source projects. You have tools to search GitHub, "
    "inspect a repository, read its README, and fetch a web page.\n"
    "Work in this order: (1) call search_repos to find candidates; (2) read the "
    "most promising ones with get_repo and fetch_readme (and fetch_url for a "
    "homepage/docs if useful); (3) then write your final answer.\n"
    "Your final answer must be plain, friendly prose (no JSON): name the best "
    "1-3 projects, say in one line what each does and why it stands out (stars, "
    "activity, momentum), and end with a concrete next step for the user. Only "
    "use facts returned by the tools; never invent repositories or numbers."
)


@dataclass
class AgentStep:
    """One executed tool call in the loop."""

    tool: str
    args: dict[str, Any]
    result_summary: str


@dataclass
class AgentResult:
    """The agent's final answer plus the trace of tool calls it made."""

    answer: str
    steps: list[AgentStep] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "steps": [
                {
                    "tool": s.tool,
                    "args": s.args,
                    "result_summary": s.result_summary,
                }
                for s in self.steps
            ],
        }


# --------------------------------------------------------------------------- #
# real Ollama chat (default, injectable)
# --------------------------------------------------------------------------- #


def ollama_available(base_url: str) -> bool:
    """Fast liveness probe: GET /api/tags with a short timeout."""
    try:
        resp = httpx.get(
            f"{base_url.rstrip('/')}/api/tags", timeout=TAGS_TIMEOUT
        )
        return resp.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def make_ollama_chat(base_url: str, model: str) -> ChatFn:
    """Build the default chat callable that hits a real Ollama server."""
    url = f"{base_url.rstrip('/')}/api/chat"

    def _chat(
        messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]]
    ) -> dict[str, Any]:
        resp = httpx.post(
            url,
            json={
                "model": model,
                "messages": messages,
                "tools": tool_schemas,
                "stream": False,
            },
            timeout=CHAT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    return _chat


# --------------------------------------------------------------------------- #
# parsing helpers (tolerant of the small format differences between models)
# --------------------------------------------------------------------------- #


def _extract_tool_calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a normalized list of {'name':..,'arguments':dict} from a message."""
    raw = message.get("tool_calls") or []
    calls: list[dict[str, Any]] = []
    for tc in raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or tc
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except (json.JSONDecodeError, ValueError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": str(name), "arguments": args})
    return calls


def _summarize_result(result: str) -> str:
    result = result or ""
    if len(result) <= RESULT_SUMMARY_CAP:
        return result
    return result[:RESULT_SUMMARY_CAP].rstrip() + " …"


def _deterministic_summary(question: str, search_json: str) -> str:
    """Turn a raw search_repos result into readable prose (fallback path)."""
    try:
        rows = json.loads(search_json)
    except (json.JSONDecodeError, ValueError):
        rows = None
    if not isinstance(rows, list) or not rows:
        return (
            "I couldn't find clear matches on GitHub for "
            f"“{question}”. Try rephrasing with more specific terms "
            "(a language, a topic, or the exact tool name)."
        )
    lines = [
        f"Here are the top GitHub matches for “{question}”:",
        "",
    ]
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        name = row.get("full_name") or "?"
        stars = row.get("stars")
        lang = row.get("language") or "—"
        desc = (row.get("description") or "").strip() or "(no description)"
        star_txt = f"{stars:,}★" if isinstance(stars, int) else "?★"
        lines.append(f"- {name} ({star_txt}, {lang}): {desc}")
    lines.append("")
    lines.append(
        "Next step: open the top result on GitHub and skim its README to confirm "
        "it fits your use case."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #


def run_loop(
    question: str,
    client: Any,
    chat: ChatFn,
    *,
    system: str = SYSTEM_PROMPT,
    max_steps: int = MAX_STEPS_DEFAULT,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> AgentResult:
    """Drive the tool-calling conversation to a final answer.

    ``chat`` is injected so this runs offline in tests. Any exception from the
    chat callable is caught and turned into the deterministic fallback so the
    caller always receives a usable :class:`AgentResult`.
    """
    schemas = tool_schemas if tool_schemas is not None else agent_tools.TOOL_SCHEMAS
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    steps: list[AgentStep] = []
    answer = ""

    for _ in range(max(1, max_steps)):
        try:
            response = chat(messages, schemas)
        except Exception as exc:  # noqa: BLE001 - degrade instead of raising
            return _fallback(question, client, steps, note=str(exc))

        message = (response or {}).get("message") or {}
        # Record the assistant turn so tool results attach to the right call.
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content", "") or "",
                "tool_calls": message.get("tool_calls") or [],
            }
        )

        calls = _extract_tool_calls(message)
        if calls:
            for call in calls:
                name, args = call["name"], call["arguments"]
                result = agent_tools.execute_tool(name, args, client)
                steps.append(
                    AgentStep(
                        tool=name,
                        args=args,
                        result_summary=_summarize_result(result),
                    )
                )
                messages.append(
                    {"role": "tool", "name": name, "content": result}
                )
            continue  # let the model react to the tool output

        # No tool calls -> the model is answering (or giving up).
        answer = (message.get("content") or "").strip()
        break

    if not answer:
        # Hit the step cap without prose, or the model produced nothing usable.
        return _fallback(question, client, steps)
    return AgentResult(answer=answer, steps=steps)


def _fallback(
    question: str, client: Any, steps: list[AgentStep], note: str | None = None
) -> AgentResult:
    """Weak-model degrade path: one search + a deterministic summary."""
    search_json = agent_tools.execute_tool(
        "search_repos", {"query": question}, client
    )
    steps.append(
        AgentStep(
            tool="search_repos",
            args={"query": question},
            result_summary=_summarize_result(search_json),
        )
    )
    answer = _deterministic_summary(question, search_json)
    if note:
        answer += (
            "\n\n(The local model was unavailable, so this is a direct GitHub "
            "search summary.)"
        )
    return AgentResult(answer=answer, steps=steps)

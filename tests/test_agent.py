"""Offline tests for the free local agentic GitHub search (B2).

No network, no real Ollama, no real GitHub. The chat call is injected with a
scripted fake model and the GitHubClient is stubbed, so the tool-calling loop is
exercised end-to-end deterministically.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ghpulse import agent
from ghpulse.agent import base
from ghpulse.agent import tools as agent_tools


# --------------------------------------------------------------------------- #
# stubs
# --------------------------------------------------------------------------- #


class FakeClient:
    """Minimal stand-in for ghpulse.http.GitHubClient (no network)."""

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.gets: list[str] = []

    def search_repositories(self, q, sort="stars", order="desc", per_page=100,
                            max_pages=3):
        self.searches.append(q)
        return [
            {
                "full_name": "chroma-core/chroma",
                "html_url": "https://github.com/chroma-core/chroma",
                "stargazers_count": 15000,
                "forks_count": 1200,
                "language": "Python",
                "topics": ["vector-database", "embeddings"],
                "description": "the AI-native open-source embedding database",
                "pushed_at": "2026-08-01T00:00:00Z",
                "open_issues_count": 200,
                "archived": False,
            }
        ]

    def get(self, path, params=None):
        self.gets.append(path)
        if path.endswith("/readme"):
            import base64

            return {
                "content": base64.b64encode(b"# Chroma\nA vector DB.").decode(),
                "encoding": "base64",
            }
        return {
            "full_name": "chroma-core/chroma",
            "html_url": "https://github.com/chroma-core/chroma",
            "stargazers_count": 15000,
            "language": "Python",
            "description": "the AI-native open-source embedding database",
        }

    def close(self) -> None:  # pragma: no cover - trivial
        pass


def _scripted_chat(responses):
    """Return a chat callable that yields queued responses; records calls."""
    calls = {"messages": []}
    queue = list(responses)

    def _chat(messages, tool_schemas):
        calls["messages"].append([dict(m) for m in messages])
        if queue:
            return queue.pop(0)
        return {"message": {"content": "done", "tool_calls": []}}

    _chat.calls = calls  # type: ignore[attr-defined]
    return _chat


def _tool_call(name, arguments):
    return {"message": {"content": "", "tool_calls": [
        {"function": {"name": name, "arguments": arguments}}
    ]}}


def _final(text):
    return {"message": {"content": text, "tool_calls": []}}


# --------------------------------------------------------------------------- #
# run_loop: executes a tool, feeds the result back, then answers
# --------------------------------------------------------------------------- #


def test_run_loop_executes_tool_and_returns_result() -> None:
    client = FakeClient()
    chat = _scripted_chat([
        _tool_call("search_repos", {"query": "vector database", "limit": 3}),
        _final("Chroma is the best pick — 15k stars. Try `pip install chromadb`."),
    ])

    result = agent.run_loop("find a vector db", client, chat)

    # It executed the search tool exactly once.
    assert client.searches == ["vector database"]
    # The result carries a step trace with the tool + args + a summary.
    assert len(result.steps) == 1
    step = result.steps[0]
    assert step.tool == "search_repos"
    assert step.args == {"query": "vector database", "limit": 3}
    assert "chroma-core/chroma" in step.result_summary
    # Final prose answer surfaced.
    assert "Chroma" in result.answer

    # The tool result was fed back to the model on the SECOND chat call.
    second_call_messages = chat.calls["messages"][1]
    roles = [m["role"] for m in second_call_messages]
    assert "tool" in roles
    tool_msg = next(m for m in second_call_messages if m["role"] == "tool")
    assert "chroma-core/chroma" in tool_msg["content"]


def test_run_loop_multi_tool_research_then_answer() -> None:
    client = FakeClient()
    chat = _scripted_chat([
        _tool_call("search_repos", {"query": "vector db"}),
        _tool_call("fetch_readme", {"full_name": "chroma-core/chroma"}),
        _final("Chroma looks great."),
    ])

    result = agent.run_loop("research vector dbs", client, chat)

    tool_names = [s.tool for s in result.steps]
    assert tool_names == ["search_repos", "fetch_readme"]
    assert client.gets == ["/repos/chroma-core/chroma/readme"]
    assert result.answer == "Chroma looks great."


def test_run_loop_parses_string_arguments() -> None:
    """Some models emit tool arguments as a JSON string, not an object."""
    client = FakeClient()
    chat = _scripted_chat([
        _tool_call("search_repos", json.dumps({"query": "rust game engine"})),
        _final("Try Bevy."),
    ])
    result = agent.run_loop("q", client, chat)
    assert client.searches == ["rust game engine"]
    assert result.steps[0].args == {"query": "rust game engine"}


# --------------------------------------------------------------------------- #
# weak-model fallback: no usable tool call / no prose -> deterministic search
# --------------------------------------------------------------------------- #


def test_run_loop_fallback_on_empty_answer() -> None:
    client = FakeClient()
    # Model answers nothing and never calls a tool.
    chat = _scripted_chat([_final("")])
    result = agent.run_loop("fast vector database", client, chat)
    # Degraded to a direct search_repos + deterministic summary.
    assert client.searches == ["fast vector database"]
    assert result.steps[-1].tool == "search_repos"
    assert "chroma-core/chroma" in result.answer


def test_run_loop_fallback_on_chat_exception() -> None:
    client = FakeClient()

    def boom(messages, tool_schemas):
        raise RuntimeError("ollama died")

    result = agent.run_loop("q", client, boom)
    assert client.searches == ["q"]
    assert "search summary" in result.answer.lower()


def test_run_loop_respects_step_cap() -> None:
    client = FakeClient()
    # Model loops forever asking for tools; cap must stop it and fall back.
    chat = _scripted_chat([_tool_call("search_repos", {"query": "x"})] * 20)
    result = agent.run_loop("q", client, chat, max_steps=3)
    # 3 model turns each ran the tool, then the fallback ran one more search.
    assert len(result.steps) == 4
    assert result.answer  # non-empty deterministic summary


# --------------------------------------------------------------------------- #
# tools are read-only and exception-guarded (never raise)
# --------------------------------------------------------------------------- #


def test_tools_never_raise_and_return_error_strings() -> None:
    class Broken:
        def search_repositories(self, *a, **k):
            raise RuntimeError("network down")

        def get(self, *a, **k):
            raise RuntimeError("network down")

    broken = Broken()
    assert agent_tools.search_repos(broken, "q").startswith("error:")
    assert agent_tools.get_repo(broken, "a/b").startswith("error:")
    assert agent_tools.fetch_readme(broken, "a/b").startswith("error:")
    # Bad inputs are guarded too.
    assert agent_tools.search_repos(broken, "").startswith("error:")
    assert agent_tools.get_repo(broken, "not-a-full-name").startswith("error:")
    assert agent_tools.fetch_url(broken, "ftp://nope").startswith("error:")


def test_execute_tool_unknown_and_bad_args() -> None:
    client = FakeClient()
    assert agent_tools.execute_tool("nope", {}, client).startswith("error:")
    # Extra kwargs the model hallucinated -> guarded, not a crash.
    out = agent_tools.execute_tool("search_repos", {"bogus": 1}, client)
    assert out.startswith("error:")


def test_search_repos_returns_compact_json() -> None:
    client = FakeClient()
    out = agent_tools.search_repos(client, "vector db", limit=1)
    rows = json.loads(out)
    assert rows[0]["full_name"] == "chroma-core/chroma"
    assert rows[0]["stars"] == 15000
    assert "html_url" not in rows[0]  # compacted to 'url'
    assert rows[0]["url"].startswith("https://")


# --------------------------------------------------------------------------- #
# schemas are in the OpenAI/Ollama tool format
# --------------------------------------------------------------------------- #


def test_tool_schemas_shape() -> None:
    names = {s["function"]["name"] for s in agent_tools.TOOL_SCHEMAS}
    assert {"search_repos", "get_repo", "fetch_readme", "fetch_url"} <= names
    for schema in agent_tools.TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        assert "description" in fn
        assert fn["parameters"]["type"] == "object"


# --------------------------------------------------------------------------- #
# run_agent: injected chat + stubbed client, and the Ollama-down help path
# --------------------------------------------------------------------------- #


def _settings(**over):
    base_settings = {
        "token": None,
        "ollama_url": "http://localhost:11434",
        "agent_model": "qwen2.5",
    }
    base_settings.update(over)
    return SimpleNamespace(**base_settings)


def test_run_agent_with_injected_chat(monkeypatch) -> None:
    fake = FakeClient()
    # Stub the GitHubClient the package constructs so no network is touched.
    monkeypatch.setattr(agent, "GitHubClient", lambda token, conn=None: fake)

    chat = _scripted_chat([
        _tool_call("search_repos", {"query": "vector db"}),
        _final("Chroma is a solid choice."),
    ])
    result = agent.run_agent("find a vector db", _settings(), chat=chat)
    assert isinstance(result, agent.AgentResult)
    assert "Chroma" in result.answer
    assert result.steps[0].tool == "search_repos"
    assert fake.searches == ["vector db"]


def test_run_agent_ollama_unreachable_returns_help(monkeypatch) -> None:
    # No injected chat -> real path; force the liveness probe to fail.
    monkeypatch.setattr(agent.base, "ollama_available", lambda url: False)
    result = agent.run_agent("anything", _settings())
    assert result.steps == []
    assert "Ollama" in result.answer
    assert "ollama pull qwen2.5" in result.answer


def test_run_agent_empty_question() -> None:
    result = agent.run_agent("   ", _settings())
    assert result.steps == []
    assert result.answer

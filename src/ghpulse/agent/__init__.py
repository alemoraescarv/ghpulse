"""Free, local, agentic GitHub search for GHPulse.

``run_agent(question, settings, conn=None, chat=None)`` runs a local Ollama
tool-calling loop that searches GitHub, reads what it finds, and advises the
user. It is FREE (no API key) and runs entirely on the user's machine.

Backends / model options
-------------------------
- Default model: **qwen2.5** via Ollama (great free tool-calling, ~4.7GB).
  Pull it with ``ollama pull qwen2.5``.
- Alternative: **llama3.1** via Ollama (``ollama pull llama3.1``) — also
  supports native function calling.
- Any Hugging Face GGUF: Ollama can run models straight from the Hub, e.g.
  ``ollama run hf.co/<user>/<repo>:<quant>``; point ``GHPULSE_AGENT_MODEL`` /
  ``settings.agent_model`` at that name.
- Hosted alternatives (not required, not free): Hugging Face Inference
  Providers or the ``smolagents`` library can run the same tool loop against a
  hosted model if a user prefers not to run anything locally.

Everything is exception-guarded: if Ollama isn't running, ``run_agent`` returns
an :class:`~ghpulse.agent.base.AgentResult` that explains how to enable it —
it never raises.
"""

from __future__ import annotations

from typing import Any

from ..http import GitHubClient
from . import base
from .base import (
    AgentResult,
    AgentStep,
    ChatFn,
    run_loop,
)
from . import tools

__all__ = [
    "AgentResult",
    "AgentStep",
    "run_agent",
    "run_loop",
    "tools",
]

DEFAULT_AGENT_MODEL = "qwen2.5"
DEFAULT_OLLAMA_URL = "http://localhost:11434"


def _ollama_help(model: str, base_url: str) -> str:
    return (
        "The free local agent needs Ollama running.\n\n"
        f"1. Install Ollama: https://ollama.com/download\n"
        f"2. Pull the model:  ollama pull {model}\n"
        "3. Make sure it's running (the menu-bar app, or `ollama serve`).\n\n"
        f"GHPulse looked for Ollama at {base_url}. Once it's up, ask again — or "
        "run `ghpulse panel` and use the “Ask the agent” box.\n\n"
        "Prefer not to run anything locally? You can switch the backend to "
        "Claude (paid, needs ANTHROPIC_API_KEY) in the control panel."
    )


def run_agent(
    question: str,
    settings: Any,
    conn: Any | None = None,
    chat: ChatFn | None = None,
) -> AgentResult:
    """Answer *question* with the local agentic GitHub researcher.

    Parameters
    ----------
    question:
        The user's free-text question, e.g. "find me a fast local vector DB".
    settings:
        A ``Settings``-like object. Reads ``token`` (optional — public search
        works without one), ``ollama_url``, and ``agent_model`` (default
        "qwen2.5"). All via ``getattr`` so this module never depends on config
        internals it doesn't own.
    conn:
        Optional sqlite connection passed to the GitHubClient for ETag caching.
    chat:
        Optional injected chat callable ``(messages, tools) -> response`` for
        offline testing. When omitted, a real Ollama chat is used (and its
        availability probed first).
    """
    q = (question or "").strip()
    if not q:
        return AgentResult(
            answer="Ask me something, e.g. “find a fast local vector database”.",
            steps=[],
        )

    base_url = (getattr(settings, "ollama_url", None) or DEFAULT_OLLAMA_URL).rstrip("/")
    model = getattr(settings, "agent_model", None) or DEFAULT_AGENT_MODEL

    # Only probe/require Ollama when we're using the real chat. An injected chat
    # (tests, or a hosted backend) bypasses the local-server requirement.
    if chat is None:
        if not base.ollama_available(base_url):
            return AgentResult(answer=_ollama_help(model, base_url), steps=[])
        chat = base.make_ollama_chat(base_url, model)

    token = getattr(settings, "token", None)
    client = GitHubClient(token=token, conn=conn)
    try:
        return run_loop(q, client, chat)
    except Exception as exc:  # noqa: BLE001 - never propagate to the caller
        return AgentResult(
            answer=(
                "The agent hit an unexpected error and stopped: "
                f"{exc}. Please try again."
            ),
            steps=[],
        )
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass

"""Anthropic Claude backend for the explainer.

Uses the official ``anthropic`` Python SDK, imported lazily inside the methods so
this module imports fine even when the package isn't installed (it's an OPTIONAL
`llm` extra — the Ollama path and the demo work without it). All errors are
swallowed and turned into "" so the summarizer can never break a run.
"""

from __future__ import annotations

# Default model: Claude Sonnet 5 (exact model string "claude-sonnet-5") — fast,
# 1M context, strong quality for the news explainer + "what it does" blurbs.
# Override per run with GHPULSE_CLAUDE_MODEL (e.g. "claude-opus-4-8").
MODEL = "claude-sonnet-5"
MAX_TOKENS = 1500


class AnthropicBackend:
    """Summarize via Claude. ``available()`` requires ANTHROPIC_API_KEY."""

    name = MODEL

    def __init__(self, api_key: str | None = None, model: str = MODEL) -> None:
        self._api_key = api_key
        self.name = model
        self._model = model

    def available(self) -> bool:
        """True only when an API key is resolvable (env ANTHROPIC_API_KEY present).

        Does not import the SDK — a missing key means we never try, and a missing
        package is handled lazily at summarize() time.
        """
        return bool(self._api_key)

    def summarize(self, system: str, prompt: str) -> str:
        if not self._api_key:
            return ""
        try:
            import anthropic  # lazy: optional dependency
        except Exception:
            return ""
        try:
            client = anthropic.Anthropic(api_key=self._api_key)
            message = client.messages.create(
                model=self._model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            # A safety refusal yields no usable text.
            if getattr(message, "stop_reason", None) == "refusal":
                return ""
            for block in message.content:
                if getattr(block, "type", None) == "text":
                    return (getattr(block, "text", "") or "").strip()
            return ""
        except Exception:
            # Never crash the run on an API/network/parse error.
            return ""

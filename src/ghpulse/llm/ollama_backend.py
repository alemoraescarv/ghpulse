"""Local, free Ollama backend for the explainer.

Talks to a LOCAL Ollama server over HTTP via httpx (already a dependency). No
API key, no cost. Default base URL http://localhost:11434 and default model
"llama3.1:8b" (both overridable via config). Note: Ollama can also run GGUF
models pulled straight from Hugging Face (e.g. `ollama run hf.co/<repo>:<tag>`),
so GHPULSE_OLLAMA_MODEL may point at one of those too.

All errors are swallowed → "" so the summarizer never breaks a run.
"""

from __future__ import annotations

import httpx

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"
TAGS_TIMEOUT = 1.5  # fast liveness probe
CHAT_TIMEOUT = 120.0


class OllamaBackend:
    """Summarize via a local Ollama server."""

    def __init__(self, base_url: str = DEFAULT_URL, model: str = DEFAULT_MODEL) -> None:
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.model = model or DEFAULT_MODEL
        self.name = f"{self.model} (local)"

    def available(self) -> bool:
        """Fast GET /api/tags with a short timeout; True if the server responds."""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=TAGS_TIMEOUT)
            return resp.status_code == 200
        except Exception:
            return False

    def summarize(self, system: str, prompt: str) -> str:
        try:
            resp = httpx.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                },
                timeout=CHAT_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return (data.get("message", {}).get("content", "") or "").strip()
        except Exception:
            return ""

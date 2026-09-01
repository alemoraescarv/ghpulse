"""Pluggable LLM "what happened" summarizer: Claude or a local Ollama model.

Selection is driven by config GHPULSE_LLM:
  - "off"       -> no backend (default); available() is never consulted.
  - "ollama"    -> local Ollama server (free, no key).
  - "anthropic" -> Claude via the official SDK (needs ANTHROPIC_API_KEY).
  - "auto"      -> try Ollama first (available()), then Anthropic.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import db
from .anthropic_backend import AnthropicBackend
from .base import SYSTEM_PROMPT, LLMBackend, build_digest
from .ollama_backend import OllamaBackend

__all__ = [
    "LLMBackend",
    "AnthropicBackend",
    "OllamaBackend",
    "build_digest",
    "select_backend",
    "summarize_edition",
    "refine_descriptions",
    "refine_news_groups",
    "answer_over_edition",
    "SYSTEM_PROMPT",
]


def select_backend(settings: Any) -> LLMBackend | None:
    """Return the configured backend instance, or None when disabled.

    Constructing a backend is network-free — no ``available()`` call happens for
    the explicit "ollama"/"anthropic" choices, and none at all for "off". Only
    "auto" probes ``available()`` to pick between them.
    """
    mode = (getattr(settings, "llm", "off") or "off").strip().lower()
    if mode == "off":
        return None
    if mode == "ollama":
        return OllamaBackend(
            base_url=getattr(settings, "ollama_url", OllamaBackend().base_url),
            model=getattr(settings, "ollama_model", OllamaBackend().model),
        )
    if mode == "anthropic":
        return AnthropicBackend(
            api_key=getattr(settings, "anthropic_key", None),
            model=getattr(settings, "claude_model", None) or AnthropicBackend.name,
        )
    if mode == "auto":
        ollama = OllamaBackend(
            base_url=getattr(settings, "ollama_url", OllamaBackend().base_url),
            model=getattr(settings, "ollama_model", OllamaBackend().model),
        )
        if ollama.available():
            return ollama
        anthropic = AnthropicBackend(
            api_key=getattr(settings, "anthropic_key", None),
            model=getattr(settings, "claude_model", None) or AnthropicBackend.name,
        )
        if anthropic.available():
            return anthropic
        return None
    return None


def _build_sections(conn: sqlite3.Connection, edition: str) -> dict[str, Any]:
    """Recompute the sections dict (GitHub + hype) for an edition — no network."""
    from .. import hype, score  # local import to keep this package light

    sections = score.compute_metrics(conn, edition)
    return hype.merge_hype_sections(conn, edition, sections)


def summarize_edition(
    conn: sqlite3.Connection, edition: str, settings: Any
) -> str | None:
    """Build the digest, call the selected backend, store + return the summary.

    Returns None (and stores nothing) when no backend is configured/available or
    the backend yields no text. Never raises for backend failures.
    """
    backend = select_backend(settings)
    if backend is None or not backend.available():
        return None
    digest = build_digest(_build_sections(conn, edition))
    text = backend.summarize(SYSTEM_PROMPT, digest)
    if not text:
        return None
    db.upsert_summary(conn, edition, text, backend.name)
    db.commit(conn)
    return text


# Re-export the focused-description refiner (imported at the bottom so
# select_backend is already defined when refine.py resolves it).
from .refine import refine_descriptions, refine_news_groups  # noqa: E402

# Grounded Q&A over the current edition's shown repos (imported at the bottom so
# select_backend is already defined when askedition.py resolves it).
from .askedition import answer_over_edition  # noqa: E402

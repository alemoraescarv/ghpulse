"""Localhost control panel for ghpulse (stdlib http.server, no new deps).

Exposes :func:`serve` to run a small glass-UI control panel bound to
127.0.0.1. The panel lets a non-technical user pick the LLM backend
(Claude / local-free-agentic / off), trigger a best-effort data refresh,
ask the free local agent a question, and open the latest rendered news page.
"""

from __future__ import annotations

from .app import build_server, serve

__all__ = ["serve", "build_server"]

"""Settings, paths, and watched-list constants for ghpulse."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WATCHED_LANGUAGES: list[str] = [
    "python",
    "typescript",
    "rust",
    "go",
    "javascript",
    "c++",
    "zig",
    "swift",
]

WATCHED_TOPICS: list[str] = [
    "llm",
    "ai-agents",
    "rust",
    "kubernetes",
    "wasm",
    "mcp",
    "rag",
    "robotics",
]

MIN_STARS_FOR_PCT: int = 100
MIN_STARS_FOR_BREAKOUT: int = 50
# Star bands for NEW-this-week repos (young repos rarely have thousands of
# stars yet). Established movers are covered by discover.ACTIVE_STAR_BANDS.
STAR_RANGE_SHARDS: list[str] = ["10..50", "50..200", ">200"]


@dataclass
class Settings:
    home: Path
    db_path: Path
    site_dir: Path
    # config_dir/token default so a Settings can be built with just the paths
    # needed for offline rendering; get_settings() always sets them explicitly.
    config_dir: Path = Path.home() / ".config" / "ghpulse"
    token: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    # P3: LLM "what happened" summarizer (pluggable backends).
    llm: str = "off"  # off | ollama | anthropic | auto
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    anthropic_key: str | None = None
    claude_model: str = "claude-sonnet-5"
    # P4: free local agentic GitHub search (Ollama tool-calling) + control panel.
    agent_model: str = "qwen2.5"
    panel_port: int = 8765


def load_env_value(name: str) -> str | None:
    """Return an env value from the process env, else ~/.config/ghpulse/env.

    Accepts plain ``KEY=value`` and ``export KEY=value`` lines, ignoring blanks
    and ``#`` comments and stripping surrounding quotes.
    """
    value = os.environ.get(name)
    if value:
        return value.strip()
    env_file = Path.home() / ".config" / "ghpulse" / "env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if line.startswith(f"{name}="):
                    parsed = line.split("=", 1)[1].strip().strip("'\"")
                    if parsed:
                        return parsed
        except OSError:
            return None
    return None


def load_token() -> str | None:
    """Return a GitHub token from env GITHUB_TOKEN, else ~/.config/ghpulse/env."""
    return load_env_value("GITHUB_TOKEN")


def get_settings() -> Settings:
    """Build Settings, creating the home and site directories if needed."""
    home = Path(os.environ.get("GHPULSE_HOME", str(Path.home() / "ghpulse"))).expanduser()
    site_dir = home / "site"
    config_dir = Path.home() / ".config" / "ghpulse"
    home.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)
    llm = (load_env_value("GHPULSE_LLM") or "off").strip().lower()
    if llm not in ("off", "ollama", "anthropic", "auto"):
        llm = "off"
    try:
        panel_port = int(load_env_value("GHPULSE_PANEL_PORT") or 8765)
    except (TypeError, ValueError):
        panel_port = 8765
    return Settings(
        home=home,
        db_path=home / "ghpulse.db",
        site_dir=site_dir,
        config_dir=config_dir,
        token=load_token(),
        reddit_client_id=load_env_value("REDDIT_CLIENT_ID"),
        reddit_client_secret=load_env_value("REDDIT_CLIENT_SECRET"),
        llm=llm,
        ollama_url=load_env_value("GHPULSE_OLLAMA_URL") or "http://localhost:11434",
        ollama_model=load_env_value("GHPULSE_OLLAMA_MODEL") or "llama3.1:8b",
        anthropic_key=load_env_value("ANTHROPIC_API_KEY"),
        claude_model=load_env_value("GHPULSE_CLAUDE_MODEL") or "claude-sonnet-5",
        agent_model=load_env_value("GHPULSE_AGENT_MODEL") or "qwen2.5",
        panel_port=panel_port,
    )

"""Offline, fast tests for the stdlib control panel (no network jobs)."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ghpulse import config
from ghpulse.panel import app as panel_app


def _make_settings(tmp_path: Path) -> config.Settings:
    """A hermetic Settings pointing entirely at tmp_path (no real $HOME writes)."""
    (tmp_path / "site").mkdir(parents=True, exist_ok=True)
    return config.Settings(
        home=tmp_path,
        db_path=tmp_path / "ghpulse.db",
        site_dir=tmp_path / "site",
        config_dir=tmp_path / "config",
        token=None,
        llm="off",
        ollama_url="http://127.0.0.1:11434",
        ollama_model="qwen2.5",
    )


@pytest.fixture()
def server(tmp_path: Path):
    settings = _make_settings(tmp_path)
    srv, state = panel_app.build_server(settings, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        yield base, settings, state
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 - localhost
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(url: str, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 - localhost
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # non-2xx still carries a JSON body
        return exc.code, json.loads(exc.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# write_env_value                                                             #
# --------------------------------------------------------------------------- #
def test_write_env_value_upserts_single_key(tmp_path: Path):
    cfg = tmp_path / "cfg"
    panel_app.write_env_value(cfg, "GHPULSE_LLM", "ollama")
    env = cfg / "env"
    assert env.read_text().strip() == "GHPULSE_LLM=ollama"
    # Upsert must replace, not duplicate.
    panel_app.write_env_value(cfg, "GHPULSE_LLM", "anthropic")
    lines = [l for l in env.read_text().splitlines() if l.strip()]
    assert lines == ["GHPULSE_LLM=anthropic"]


def test_write_env_value_preserves_other_keys(tmp_path: Path):
    cfg = tmp_path / "cfg"
    env = cfg / "env"
    cfg.mkdir()
    env.write_text("# comment\nGITHUB_TOKEN=abc\nexport GHPULSE_LLM=off\n")
    panel_app.write_env_value(cfg, "GHPULSE_LLM", "ollama")
    text = env.read_text()
    assert "GITHUB_TOKEN=abc" in text
    assert "# comment" in text
    assert "GHPULSE_LLM=ollama" in text
    assert text.count("GHPULSE_LLM=") == 1


# --------------------------------------------------------------------------- #
# /api/status                                                                 #
# --------------------------------------------------------------------------- #
def test_status_shape(server):
    base, _settings, _state = server
    status, payload = _get(base + "/api/status")
    assert status == 200
    for key in (
        "backend",
        "ollama_available",
        "latest_edition",
        "busy",
        "panel_port",
    ):
        assert key in payload
    assert payload["backend"] == "off"
    assert payload["busy"] is False
    assert payload["latest_edition"] is None
    assert isinstance(payload["ollama_available"], bool)


def test_status_sends_cors_headers(server):
    """The static news page calls /api/* cross-origin; CORS must be permissive."""
    base, _settings, _state = server
    with urllib.request.urlopen(base + "/api/status", timeout=3) as resp:  # noqa: S310
        headers = resp.headers
    assert headers.get("Access-Control-Allow-Origin") == "*"
    assert "POST" in (headers.get("Access-Control-Allow-Methods") or "")
    assert "Content-Type" in (headers.get("Access-Control-Allow-Headers") or "")


def test_options_preflight_returns_cors(server):
    """A CORS preflight OPTIONS returns 204 with the permissive CORS headers."""
    base, _settings, _state = server
    req = urllib.request.Request(base + "/api/ask_edition", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
        code = resp.status
        headers = resp.headers
    assert code == 204
    assert headers.get("Access-Control-Allow-Origin") == "*"
    assert "OPTIONS" in (headers.get("Access-Control-Allow-Methods") or "")


def test_index_serves_glass_html(server):
    base, _settings, _state = server
    with urllib.request.urlopen(base + "/", timeout=3) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
    assert "control panel" in body
    assert "backdrop-filter" in body  # glass tokens present, self-contained


# --------------------------------------------------------------------------- #
# /api/config persistence                                                     #
# --------------------------------------------------------------------------- #
def test_config_persists_and_reflects(server):
    base, settings, _state = server
    status, payload = _post(base + "/api/config", {"backend": "ollama"})
    assert status == 200
    assert payload["backend"] == "ollama"
    env = Path(settings.config_dir) / "env"
    assert env.is_file()
    assert "GHPULSE_LLM=ollama" in env.read_text()

    # Switching again upserts (no duplicate lines).
    status, payload = _post(base + "/api/config", {"backend": "anthropic"})
    assert status == 200
    assert payload["backend"] == "anthropic"
    text = env.read_text()
    assert "GHPULSE_LLM=anthropic" in text
    assert text.count("GHPULSE_LLM=") == 1


def test_config_rejects_invalid_backend(server):
    base, _settings, _state = server
    status, payload = _post(base + "/api/config", {"backend": "gpt5"})
    assert status == 400
    assert "error" in payload


# --------------------------------------------------------------------------- #
# /api/ask/result before any job                                              #
# --------------------------------------------------------------------------- #
def test_ask_result_not_ready_initially(server):
    base, _settings, _state = server
    status, payload = _get(base + "/api/ask/result")
    assert status == 200
    assert payload["ready"] is False


def test_empty_question_is_rejected(server):
    base, _settings, _state = server
    status, payload = _post(base + "/api/ask", {"question": "   "})
    assert status == 400
    assert payload["started"] is False


def test_ask_edition_result_not_ready_initially(server):
    base, _settings, _state = server
    status, payload = _get(base + "/api/ask_edition/result")
    assert status == 200
    assert payload["ready"] is False


def test_ask_edition_empty_question_is_rejected(server):
    base, _settings, _state = server
    status, payload = _post(base + "/api/ask_edition", {"question": "   "})
    assert status == 400
    assert payload["started"] is False


def test_index_has_ask_this_week_toggle(server):
    base, _settings, _state = server
    with urllib.request.urlopen(base + "/", timeout=3) as resp:  # noqa: S310
        body = resp.read().decode("utf-8")
    assert "Ask this week" in body
    assert "/api/ask_edition" in body


def _seed_tracked(settings) -> str:
    """Seed the demo cohort into the settings DB so /api/search has data."""
    from ghpulse import db, demo, score

    conn = db.connect(settings.db_path)
    db.init_db(conn)
    edition = demo.seed_demo(conn, settings, edition="2026-01-15")
    score.compute_metrics(conn, edition)
    conn.close()
    return edition


# --------------------------------------------------------------------------- #
# /api/search (tracked cohort, offline, synchronous)                          #
# --------------------------------------------------------------------------- #
def test_search_tracked_returns_json(server):
    base, settings, _state = server
    _seed_tracked(settings)
    status, payload = _get(base + "/api/search?q=llm&limit=5")
    assert status == 200
    assert payload["source"] == "tracked"
    assert payload["query"] == "llm"
    assert isinstance(payload["repos"], list)
    assert payload["total"] >= len(payload["repos"])
    if payload["repos"]:
        repo = payload["repos"][0]
        for key in ("full_name", "url", "stars", "tags", "tracked"):
            assert key in repo
        assert repo["tracked"] is True


def test_search_tracked_empty_query(server):
    base, _settings, _state = server
    status, payload = _get(base + "/api/search?q=")
    assert status == 200
    assert payload["repos"] == []
    assert payload["total"] == 0


# --------------------------------------------------------------------------- #
# /api/search_github (live) — friendly error with no token                    #
# --------------------------------------------------------------------------- #
def test_search_github_no_token_is_friendly(server):
    base, _settings, _state = server  # settings.token is None in the fixture
    status, payload = _get(base + "/api/search_github?q=llm&page=1")
    assert status == 200
    assert payload["source"] == "github"
    assert payload["repos"] == []
    assert "error" in payload
    assert "token" in payload["error"].lower()
    # Must not crash the panel — a subsequent status call still works.
    status2, _ = _get(base + "/api/status")
    assert status2 == 200


def test_news_missing_edition_is_not_found(server):
    base, _settings, _state = server
    req = urllib.request.Request(base + "/news", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            code = resp.status
        body = ""
    except urllib.error.HTTPError as exc:
        code = exc.code
        body = exc.read().decode("utf-8")
    assert code == 404
    assert "edition" in body.lower()


# --------------------------------------------------------------------------- #
# Serializer                                                                   #
# --------------------------------------------------------------------------- #
def test_serialize_agent_result_from_object():
    class Step:
        def __init__(self):
            self.tool = "search_repos"
            self.args = {"query": "rust"}
            self.result_summary = "3 repos"

    class Result:
        answer = "Try repo X first."
        steps = [Step()]

    out = panel_app._serialize_agent_result(Result(), "find rust")
    assert out["question"] == "find rust"
    assert out["answer"] == "Try repo X first."
    assert out["steps"][0]["tool"] == "search_repos"
    assert out["steps"][0]["result_summary"] == "3 repos"


def test_serialize_agent_result_from_dict():
    result = {"answer": "hi", "steps": [{"tool": "get_repo", "result_summary": "ok"}]}
    out = panel_app._serialize_agent_result(result, "q")
    assert out["answer"] == "hi"
    assert out["steps"][0]["tool"] == "get_repo"

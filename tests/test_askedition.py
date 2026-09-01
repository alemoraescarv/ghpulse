"""Offline tests for the grounded "Ask about this week" Q&A layer.

No network, no real LLM: the chat call is injected as a scripted fake, and the
no-backend path uses llm='off' so select_backend returns None.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ghpulse import db, demo
from ghpulse.llm import askedition


def _settings(**overrides):
    base = {
        "llm": "off",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.1:8b",
        "anthropic_key": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _seed(tmp_path: Path):
    conn = db.connect(tmp_path / "ask.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    return conn, edition


# ---------------------------------------------------------------------------
# build_context — compact, citable, includes blurbs/tags/metrics
# ---------------------------------------------------------------------------


def test_build_context_includes_blurbs_tags_and_metrics(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)
    context, names = askedition.build_context(conn, edition, limit=60)
    assert context.strip()
    assert names
    # A known demo repo is present and numbered.
    assert "ollama/ollama" in context
    assert "ollama/ollama" in names
    # Metrics + section membership are rendered for grounding/citation.
    assert "★" in context
    assert "section:" in context
    # Tags (labels in brackets) appear for at least one line.
    assert "[" in context and "]" in context
    # Canned demo blurb (focused "what it does") is used, not just raw desc.
    assert "Run large language models locally with one command" in context
    conn.close()


def test_build_context_respects_limit(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)
    _context, names = askedition.build_context(conn, edition, limit=3)
    assert len(names) <= 3
    conn.close()


# ---------------------------------------------------------------------------
# answer_over_edition — injected fake naming a repo -> grounded + cited
# ---------------------------------------------------------------------------


def test_answer_grounded_cites_named_repo(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)
    seen = {}

    def fake_chat(system: str, prompt: str) -> str:
        seen["system"] = system
        seen["prompt"] = prompt
        return "I'd recommend ollama/ollama — it runs models locally."

    out = askedition.answer_over_edition(
        conn, edition, "which repo runs models locally?", _settings(), chat=fake_chat
    )
    assert out["grounded"] is True
    assert "ollama/ollama" in out["answer"]
    assert "ollama/ollama" in out["cited"]
    # The prompt fed to the model carried the question and the repo list.
    assert "which repo runs models locally?" in seen["prompt"]
    assert "ollama/ollama" in seen["prompt"]
    assert "ONLY the provided list" in seen["system"]
    conn.close()


def test_answer_short_name_is_cited(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)

    def fake_chat(system: str, prompt: str) -> str:
        # Names the short repo name only (owner prefix dropped).
        return "Try ollama for running models locally."

    out = askedition.answer_over_edition(
        conn, edition, "run models locally?", _settings(), chat=fake_chat
    )
    assert out["grounded"] is True
    assert "ollama/ollama" in out["cited"]
    conn.close()


def test_answer_no_match_still_grounded_no_cites(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)

    def fake_chat(system: str, prompt: str) -> str:
        return "Nothing in this week's list fits that question."

    out = askedition.answer_over_edition(
        conn, edition, "quantum teleportation library?", _settings(), chat=fake_chat
    )
    assert out["grounded"] is True
    assert out["cited"] == []
    conn.close()


# ---------------------------------------------------------------------------
# no backend -> friendly note, grounded False (never hangs / no network)
# ---------------------------------------------------------------------------


def test_no_backend_returns_friendly_note(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)
    out = askedition.answer_over_edition(
        conn, edition, "which repo runs models locally?", _settings(llm="off")
    )
    assert out["grounded"] is False
    assert out["cited"] == []
    assert "Ollama" in out["answer"] or "Claude" in out["answer"]
    conn.close()


# ---------------------------------------------------------------------------
# robustness — never raises on odd input
# ---------------------------------------------------------------------------


def test_empty_question_is_friendly(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)
    out = askedition.answer_over_edition(conn, edition, "   ", _settings())
    assert out["grounded"] is False
    assert out["cited"] == []
    assert isinstance(out["answer"], str) and out["answer"]
    conn.close()


def test_none_question_does_not_raise(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)
    out = askedition.answer_over_edition(conn, edition, None, _settings())  # type: ignore[arg-type]
    assert out["grounded"] is False
    assert isinstance(out["answer"], str)
    conn.close()


def test_chat_that_raises_is_handled(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)

    def boom(system: str, prompt: str) -> str:
        raise RuntimeError("model exploded")

    out = askedition.answer_over_edition(
        conn, edition, "anything?", _settings(), chat=boom
    )
    # Chat failure degrades to a friendly grounded answer, never raises.
    assert isinstance(out["answer"], str) and out["answer"]
    assert out["cited"] == []
    conn.close()


def test_chat_empty_output_is_friendly(tmp_path: Path) -> None:
    conn, edition = _seed(tmp_path)

    def blank(system: str, prompt: str) -> str:
        return "   "

    out = askedition.answer_over_edition(
        conn, edition, "anything?", _settings(), chat=blank
    )
    assert isinstance(out["answer"], str) and out["answer"]
    assert out["cited"] == []
    conn.close()


def test_odd_edition_label_does_not_raise(tmp_path: Path) -> None:
    conn, _edition = _seed(tmp_path)

    def fake_chat(system: str, prompt: str) -> str:
        return "ollama/ollama runs models locally."

    # An arbitrary edition label still resolves against the tracked cohort; the
    # point of this test is that it never raises and returns a usable answer.
    out = askedition.answer_over_edition(
        conn, "1999-01-01", "anything?", _settings(), chat=fake_chat
    )
    assert isinstance(out["answer"], str) and out["answer"]
    conn.close()

"""Offline tests for the P3 LLM explainer layer (no network, no real LLM)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ghpulse import db, demo, render
from ghpulse import llm
from ghpulse.llm import base


# ---------------------------------------------------------------------------
# build_digest — pure, includes section content + monthly deltas
# ---------------------------------------------------------------------------


def _sample_sections() -> dict:
    return {
        "edition": "2026-08-25",
        "cohort_size": 3,
        "days_of_history": 30,
        "generated_at": "2026-08-25T07:00:00+00:00",
        "sections": [
            {
                "key": "biggest_gains",
                "title": "Biggest gains",
                "subtitle": "Most stars added this week",
                "items": [
                    {
                        "rank": 1,
                        "full_name": "astral-sh/uv",
                        "url": "https://github.com/astral-sh/uv",
                        "language": "rust",
                        "stars": 41000,
                        "value_label": "+1,200 stars",
                        "deltas": {"week": "+3% wk", "month": "+18% mo"},
                        "hype_badges": [{"label": "HN", "count": 5, "platform": "hn"}],
                    },
                    {
                        "rank": 2,
                        "full_name": "ollama/ollama",
                        "url": "https://github.com/ollama/ollama",
                        "language": "go",
                        "stars": 90000,
                        "value_label": "+800 stars",
                        "deltas": {"week": "+1% wk"},
                    },
                ],
            }
        ],
    }


def test_build_digest_is_pure_and_deterministic() -> None:
    sections = _sample_sections()
    a = base.build_digest(sections)
    b = base.build_digest(sections)
    assert a == b  # deterministic
    # Input dict is not mutated.
    assert sections == _sample_sections()


def test_build_digest_includes_content_and_monthly_deltas() -> None:
    digest = base.build_digest(_sample_sections())
    assert "Biggest gains" in digest
    assert "astral-sh/uv" in digest
    assert "ollama/ollama" in digest
    # Monthly delta surfaced for the repo that has one.
    assert "+18% mo" in digest
    # Buzz badge surfaced.
    assert "HN 5" in digest
    # Edition/cohort header line.
    assert "2026-08-25" in digest


# ---------------------------------------------------------------------------
# select_backend — honors GHPULSE_LLM without touching the network
# ---------------------------------------------------------------------------


def _settings(**overrides):
    base_settings = {
        "llm": "off",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.1:8b",
        "anthropic_key": None,
    }
    base_settings.update(overrides)
    return SimpleNamespace(**base_settings)


def test_select_backend_off_returns_none_without_probing(monkeypatch) -> None:
    # available() must never be called for "off".
    def boom(self) -> bool:  # pragma: no cover - must not run
        raise AssertionError("available() must not be called for GHPULSE_LLM=off")

    monkeypatch.setattr(llm.OllamaBackend, "available", boom)
    monkeypatch.setattr(llm.AnthropicBackend, "available", boom)
    assert llm.select_backend(_settings(llm="off")) is None


def test_select_backend_explicit_choices_do_not_probe(monkeypatch) -> None:
    # Constructing an explicit backend is network-free: available() not called.
    def boom(self) -> bool:  # pragma: no cover - must not run
        raise AssertionError("available() must not be called by select_backend")

    monkeypatch.setattr(llm.OllamaBackend, "available", boom)
    monkeypatch.setattr(llm.AnthropicBackend, "available", boom)

    ollama = llm.select_backend(_settings(llm="ollama"))
    assert isinstance(ollama, llm.OllamaBackend)
    assert ollama.model == "llama3.1:8b"

    anthropic = llm.select_backend(_settings(llm="anthropic", anthropic_key="sk-x"))
    assert isinstance(anthropic, llm.AnthropicBackend)


def test_anthropic_available_is_key_only_no_network() -> None:
    assert llm.AnthropicBackend(api_key=None).available() is False
    assert llm.AnthropicBackend(api_key="sk-test").available() is True


def test_summarize_edition_off_is_noop(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "s.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    # seed_demo seeds a canned row; clear it so we test the off path cleanly.
    conn.execute("DELETE FROM summary")
    conn.commit()
    assert llm.summarize_edition(conn, edition, _settings(llm="off")) is None
    assert db.get_summary(conn, edition) is None
    conn.close()


# ---------------------------------------------------------------------------
# summary table insert/get
# ---------------------------------------------------------------------------


def test_summary_table_insert_get(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "sum.db")
    db.init_db(conn)
    assert db.get_summary(conn, "2026-08-25") is None
    db.upsert_summary(conn, "2026-08-25", "hello world", "demo (offline)")
    conn.commit()
    row = db.get_summary(conn, "2026-08-25")
    assert row is not None
    assert row["text"] == "hello world"
    assert row["model"] == "demo (offline)"
    assert row["generated_at"]
    # Replace on same edition.
    db.upsert_summary(conn, "2026-08-25", "updated", "llama3.1:8b (local)")
    conn.commit()
    row2 = db.get_summary(conn, "2026-08-25")
    assert row2["text"] == "updated"
    assert row2["model"] == "llama3.1:8b (local)"
    count = conn.execute("SELECT COUNT(*) FROM summary").fetchone()[0]
    assert count == 1
    conn.close()


# ---------------------------------------------------------------------------
# render: explainer card present with a summary row, omitted without one
# ---------------------------------------------------------------------------


def test_explainer_card_removed_and_monthly_chip_present(tmp_path: Path) -> None:
    # The "This week in tech, explained" card was removed on request; the top
    # "Ask about this week" chat bar now covers that role. It must never render.
    conn = db.connect(tmp_path / "r.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    settings = SimpleNamespace(site_dir=tmp_path / "site")

    out = render.render_edition(conn, edition, settings)
    html = out.read_text(encoding="utf-8")
    assert "This week in tech, explained" not in html
    # Monthly delta chip still renders on cards.
    assert "% mo" in html
    conn.close()

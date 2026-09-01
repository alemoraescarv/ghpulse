"""Offline tests for the P4 focused-description (blurb) refine layer.

No network, no real LLM: the chat call is injected as a scripted fake.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ghpulse import db, demo, render
from ghpulse.llm import refine


def _settings(**overrides):
    base_settings = {
        "llm": "off",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.1:8b",
        "anthropic_key": None,
    }
    base_settings.update(overrides)
    return SimpleNamespace(**base_settings)


# ---------------------------------------------------------------------------
# desc_hash — deterministic, normalization-stable
# ---------------------------------------------------------------------------


def test_desc_hash_is_deterministic() -> None:
    a = db.desc_hash("Run large language models locally.")
    b = db.desc_hash("Run large language models locally.")
    assert a == b
    assert isinstance(a, str) and a


def test_desc_hash_normalizes_whitespace_and_case() -> None:
    assert db.desc_hash("  Run   LLMs\tlocally ") == db.desc_hash("run llms locally")
    assert db.desc_hash(None) == db.desc_hash("")
    assert db.desc_hash("a") != db.desc_hash("b")


# ---------------------------------------------------------------------------
# get_blurb / upsert_blurb round-trip
# ---------------------------------------------------------------------------


def test_blurb_table_round_trip(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "b.db")
    db.init_db(conn)
    key = db.desc_hash("some description")
    assert db.get_blurb(conn, "acme/thing", key) is None
    db.upsert_blurb(conn, "acme/thing", key, "Do the thing fast", "demo (offline)")
    conn.commit()
    assert db.get_blurb(conn, "acme/thing", key) == "Do the thing fast"
    # Replace on same key.
    db.upsert_blurb(conn, "acme/thing", key, "Do it faster", "demo (offline)")
    conn.commit()
    assert db.get_blurb(conn, "acme/thing", key) == "Do it faster"
    count = conn.execute("SELECT COUNT(*) FROM repo_blurb").fetchone()[0]
    assert count == 1
    conn.close()


# ---------------------------------------------------------------------------
# refine_descriptions: good line is stored and rendered on cards
# ---------------------------------------------------------------------------


def test_refine_stores_good_blurb_and_render_shows_it(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "r.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    # Wipe the canned demo blurbs so we exercise the refine path cleanly.
    conn.execute("DELETE FROM repo_blurb")
    conn.commit()

    good = "Reduce context in long agentic sessions"

    def fake_chat(system: str, prompt: str) -> str:
        assert "focused description" in system.lower() or "punchy" in system.lower()
        return good

    written = refine.refine_descriptions(
        conn, edition, _settings(), chat=fake_chat, model="fake"
    )
    assert written > 0
    # Every shown repo now has the same fake blurb stored.
    row = conn.execute(
        "SELECT full_name, description FROM repo LIMIT 1"
    ).fetchone()
    # Render and confirm the blurb text is on the page (not the raw description).
    settings = SimpleNamespace(site_dir=tmp_path / "site")
    out = render.render_edition(conn, edition, settings)
    html = out.read_text(encoding="utf-8")
    assert good in html


def test_refine_is_cached_second_call_writes_zero(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "c.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    conn.execute("DELETE FROM repo_blurb")
    conn.commit()

    calls = {"n": 0}

    def fake_chat(system: str, prompt: str) -> str:
        calls["n"] += 1
        return "Do a concrete useful thing"

    first = refine.refine_descriptions(conn, edition, _settings(), chat=fake_chat)
    assert first > 0
    after_first = calls["n"]
    # Second pass: all cache hits, no new writes, no new chat calls.
    second = refine.refine_descriptions(conn, edition, _settings(), chat=fake_chat)
    assert second == 0
    assert calls["n"] == after_first


# ---------------------------------------------------------------------------
# refine_descriptions: bad output (ramble / empty) falls back to hardened desc
# ---------------------------------------------------------------------------


def test_refine_ramble_falls_back_to_hardened_description(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "ram.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    conn.execute("DELETE FROM repo_blurb")
    conn.commit()

    ramble = " ".join(["word"] * 30)  # 30 words, way over the ceiling

    def fake_chat(system: str, prompt: str) -> str:
        return ramble

    written = refine.refine_descriptions(conn, edition, _settings(), chat=fake_chat)
    assert written > 0
    # Pick a known repo and check the stored blurb equals the hardened description.
    row = conn.execute(
        "SELECT full_name, description FROM repo WHERE full_name = ?",
        ("ollama/ollama",),
    ).fetchone()
    stored = db.get_blurb(conn, row["full_name"], db.desc_hash(row["description"]))
    assert stored == render.harden_description(row["description"])
    assert "word word word" not in stored
    conn.close()


def test_refine_empty_output_falls_back(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "empty.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    conn.execute("DELETE FROM repo_blurb")
    conn.commit()

    def fake_chat(system: str, prompt: str) -> str:
        return "   "

    written = refine.refine_descriptions(conn, edition, _settings(), chat=fake_chat)
    assert written > 0
    row = conn.execute(
        "SELECT full_name, description FROM repo WHERE full_name = ?",
        ("astral-sh/uv",),
    ).fetchone()
    stored = db.get_blurb(conn, row["full_name"], db.desc_hash(row["description"]))
    assert stored == render.harden_description(row["description"])
    conn.close()


def test_refine_refusal_falls_back(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "ref.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    conn.execute("DELETE FROM repo_blurb")
    conn.commit()

    def fake_chat(system: str, prompt: str) -> str:
        return "I'm sorry, I cannot help with that request."

    written = refine.refine_descriptions(conn, edition, _settings(), chat=fake_chat)
    assert written > 0
    row = conn.execute(
        "SELECT full_name, description FROM repo WHERE full_name = ?",
        ("ollama/ollama",),
    ).fetchone()
    stored = db.get_blurb(conn, row["full_name"], db.desc_hash(row["description"]))
    assert stored == render.harden_description(row["description"])
    assert "sorry" not in stored.lower()
    conn.close()


# ---------------------------------------------------------------------------
# no backend -> no-op (GitHub descriptions keep showing)
# ---------------------------------------------------------------------------


def test_refine_no_backend_is_noop(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "noop.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    conn.execute("DELETE FROM repo_blurb")
    conn.commit()
    # llm=off -> select_backend returns None -> refine writes nothing.
    written = refine.refine_descriptions(conn, edition, _settings(llm="off"))
    assert written == 0
    assert conn.execute("SELECT COUNT(*) FROM repo_blurb").fetchone()[0] == 0
    conn.close()


# ---------------------------------------------------------------------------
# render prefers a stored blurb over the hardened GitHub description
# ---------------------------------------------------------------------------


def test_render_uses_blurb_when_present(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "seed.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)  # seeds canned blurbs too
    settings = SimpleNamespace(site_dir=tmp_path / "site")
    out = render.render_edition(conn, edition, settings)
    html = out.read_text(encoding="utf-8")
    # A couple of the canned demo blurbs must appear on the page.
    assert "Run large language models locally with one command" in html
    assert "Install and manage Python projects blazingly fast" in html
    # And the raw GitHub description they replace must NOT appear.
    assert "Get up and running with large language models locally." not in html
    conn.close()

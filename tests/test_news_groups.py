"""Offline tests for the grouped-thematic News view.

Pure and deterministic: build_news_groups is exercised with small synthetic
sections, and refine_news_groups runs with an injected scripted chat (no network,
no real LLM). The demo end-to-end path confirms real deterministic paragraphs.
"""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

from ghpulse import db, demo, render
from ghpulse.llm import refine


def _section(key, title, items):
    return {"key": key, "title": title, "subtitle": "", "items": items}


def _item(full_name, *, tags, week=None, description=None, stars=100, language=None):
    item = {
        "full_name": full_name,
        "url": f"https://github.com/{full_name}",
        "description": description,
        "language": language,
        "stars": stars,
        "tags": list(tags),
        "tag_meta": [],
    }
    if week is not None:
        item["deltas"] = {"week": f"{week:+.0f}% wk"}
    return item


def _settings(**overrides):
    base = {
        "llm": "off",
        "ollama_url": "http://localhost:11434",
        "ollama_model": "llama3.1:8b",
        "anthropic_key": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# build_news_groups — grouping, ordering, paragraphs
# ---------------------------------------------------------------------------


def test_groups_by_primary_tag_and_orders_by_count():
    sections = [
        _section("a", "A", [
            _item("acme/crewpilot", tags=["ai-agents"], week=40,
                  description="Coordinate cooperative AI agents with typed contracts"),
            _item("acme/agentlens", tags=["ai-agents"], week=30,
                  description="Trace and replay multi-agent LLM runs step by step"),
            _item("acme/mcp-kit", tags=["ai-agents"], week=20,
                  description="Build Model Context Protocol servers with batteries included"),
            _item("infra/vllm-lite", tags=["llm-infra"], week=15,
                  description="Serve quantized models fast on one GPU"),
            _item("infra/ggml-run", tags=["llm-infra"], week=10,
                  description="Run LLM inference on CPU with tiny dependencies"),
        ]),
    ]
    groups = render.build_news_groups(sections)
    # Two real groups; ai-agents (3) before llm-infra (2) by count desc.
    assert [g["tag_id"] for g in groups] == ["ai-agents", "llm-infra"]
    ai = groups[0]
    assert ai["count"] == 3
    assert ai["label"] == "AI Agents"
    assert ai["headline"] == "🤖 AI Agents — 3 trending this week"
    assert ai["data_tags"] == "ai-agents"
    # Repos ordered by weekly % desc.
    assert [r["full_name"] for r in ai["repos"]] == [
        "acme/crewpilot", "acme/agentlens", "acme/mcp-kit"
    ]


def test_paragraph_names_top_repos_and_is_grounded():
    sections = [
        _section("a", "A", [
            _item("acme/crewpilot", tags=["ai-agents"], week=40,
                  description="Coordinate cooperative AI agents with typed contracts"),
            _item("acme/agentlens", tags=["ai-agents"], week=30,
                  description="Trace and replay multi-agent LLM runs"),
        ]),
    ]
    para = render.build_news_groups(sections)[0]["paragraph"]
    # Names the repos actually in the group (short names), states the count,
    # and closes with the category signal about rising interest.
    assert "crewpilot" in para
    assert "agentlens" in para
    assert "2" in para
    assert "agent harnesses and tooling" in para
    # Grounded: never names a repo not in the group.
    assert "mcp-kit" not in para


def test_paragraph_wording_varies_by_count_bucket():
    def build(n):
        items = [
            _item(f"acme/agent{i}", tags=["ai-agents"], week=50 - i,
                  description=f"Do agent thing number {i}")
            for i in range(n)
        ]
        return render.build_news_groups([_section("a", "A", items)])[0]["paragraph"]

    small = build(2)
    strong = build(5)
    assert "Steady interest" in small
    assert "A strong week" in strong
    assert small != strong


def test_singletons_go_to_also_trending():
    sections = [
        _section("a", "A", [
            _item("acme/crewpilot", tags=["ai-agents"], week=40, description="Run agents"),
            _item("acme/agentlens", tags=["ai-agents"], week=30, description="Trace agents"),
            _item("solo/paint", tags=["ui-ux"], week=25, description="Draw pretty UI"),
            _item("solo/query", tags=["databases"], week=12, description="Query things fast"),
        ]),
    ]
    groups = render.build_news_groups(sections)
    also = groups[-1]
    assert also["tag_id"] == "_also"
    assert also["label"] == "Also trending"
    assert also["count"] == 2
    names = {r["full_name"] for r in also["repos"]}
    assert names == {"solo/paint", "solo/query"}
    # data-tags unions the singletons' primary tags so the filter reveals it.
    assert "ui-ux" in also["data_tags"]
    assert "databases" in also["data_tags"]


def test_stored_llm_blurb_is_preferred():
    sections = [
        _section("a", "A", [
            _item("acme/crewpilot", tags=["ai-agents"], week=40, description="Run agents"),
            _item("acme/agentlens", tags=["ai-agents"], week=30, description="Trace agents"),
        ]),
    ]
    llm_para = "An LLM-written trend paragraph about agents."
    groups = render.build_news_groups(sections, group_blurbs={"ai-agents": llm_para})
    assert groups[0]["paragraph"] == llm_para


def test_empty_safe_and_pure():
    assert render.build_news_groups([]) == []
    assert render.build_news_groups({}) == []
    assert render.build_news_groups({"sections": []}) == []
    # A lone repo with no siblings -> only the "Also trending" group.
    sections = [_section("a", "A", [_item("x/y", tags=["ai-agents"], week=5, description="do y")])]
    groups = render.build_news_groups(sections)
    assert len(groups) == 1 and groups[0]["tag_id"] == "_also"
    # Purity: input not mutated, repeatable output.
    snap = copy.deepcopy(sections)
    assert render.build_news_groups(sections) == render.build_news_groups(sections)
    assert sections == snap


def test_accepts_full_metrics_dict_and_bare_list():
    section_list = [_section("a", "A", [
        _item("acme/a", tags=["ai-agents"], week=5, description="do a"),
        _item("acme/b", tags=["ai-agents"], week=4, description="do b"),
    ])]
    from_list = render.build_news_groups(section_list)
    from_dict = render.build_news_groups({"sections": section_list})
    assert from_list == from_dict


# ---------------------------------------------------------------------------
# End-to-end from demo data (deterministic, offline)
# ---------------------------------------------------------------------------


def test_demo_yields_populated_groups(tmp_path: Path):
    from ghpulse import hype, score
    from ghpulse.config import Settings

    conn = db.connect(tmp_path / "g.db")
    db.init_db(conn)
    settings = Settings(home=tmp_path, db_path=tmp_path / "g.db", site_dir=tmp_path / "site")
    edition = demo.seed_demo(conn, settings, edition="2026-01-15")

    sections = score.compute_metrics(conn, edition)
    sections = hype.merge_hype_sections(conn, edition, sections)
    # Mirror render_edition: apply focused blurbs before grouping.
    for section in sections["sections"]:
        for item in section.get("items") or []:
            from ghpulse import tags as tg
            item["tags"] = tg.classify(item)
            blurb = db.get_blurb(conn, item["full_name"], db.desc_hash(item.get("description")))
            item["description"] = blurb or render.harden_description(item.get("description"))

    groups = render.build_news_groups(sections["sections"])
    real = [g for g in groups if g["tag_id"] != "_also"]
    assert len(real) >= 2, "demo should produce multiple multi-repo groups"
    for g in real:
        assert g["count"] >= 2
        assert g["paragraph"]
        # The paragraph names its top repo's short name.
        top_short = g["repos"][0]["full_name"].split("/")[-1]
        assert top_short in g["paragraph"]
    conn.close()


def test_render_edition_shows_group_cards(tmp_path: Path):
    conn = db.connect(tmp_path / "r.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    settings = SimpleNamespace(site_dir=tmp_path / "site")
    out = render.render_edition(conn, edition, settings)
    html = out.read_text(encoding="utf-8")
    assert "trending this week" in html
    assert "news-group" in html
    assert 'id="view-news"' in html
    conn.close()


# ---------------------------------------------------------------------------
# refine_news_groups — injected fake chat stores + render prefers it
# ---------------------------------------------------------------------------


def test_refine_stores_group_blurb_and_render_prefers_it(tmp_path: Path):
    conn = db.connect(tmp_path / "ref.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)

    good = "Agent frameworks led the week as several tooling projects climbed together."

    def fake_chat(system: str, prompt: str) -> str:
        assert "trend" in system.lower()
        return good

    written = refine.refine_news_groups(conn, edition, _settings(), chat=fake_chat, model="fake")
    assert written > 0
    # A group_blurb row now exists and render puts the LLM text on the page.
    stored = conn.execute("SELECT COUNT(*) FROM group_blurb").fetchone()[0]
    assert stored == written
    settings = SimpleNamespace(site_dir=tmp_path / "site")
    out = render.render_edition(conn, edition, settings)
    html = out.read_text(encoding="utf-8")
    assert good in html
    conn.close()


def test_refine_bad_output_falls_back_to_deterministic(tmp_path: Path):
    conn = db.connect(tmp_path / "bad.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)

    ramble = " ".join(["word"] * 80)  # way over the 55-word ceiling

    def fake_chat(system: str, prompt: str) -> str:
        return ramble

    written = refine.refine_news_groups(conn, edition, _settings(), chat=fake_chat)
    assert written == 0
    assert conn.execute("SELECT COUNT(*) FROM group_blurb").fetchone()[0] == 0
    # Render still shows a deterministic paragraph (never the ramble).
    settings = SimpleNamespace(site_dir=tmp_path / "site")
    out = render.render_edition(conn, edition, settings)
    html = out.read_text(encoding="utf-8")
    assert "word word word" not in html
    assert "trending this week" in html
    conn.close()


def test_refine_no_backend_is_noop(tmp_path: Path):
    conn = db.connect(tmp_path / "noop.db")
    db.init_db(conn)
    edition = demo.seed_demo(conn, None)
    written = refine.refine_news_groups(conn, edition, _settings(llm="off"))
    assert written == 0
    assert conn.execute("SELECT COUNT(*) FROM group_blurb").fetchone()[0] == 0
    conn.close()

"""Offline tests for the deterministic topical tag classifier + render wiring.

Pure, no network, no LLM. Covers ghpulse.tags.classify on hand-built fixtures
and asserts render.render_edition exposes tag_bar and that both the Repos cards
and the News items carry tags (identical per repo).
"""

from __future__ import annotations

from ghpulse import render, tags


# ---------------------------------------------------------------------------
# classify — determinism + fixtures
# ---------------------------------------------------------------------------


def test_classify_is_deterministic():
    repo = {
        "full_name": "shadcn-ui/ui",
        "language": "TypeScript",
        "topics": ["ui", "components", "design-system", "tailwind", "radix"],
        "description": "Beautifully designed components built with Radix UI and Tailwind CSS.",
    }
    first = tags.classify(repo)
    second = tags.classify(repo)
    assert first == second


def test_ui_repo_tags_ui_ux():
    repo = {
        "full_name": "shadcn-ui/ui",
        "language": "TypeScript",
        "topics": ["ui", "components", "design-system", "tailwind", "radix"],
        "description": "Beautifully designed components built with Radix UI and Tailwind CSS.",
    }
    assert tags.classify(repo) == ["ui-ux"]


def test_vllm_like_tags_llm_infra_only():
    repo = {
        "full_name": "vllm-project/vllm",
        "language": "Python",
        "topics": ["llm", "inference", "cuda"],
        "description": "A high-throughput and memory-efficient inference engine for LLMs.",
    }
    assert tags.classify(repo) == ["llm-infra"]


def test_langchain_like_includes_context_rag_and_ai_agents():
    repo = {
        "full_name": "langchain-ai/langchain",
        "language": "Python",
        "topics": ["rag", "agents", "llm"],
        "description": (
            "Build context-aware reasoning applications: a retrieval-augmented "
            "framework for orchestrating agents."
        ),
    }
    out = tags.classify(repo)
    assert "context-rag" in out
    assert "ai-agents" in out
    assert 1 <= len(out) <= 3


def test_no_signal_falls_back_to_general():
    repo = {
        "full_name": "someone/plain-thing",
        "language": None,
        "topics": [],
        "description": "A perfectly ordinary repository with nothing notable to say.",
    }
    assert tags.classify(repo) == ["general"]


def test_language_alone_never_tags():
    # Python with no topical topics/keywords must not tag data-ml or ai-agents.
    repo = {
        "full_name": "plain/python-repo",
        "language": "Python",
        "topics": [],
        "description": "A small helper library.",
    }
    assert tags.classify(repo) == ["general"]


def test_returns_one_to_three_tags():
    repo = {
        "full_name": "mega/stack",
        "language": "Python",
        "topics": ["llm", "rag", "agents", "vector-search", "inference", "cuda"],
        "description": (
            "Agents with retrieval-augmented memory and gpu inference for large "
            "language models."
        ),
    }
    out = tags.classify(repo)
    assert 1 <= len(out) <= 3


def test_general_never_alongside_another_tag():
    repo = {
        "full_name": "x/y",
        "language": "Go",
        "topics": ["kubernetes", "docker"],
        "description": "Container orchestration tooling for kubernetes clusters.",
    }
    out = tags.classify(repo)
    assert "general" not in out


def test_tag_meta_shape():
    meta = tags.tag_meta(["ai-agents", "general"])
    assert meta[0] == ("ai-agents", "AI Agents", "🤖")
    assert meta[1] == ("general", "General", "📦")


def test_classify_does_not_mutate_input():
    repo = {
        "full_name": "a/b",
        "language": "Python",
        "topics": ["llm"],
        "description": "language model serving",
    }
    import copy

    snap = copy.deepcopy(repo)
    tags.classify(repo)
    assert repo == snap


# ---------------------------------------------------------------------------
# render wiring — tag_bar + items carry tags + news mirrors card tags
# ---------------------------------------------------------------------------


def _demo_sections(tmp_path):
    from ghpulse import db, demo, hype, score
    from ghpulse.config import Settings

    conn = db.connect(tmp_path / "tags.db")
    db.init_db(conn)
    settings = Settings(
        home=tmp_path,
        db_path=tmp_path / "tags.db",
        site_dir=tmp_path / "site",
    )
    edition = demo.seed_demo(conn, settings, edition="2026-01-15")
    sections = score.compute_metrics(conn, edition)
    sections = hype.merge_hype_sections(conn, edition, sections)
    return conn, settings, edition, sections


def test_build_news_items_carry_tags(tmp_path):
    _, _, _, sections = _demo_sections(tmp_path)
    news = render.build_news_items(sections)
    assert news, "demo should yield news items"
    for n in news:
        assert n.get("tags"), f"news item {n['full_name']} missing tags"
        assert isinstance(n["tags"], list)
        # tag_meta mirrors the ids
        assert [m[0] for m in n["tag_meta"]] == n["tags"]


def test_render_exposes_tag_bar_and_cards_and_news_share_tags(tmp_path):
    conn, settings, edition, sections = _demo_sections(tmp_path)

    out = render.render_edition(conn, edition, settings, sections=sections)
    html = out.read_text(encoding="utf-8")

    # The rendered page carries the filter bar, data-tags and the filter JS.
    assert 'class="tag-bar' in html
    assert 'data-tag="all"' in html
    assert "data-tags=" in html
    assert "ghpulse-tag" in html
    assert "selectTag" in html
    assert "metric-pill" in html

    # Repos cards and the grouped News trend cards must exist; both carry data-tags.
    assert 'class="card glass refract" data-tags=' in html
    assert "news-group glass refract" in html
    assert 'id="view-news"' in html and "data-tags=" in html

    # The same repo must carry identical tags in both views (map behaviour).
    card_tags = {}
    for section in sections["sections"]:
        for item in section["items"]:
            card_tags[item["full_name"]] = item["tags"]
    news = render.build_news_items(sections["sections"])
    for n in news:
        assert n["tags"] == card_tags[n["full_name"]]


def test_render_has_top_chat_bar_and_no_confidence_banner(tmp_path):
    conn, settings, edition, sections = _demo_sections(tmp_path)
    out = render.render_edition(conn, edition, settings, sections=sections)
    html = out.read_text(encoding="utf-8")

    # Top chatbot ask bar wired to the panel via PANEL_URL + poll logic.
    assert 'class="ask ' in html
    assert 'id="ask-form"' in html
    assert 'var PANEL_URL = "http://127.0.0.1:' in html
    assert "/api/ask_edition" in html
    assert "/api/ask_edition/result" in html
    # panel_port context flows into the page (default 8765 for a bare Settings).
    assert "http://127.0.0.1:8765" in html

    # The old confidence banner is gone entirely.
    assert 'class="banner' not in html
    assert "snapshot history behind these numbers" not in html

    # The "This week in tech, explained" explainer card was removed on request;
    # the top chat bar covers the conversational role now.
    assert "This week in tech, explained" not in html


def test_summary_extras_derives_followup_and_suggestions():
    summary = {"text": "hi", "model": "demo"}
    tag_bar = [{"id": "ai-agents"}, {"id": "context-rag"}]
    sections = [{"items": [{"language": "rust"}, {"language": "rust"}]}]
    out = render.summary_extras(summary, tag_bar, sections)
    assert out["followup"]
    assert isinstance(out["suggestions"], list) and out["suggestions"]
    assert len(out["suggestions"]) <= 3
    # Grounded on the edition's tags/language.
    assert any("agent" in s.lower() for s in out["suggestions"])
    assert any("rust" in s.lower() for s in out["suggestions"])

    # Existing keys are preserved (idempotent for a future LLM path).
    pre = {"text": "hi", "followup": "Keep me?", "suggestions": ["only this"]}
    out2 = render.summary_extras(pre, tag_bar, sections)
    assert out2["followup"] == "Keep me?"
    assert out2["suggestions"] == ["only this"]

    # Empty-safe.
    assert render.summary_extras(None, tag_bar, sections) is None

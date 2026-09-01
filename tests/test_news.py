"""Offline tests for the News view: render.build_news_items and its helpers.

Pure, deterministic, no network and no LLM. The News feed is derived from the
exact same sections dict shape that score.compute_metrics + hype.merge_hype_sections
produce, so these tests build small synthetic sections and assert the blurbs.
"""

from __future__ import annotations

import copy

from ghpulse import render


def _section(key, title, items):
    return {"key": key, "title": title, "subtitle": "", "items": items}


def _item(rank, full_name, *, week=None, month=None, description=None, **extra):
    item = {
        "rank": rank,
        "full_name": full_name,
        "url": f"https://github.com/{full_name}",
        "language": extra.get("language"),
        "stars": extra.get("stars", 100),
        "value": extra.get("value", 1.0),
        "value_label": extra.get("value_label", "+1"),
        "description": description,
        "spark": extra.get("spark", [1, 2, 3]),
    }
    deltas = {}
    if week is not None:
        deltas["week"] = f"{week:+.0f}% wk"
    if month is not None:
        deltas["month"] = f"{month:+.0f}% mo"
    if deltas:
        item["deltas"] = deltas
    if "hype_badges" in extra:
        item["hype_badges"] = extra["hype_badges"]
    return item


# ---------------------------------------------------------------------------
# Percent parsing helper
# ---------------------------------------------------------------------------


def test_parse_pct_reads_signed_int():
    assert render._parse_pct("+14% wk") == 14
    assert render._parse_pct("-6% mo") == -6
    assert render._parse_pct("+0% wk") == 0


def test_parse_pct_is_empty_safe():
    assert render._parse_pct(None) is None
    assert render._parse_pct("") is None
    assert render._parse_pct("no digits here") is None


# ---------------------------------------------------------------------------
# build_news_items — headline content
# ---------------------------------------------------------------------------


def test_one_item_per_top_mover_with_weekly_pct():
    sections = [
        _section("a", "New stars", [_item(1, "astral-sh/uv", week=14, month=20),
                                    _item(2, "other/repo", week=2)]),
        _section("b", "Fastest %", [_item(1, "newwave/agentlens", week=40)]),
    ]
    items = render.build_news_items(sections)
    # One blurb per section top mover (rank-1 item only).
    assert len(items) == 2
    assert items[0]["full_name"] == "astral-sh/uv"
    assert items[1]["full_name"] == "newwave/agentlens"
    # Weekly % must appear in the headline text for every mover.
    assert "14%" in items[0]["headline"]
    assert "this week" in items[0]["headline"]
    assert "40%" in items[1]["headline"]


def test_monthly_pct_appears_only_when_present():
    sections = [
        _section("a", "A", [_item(1, "astral-sh/uv", week=14, month=20)]),
        _section("b", "B", [_item(1, "solo/repo", week=9)]),
    ]
    items = render.build_news_items(sections)
    with_month = items[0]["headline"]
    without_month = items[1]["headline"]
    assert "20%" in with_month and "this month" in with_month
    assert "this month" not in without_month
    assert items[1]["month_pct"] is None


def test_headline_direction_and_takeaway_from_description():
    sections = [
        _section("a", "A", [
            _item(1, "falling/star", week=-8,
                  description="A once-hot project cooling off after its launch spike."),
        ]),
    ]
    items = render.build_news_items(sections)
    head = items[0]["headline"]
    assert "down" in head and "8%" in head
    # Takeaway is derived deterministically from the description.
    assert items[0]["takeaway"].startswith("A once-hot project")


def test_dedupe_repo_topping_multiple_sections():
    top = _item(1, "astral-sh/uv", week=14, month=20)
    sections = [
        _section("new_stars_abs", "New stars", [copy.deepcopy(top)]),
        _section("star_growth_pct", "Fastest %", [copy.deepcopy(top)]),
        _section("breakout", "Breakouts", [_item(1, "fresh/thing", week=5)]),
    ]
    items = render.build_news_items(sections)
    names = [n["full_name"] for n in items]
    assert names == ["astral-sh/uv", "fresh/thing"]  # uv reported once


def test_mover_without_any_delta_still_gets_a_headline():
    sections = [_section("breakout", "Breakouts", [_item(1, "brand/new")])]
    items = render.build_news_items(sections)
    assert len(items) == 1
    assert items[0]["week_pct"] is None and items[0]["month_pct"] is None
    assert "brand/new" in items[0]["headline"]


# ---------------------------------------------------------------------------
# Shape / passthrough
# ---------------------------------------------------------------------------


def test_accepts_full_metrics_dict_and_bare_list():
    section_list = [_section("a", "A", [_item(1, "x/y", week=5)])]
    from_list = render.build_news_items(section_list)
    from_dict = render.build_news_items({"edition": "2026-01-01", "sections": section_list})
    assert from_list == from_dict
    assert from_list[0]["full_name"] == "x/y"


def test_hype_badges_pass_through():
    badges = [{"platform": "hn", "label": "HN", "count": 3, "top_url": "https://hn/x"}]
    sections = [_section("a", "A", [_item(1, "buzzy/repo", week=10, hype_badges=badges)])]
    items = render.build_news_items(sections)
    assert items[0]["hype_badges"] == badges


# ---------------------------------------------------------------------------
# Empty-safe + purity
# ---------------------------------------------------------------------------


def test_empty_safe():
    assert render.build_news_items([]) == []
    assert render.build_news_items({}) == []
    assert render.build_news_items({"sections": []}) == []
    # Sections that have no items are skipped, not crashed on.
    assert render.build_news_items([_section("a", "A", [])]) == []


def test_pure_no_mutation_and_repeatable():
    sections = [
        _section("a", "A", [_item(1, "astral-sh/uv", week=14, month=20)]),
        _section("b", "B", [_item(1, "newwave/agentlens", week=40)]),
    ]
    snapshot = copy.deepcopy(sections)
    first = render.build_news_items(sections)
    second = render.build_news_items(sections)
    # Same input -> identical output, and the input is never mutated.
    assert first == second
    assert sections == snapshot


def test_end_to_end_from_demo_sections(tmp_path):
    """The News feed populates from real demo data (no network, no LLM)."""
    from ghpulse import db, demo, hype, score
    from ghpulse.config import Settings

    conn = db.connect(tmp_path / "news.db")
    db.init_db(conn)
    settings = Settings(
        home=tmp_path,
        db_path=tmp_path / "news.db",
        site_dir=tmp_path / "site",
    )
    edition = demo.seed_demo(conn, settings, edition="2026-01-15")

    sections = score.compute_metrics(conn, edition)
    sections = hype.merge_hype_sections(conn, edition, sections)
    items = render.build_news_items(sections)

    assert items, "demo data should yield at least one news blurb"
    # Every blurb names a repo and carries a non-empty headline.
    for n in items:
        assert n["full_name"]
        assert n["headline"]
    # De-duplicated across sections.
    names = [n["full_name"] for n in items]
    assert len(names) == len(set(names))

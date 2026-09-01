"""Offline tests for ghpulse.score: pure helpers and an end-to-end metrics pass."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ghpulse import db, score


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_pct_growth_basic() -> None:
    assert score.pct_growth(150, 100) == pytest.approx(50.0)
    assert score.pct_growth(100, 100) == pytest.approx(0.0)
    assert score.pct_growth(300, 100) == pytest.approx(200.0)


def test_pct_growth_zero_ref_guard() -> None:
    # Contract: divide by max(ref, 1), so ref=0 must not raise.
    assert score.pct_growth(10, 0) == pytest.approx(1000.0)


def test_zscore_basic_properties() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    zs = score.zscore(values)
    assert len(zs) == len(values)
    # Mean of z-scores is ~0; ordering preserved; symmetric around the middle.
    assert sum(zs) == pytest.approx(0.0, abs=1e-9)
    assert zs == sorted(zs)
    assert zs[2] == pytest.approx(0.0, abs=1e-9)
    assert zs[0] == pytest.approx(-zs[4])


def test_zscore_constant_input_does_not_crash() -> None:
    zs = score.zscore([5.0, 5.0, 5.0])
    assert len(zs) == 3
    for z in zs:
        assert z == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# End-to-end: seed snapshots via db, run compute_metrics, inspect sections
# ---------------------------------------------------------------------------


def _seed_repo(
    conn,
    repo_id: int,
    full_name: str,
    *,
    stars_ref: int,
    stars_now: int,
    forks_ref: int = 10,
    forks_now: int = 12,
    created_at: str | None = None,
    language: str = "python",
) -> None:
    now = datetime.now(timezone.utc)
    if created_at is None:
        created_at = (now - timedelta(days=400)).isoformat()
    db.upsert_repo(
        conn,
        {
            "id": repo_id,
            "full_name": full_name,
            "owner": full_name.split("/")[0],
            "language": language,
            "created_at": created_at,
            "description": f"Test repo {full_name}",
            "homepage": None,
            "license": "MIT",
            "topics": ["llm"],
        },
    )
    ref_ts = (now - timedelta(days=8)).isoformat()
    now_ts = now.isoformat()
    db.insert_snapshot(conn, repo_id, ref_ts, stars=stars_ref, forks=forks_ref)
    db.insert_snapshot(conn, repo_id, now_ts, stars=stars_now, forks=forks_now)


@pytest.fixture()
def seeded_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "score.db")
    db.init_db(conn)
    now = datetime.now(timezone.utc)

    # A spread of established repos with real growth (all above the 100-star
    # floor at the reference snapshot) so every section has candidates.
    established = [
        (1, "acme/rocket", 1000, 1400),
        (2, "orbit/lander", 500, 650),
        (3, "nova/engine", 2000, 2300),
        (4, "quark/db", 150, 240),
        (5, "flux/queue", 800, 830),
        (6, "ion/cache", 300, 420),
        (7, "zen/parser", 5000, 5600),
        (8, "echo/net", 120, 200),
        (9, "warp/cli", 900, 1000),
        (10, "beam/ml", 4000, 4900),
    ]
    for repo_id, name, s_ref, s_now in established:
        _seed_repo(conn, repo_id, name, stars_ref=s_ref, stars_now=s_now)

    # The anti-noise case: tiny repo going 2 -> 20 stars (900% "growth").
    # Its reference stars are far below MIN_STARS_FOR_PCT, so it must be
    # excluded from the fastest_pct section.
    _seed_repo(conn, 99, "tiny/noise", stars_ref=2, stars_now=20)

    # A young breakout repo: created 10 days ago, above the breakout floor.
    _seed_repo(
        conn,
        50,
        "fresh/breakout",
        stars_ref=100,
        stars_now=900,
        created_at=(now - timedelta(days=10)).isoformat(),
    )

    db.commit(conn)
    yield conn
    conn.close()


def _sections_by_key(result: dict) -> dict:
    return {section["key"]: section for section in result["sections"]}


def test_compute_metrics_returns_populated_sections(seeded_conn) -> None:
    edition = datetime.now(timezone.utc).date().isoformat()
    result = score.compute_metrics(seeded_conn, edition)

    assert result["edition"] == edition
    assert result["cohort_size"] > 0
    assert result["generated_at"]

    sections = _sections_by_key(result)
    # The fixed sort-based sections were replaced by ONE reader-controlled
    # Leaderboard; Breakouts and Risers remain as their own sections.
    for key in ("leaderboard", "breakouts", "risers_watch"):
        assert key in sections, f"missing section {key}"

    assert len(sections["leaderboard"]["items"]) > 0

    # Leaderboard availability + default are surfaced for the selector, and the
    # seed HAS week history so momentum is the default and is available.
    assert result["leaderboard_default"] == "momentum"
    dims = result["leaderboard_dims"]
    assert dims["momentum"]["available"] is True
    assert dims["stars"]["available"] is True

    # Items carry the fields the renderer + client-side sorter need.
    item = sections["leaderboard"]["items"][0]
    for field in ("rank", "full_name", "url", "stars", "value", "value_label", "metrics"):
        assert field in item
    for mkey in ("stars", "gained_wk", "momentum", "star_velocity", "forks", "commits_7d"):
        assert mkey in item["metrics"]

    # Biggest absolute gain in the seed: beam/ml at +900 (via the metric table).
    top_gain = seeded_conn.execute(
        "SELECT repo_id FROM metric WHERE edition = ? AND name = ? AND rank = 1",
        (edition, "new_stars_abs"),
    ).fetchone()[0]
    top_name = seeded_conn.execute(
        "SELECT full_name FROM repo WHERE id = ?", (top_gain,)
    ).fetchone()[0]
    assert top_name == "beam/ml"


def test_leaderboard_growth_dim_respects_min_star_floor(seeded_conn) -> None:
    edition = datetime.now(timezone.utc).date().isoformat()
    result = score.compute_metrics(seeded_conn, edition)

    sections = _sections_by_key(result)
    lb_items = {i["full_name"]: i for i in sections["leaderboard"]["items"]}

    # 2 -> 20 stars is 900% growth but below MIN_STARS_FOR_PCT: its weekly %
    # growth must be UNAVAILABLE (None), so the growth dimension can't rank it.
    if "tiny/noise" in lb_items:
        assert lb_items["tiny/noise"]["metrics"]["growth_wk"] is None

    # A legitimately fast grower keeps a real weekly % growth metric.
    assert any(
        i["metrics"]["growth_wk"] is not None
        for i in sections["leaderboard"]["items"]
    )


def test_breakouts_only_young_repos(seeded_conn) -> None:
    edition = datetime.now(timezone.utc).date().isoformat()
    result = score.compute_metrics(seeded_conn, edition)

    sections = _sections_by_key(result)
    breakout_names = [i["full_name"] for i in sections["breakouts"]["items"]]

    assert "fresh/breakout" in breakout_names
    # Old repos (created ~400 days ago) must not appear as breakouts.
    assert "acme/rocket" not in breakout_names


def test_metrics_written_to_metric_table(seeded_conn) -> None:
    edition = datetime.now(timezone.utc).date().isoformat()
    score.compute_metrics(seeded_conn, edition)

    rows = seeded_conn.execute(
        "SELECT DISTINCT name FROM metric WHERE edition = ?", (edition,)
    ).fetchall()
    names = {row[0] for row in rows}
    assert "new_stars_abs" in names
    assert "star_growth_pct" in names

    # The tiny repo must have no star_growth_pct metric row (gated by floor).
    gated = seeded_conn.execute(
        "SELECT COUNT(*) FROM metric WHERE edition = ? AND name = ? AND repo_id = ?",
        (edition, "star_growth_pct", 99),
    ).fetchone()[0]
    assert gated == 0


def test_latest_edition(seeded_conn) -> None:
    assert score.latest_edition(seeded_conn) is None
    edition = datetime.now(timezone.utc).date().isoformat()
    score.compute_metrics(seeded_conn, edition)
    assert score.latest_edition(seeded_conn) == edition


# ---------------------------------------------------------------------------
# Monthly (~30-day) window
# ---------------------------------------------------------------------------


def test_monthly_pct_present_with_30d_snapshot_absent_without(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "monthly.db")
    db.init_db(conn)
    now = datetime.now(timezone.utc)

    # has_month: three snapshots, incl. one ~35 days old (above the pct floor).
    db.upsert_repo(
        conn,
        {
            "id": 1,
            "full_name": "acme/rocket",
            "owner": "acme",
            "language": "python",
            "created_at": (now - timedelta(days=400)).isoformat(),
            "description": "monthly-capable repo",
            "homepage": None,
            "license": "MIT",
            "topics": [],
        },
    )
    db.insert_snapshot(conn, 1, (now - timedelta(days=35)).isoformat(), stars=1000, forks=10)
    db.insert_snapshot(conn, 1, (now - timedelta(days=7)).isoformat(), stars=1300, forks=12)
    db.insert_snapshot(conn, 1, now.isoformat(), stars=1500, forks=14)

    # no_month: only recent snapshots (nothing ~30 days old).
    db.upsert_repo(
        conn,
        {
            "id": 2,
            "full_name": "fresh/breakout",
            "owner": "fresh",
            "language": "rust",
            "created_at": (now - timedelta(days=10)).isoformat(),
            "description": "too young for a monthly baseline",
            "homepage": None,
            "license": "MIT",
            "topics": [],
        },
    )
    db.insert_snapshot(conn, 2, (now - timedelta(days=6)).isoformat(), stars=200, forks=5)
    db.insert_snapshot(conn, 2, now.isoformat(), stars=900, forks=9)
    db.commit(conn)

    edition = now.date().isoformat()
    result = score.compute_metrics(conn, edition)

    # Monthly metric rows persisted for the repo with a 30-day baseline...
    month_rows = {
        r["repo_id"]: r["value"]
        for r in conn.execute(
            "SELECT repo_id, value FROM metric WHERE edition = ? AND name = ?",
            (edition, "star_growth_pct_30d"),
        ).fetchall()
    }
    assert 1 in month_rows
    # 1000 -> 1500 over ~35 days = +50%.
    assert month_rows[1] == pytest.approx(50.0)
    # ...and ABSENT for the young repo with no 30-day snapshot (not fabricated).
    assert 2 not in month_rows
    abs30 = {
        r["repo_id"]
        for r in conn.execute(
            "SELECT repo_id FROM metric WHERE edition = ? AND name = ?",
            (edition, "new_stars_abs_30d"),
        ).fetchall()
    }
    assert 1 in abs30
    assert 2 not in abs30

    # The card for the monthly-capable repo carries a `deltas.month` chip;
    # the young repo's card does not.
    items = {
        item["full_name"]: item
        for section in result["sections"]
        for item in section["items"]
    }
    assert items["acme/rocket"].get("deltas", {}).get("month") is not None
    young_deltas = items.get("fresh/breakout", {}).get("deltas") or {}
    assert "month" not in young_deltas

    conn.close()


# ---------------------------------------------------------------------------
# build_leaderboard — pure ordering / dedup / cap / dims / default_dim
# ---------------------------------------------------------------------------


def _stat(
    repo_id: int,
    full_name: str,
    *,
    momentum_z: float = 0.0,
    new_stars_abs: int = 0,
    star_growth_pct=None,
    star_velocity: float = 0.0,
    fork_delta: int = 0,
    forks_now: int = 0,
    commits_7d=None,
    stars_now: int = 100,
    new_stars_abs_30d=None,
    star_growth_pct_30d=None,
    commit_growth_wk=None,
) -> score._RepoStats:
    return score._RepoStats(
        repo_id=repo_id,
        full_name=full_name,
        language="python",
        description="desc",
        topics=[],
        commits_7d=commits_7d,
        contributors_7d=None,
        stars_now=stars_now,
        stars_ref=stars_now - new_stars_abs,
        new_stars_abs=new_stars_abs,
        star_growth_pct=star_growth_pct,
        star_velocity=star_velocity,
        breakout=None,
        fork_velocity=0.0,
        watcher_growth=0.0,
        issue_delta=0,
        fork_delta=fork_delta,
        forks_now=forks_now,
        age_days=100.0,
        span_days=7.0,
        history_days=7.0,
        momentum_z=momentum_z,
        commit_growth_wk=commit_growth_wk,
        new_stars_abs_30d=new_stars_abs_30d,
        star_growth_pct_30d=star_growth_pct_30d,
    )


def test_build_leaderboard_default_sort_and_fields() -> None:
    stats = [
        _stat(1, "a/one", momentum_z=2.0, new_stars_abs=100, stars_now=500),
        _stat(2, "b/two", momentum_z=0.5, new_stars_abs=800, stars_now=1000),
        _stat(3, "c/three", momentum_z=1.2, new_stars_abs=50, stars_now=300),
    ]
    lb = score.build_leaderboard(stats)
    assert lb["key"] == "leaderboard"
    # Week history present (non-zero momentum/new stars) -> default momentum.
    assert lb["default_dim"] == "momentum"
    names = [i["full_name"] for i in lb["items"]]
    # Server-sorted by momentum desc: a(2.0) > c(1.2) > b(0.5).
    assert names == ["a/one", "c/three", "b/two"]
    assert [i["rank"] for i in lb["items"]] == [1, 2, 3]


def test_build_leaderboard_dedup_and_cap() -> None:
    # 5 repos, per_dim=2, cap=3. Union across dims dedups by repo, capped at 3.
    stats = [
        _stat(1, "a/1", momentum_z=5, new_stars_abs=1, stars_now=10),
        _stat(2, "a/2", momentum_z=4, new_stars_abs=2, stars_now=20),
        _stat(3, "a/3", momentum_z=3, new_stars_abs=3, stars_now=30),
        _stat(4, "a/4", momentum_z=2, new_stars_abs=4, stars_now=40),
        _stat(5, "a/5", momentum_z=1, new_stars_abs=5, stars_now=50),
    ]
    lb = score.build_leaderboard(stats, per_dim=2, cap=3)
    assert len(lb["items"]) == 3
    # No duplicate repos.
    names = [i["full_name"] for i in lb["items"]]
    assert len(set(names)) == len(names)


def test_build_leaderboard_dims_availability() -> None:
    # No week history: all momentum_z / new_stars_abs are zero.
    cold = [
        _stat(1, "a/1", commits_7d=10, stars_now=800),
        _stat(2, "a/2", commits_7d=0, stars_now=200),
    ]
    lb = score.build_leaderboard(cold)
    assert lb["default_dim"] == "commits"
    dims = lb["dims"]
    assert dims["momentum"]["available"] is False
    assert dims["gained"]["available"] is False
    assert dims["growth"]["available"] is False
    assert dims["commits"]["available"] is True  # one repo has commits
    assert dims["forks"]["available"] is True
    assert dims["stars"]["available"] is True

    # With growth + monthly data present the flags flip on.
    warm = [
        _stat(1, "a/1", momentum_z=1.0, new_stars_abs=100, star_growth_pct=12.0,
              star_growth_pct_30d=30.0, new_stars_abs_30d=200, commits_7d=5),
    ]
    lb2 = score.build_leaderboard(warm)
    assert lb2["default_dim"] == "momentum"
    assert lb2["dims"]["growth"]["available"] is True
    assert lb2["dims"]["growth"]["month"] is True
    assert lb2["dims"]["gained"]["month"] is True


# ---------------------------------------------------------------------------
# Render smoke: data-growth-* absent on cold start, present with history
# ---------------------------------------------------------------------------


def _render_html(tmp_path: Path, *, with_history: bool) -> str:
    from ghpulse import render
    from ghpulse.config import Settings

    conn = db.connect(tmp_path / "render.db")
    db.init_db(conn)
    settings = Settings(
        home=tmp_path, db_path=tmp_path / "render.db", site_dir=tmp_path / "site"
    )
    now = datetime.now(timezone.utc)
    db.upsert_repo(
        conn,
        {
            "id": 1,
            "full_name": "acme/rocket",
            "owner": "acme",
            "language": "python",
            "created_at": (now - timedelta(days=400)).isoformat(),
            "description": "a tracked repo",
            "homepage": None,
            "license": "MIT",
            "topics": ["llm"],
        },
    )
    if with_history:
        db.insert_snapshot(
            conn, 1, (now - timedelta(days=8)).isoformat(), stars=1000, forks=10,
            commits_7d=20,
        )
    db.insert_snapshot(conn, 1, now.isoformat(), stars=1400, forks=14, commits_7d=25)
    db.commit(conn)
    edition = now.date().isoformat()
    out = render.render_edition(conn, edition, settings)
    html = out.read_text(encoding="utf-8")
    conn.close()
    return html


def test_render_leaderboard_growth_attrs_present_with_history(tmp_path: Path) -> None:
    html = _render_html(tmp_path, with_history=True)
    assert 'id="leaderboard-grid"' in html
    assert 'class="rank-bar' in html
    assert "data-growth-wk=" in html  # weekly % growth is available
    assert "data-momentum=" in html


def test_render_leaderboard_growth_attrs_absent_on_cold_start(tmp_path: Path) -> None:
    html = _render_html(tmp_path, with_history=False)
    assert 'id="leaderboard-grid"' in html
    # Cold start: only one snapshot -> weekly/monthly % growth are unavailable,
    # so their attributes must be ABSENT (never fabricated as 0).
    assert "data-growth-wk=" not in html
    assert "data-growth-mo=" not in html
    # The always-available metrics are still present.
    assert "data-stars=" in html
    assert "data-commits=" in html

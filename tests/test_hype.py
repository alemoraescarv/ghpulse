"""Offline tests for ghpulse.hype: pure helpers + an end-to-end buzz pass.

No network: social_mention rows are seeded directly via db, exactly like the
snapshot rows in test_score.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ghpulse import db, hype, score


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_recency_decay_halves_each_half_life() -> None:
    assert hype.recency_decay(0.0, 48.0) == pytest.approx(1.0)
    assert hype.recency_decay(48.0, 48.0) == pytest.approx(0.5)
    assert hype.recency_decay(96.0, 48.0) == pytest.approx(0.25)


def test_recency_decay_clamps_negative_age() -> None:
    # A post "from the future" must not amplify beyond 1.0.
    assert hype.recency_decay(-10.0, 48.0) == pytest.approx(1.0)


def test_blend_weights_momentum_and_hype() -> None:
    assert hype.blend(1.0, 0.0) == pytest.approx(0.6)
    assert hype.blend(0.0, 1.0) == pytest.approx(0.4)
    assert hype.blend(2.0, -1.0, 0.5) == pytest.approx(0.5)


def test_platform_label() -> None:
    assert hype.platform_label("hn") == "HN"
    assert hype.platform_label("reddit") == "Reddit"
    assert hype.platform_label("mystery") == "Mystery"


# ---------------------------------------------------------------------------
# End-to-end: snapshots + mentions -> compute_metrics + compute_hype -> sections
# ---------------------------------------------------------------------------


def _seed_repo(conn, repo_id, full_name, *, stars_ref, stars_now, language="python"):
    now = datetime.now(timezone.utc)
    db.upsert_repo(
        conn,
        {
            "id": repo_id,
            "full_name": full_name,
            "owner": full_name.split("/")[0],
            "language": language,
            "created_at": (now - timedelta(days=400)).isoformat(),
            "description": f"Test repo {full_name}",
            "homepage": None,
            "license": "MIT",
            "topics": ["llm"],
        },
    )
    db.insert_snapshot(conn, repo_id, (now - timedelta(days=8)).isoformat(), stars=stars_ref, forks=10)
    db.insert_snapshot(conn, repo_id, now.isoformat(), stars=stars_now, forks=12)


def _add_mentions(conn, repo_id, platform, count, engagement):
    now = datetime.now(timezone.utc)
    for i in range(count):
        db.insert_mention(
            conn,
            repo_id=repo_id,
            platform=platform,
            post_id=f"{platform}-{repo_id}-{i}",
            url=f"https://example.test/{platform}/{repo_id}/{i}",
            title=f"post {i}",
            engagement=engagement,
            posted_at=(now - timedelta(hours=6 * (i + 1))).isoformat(),
            captured_at=now.isoformat(),
        )


@pytest.fixture()
def seeded_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "hype.db")
    db.init_db(conn)

    # Four growers in the same star bucket (~1000s) with real star velocity.
    _seed_repo(conn, 1, "acme/rocket", stars_ref=1000, stars_now=1400)
    _seed_repo(conn, 2, "orbit/lander", stars_ref=1100, stars_now=1500)
    _seed_repo(conn, 3, "nova/engine", stars_ref=1200, stars_now=1650)
    _seed_repo(conn, 4, "flux/queue", stars_ref=1300, stars_now=1720)

    # The divergence plant: flat stars (low momentum) but a huge HN thread.
    _seed_repo(conn, 10, "sleeper/quiet", stars_ref=1000, stars_now=1005)

    db.commit(conn)
    return conn


def test_compute_hype_writes_buzz_and_hype_metrics(seeded_conn) -> None:
    edition = datetime.now(timezone.utc).date().isoformat()
    score.compute_metrics(seeded_conn, edition)

    _add_mentions(seeded_conn, 10, "hn", count=5, engagement=500)
    _add_mentions(seeded_conn, 10, "reddit", count=3, engagement=120)
    seeded_conn.commit()

    hype.compute_hype(seeded_conn, edition)

    names = {
        r[0]
        for r in seeded_conn.execute(
            "SELECT DISTINCT name FROM metric WHERE edition = ?", (edition,)
        ).fetchall()
    }
    assert "buzz_score" in names
    assert "hype_z" in names
    assert "hype_hn" in names

    # compute_hype must not wipe the base metrics written by compute_metrics.
    assert "momentum_z" in names
    assert "new_stars_abs" in names


def test_merge_attaches_badges_without_buzz_sections(seeded_conn) -> None:
    edition = datetime.now(timezone.utc).date().isoformat()
    sections = score.compute_metrics(seeded_conn, edition)
    before_keys = [s["key"] for s in sections["sections"]]

    # Big buzz on the flat-star repo; little/none on the growers.
    _add_mentions(seeded_conn, 10, "hn", count=6, engagement=700)
    _add_mentions(seeded_conn, 10, "reddit", count=4, engagement=150)
    _add_mentions(seeded_conn, 1, "lobsters", count=1, engagement=5)
    seeded_conn.commit()

    hype.compute_hype(seeded_conn, edition)
    merged = hype.merge_hype_sections(seeded_conn, edition, sections)

    keys = [s["key"] for s in merged["sections"]]
    # The standalone Buzz sections are gone; the leaderboard stays first.
    assert keys == before_keys
    assert keys[0] == "leaderboard"
    assert "buzz_build" not in keys
    assert "buzzing_social" not in keys
    assert "ahead_curve" not in keys

    # Per-card hype badges still attach to the mentioned repo wherever it shows.
    by_key = {s["key"]: s for s in merged["sections"]}
    lb_items = {i["full_name"]: i for i in by_key["leaderboard"]["items"]}
    assert "sleeper/quiet" in lb_items
    sleeper_item = lb_items["sleeper/quiet"]
    assert sleeper_item.get("hype_badges")
    platforms = {b["platform"] for b in sleeper_item["hype_badges"]}
    assert "hn" in platforms


def test_no_social_data_leaves_sections_unchanged(seeded_conn) -> None:
    edition = datetime.now(timezone.utc).date().isoformat()
    sections = score.compute_metrics(seeded_conn, edition)
    before = [s["key"] for s in sections["sections"]]

    # No mentions seeded: hype pass runs but merge is a no-op (backward compatible).
    hype.compute_hype(seeded_conn, edition)
    merged = hype.merge_hype_sections(seeded_conn, edition, sections)

    after = [s["key"] for s in merged["sections"]]
    assert after == before
    for section in merged["sections"]:
        for item in section["items"]:
            assert "hype_badges" not in item

"""Offline tests for ghpulse.db: schema init, repo upsert, snapshot round-trips."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ghpulse import db


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_repo(repo_id: int = 1, full_name: str = "acme/rocket", **overrides) -> dict:
    repo = {
        "id": repo_id,
        "full_name": full_name,
        "owner": full_name.split("/")[0],
        "language": "python",
        "created_at": "2025-01-01T00:00:00+00:00",
        "description": "A rocket-fast library",
        "homepage": "https://example.com",
        "license": "MIT",
        "topics": ["llm", "rag"],
    }
    repo.update(overrides)
    return repo


@pytest.fixture()
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "test.db")
    db.init_db(connection)
    yield connection
    connection.close()


def test_init_db_idempotent(tmp_path: Path) -> None:
    connection = db.connect(tmp_path / "idem.db")
    db.init_db(connection)
    # Second (and third) init must not raise or destroy data.
    db.init_db(connection)

    db.upsert_repo(connection, _make_repo())
    db.commit(connection)
    db.init_db(connection)

    row = db.repo_row(connection, 1)
    assert row is not None
    assert row["full_name"] == "acme/rocket"
    connection.close()


def test_connect_sets_row_factory(conn) -> None:
    row = conn.execute("SELECT 1 AS one").fetchone()
    assert row["one"] == 1


def test_now_iso_parses_as_utc() -> None:
    ts = db.now_iso()
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_upsert_repo_roundtrip_and_update(conn) -> None:
    db.upsert_repo(conn, _make_repo())
    db.commit(conn)

    row = db.repo_row(conn, 1)
    assert row is not None
    assert row["full_name"] == "acme/rocket"
    assert row["owner"] == "acme"
    assert row["language"] == "python"
    assert row["is_tracked"] == 1

    topics = {
        r[0]
        for r in conn.execute(
            "SELECT topic FROM repo_topic WHERE repo_id = ?", (1,)
        ).fetchall()
    }
    assert topics == {"llm", "rag"}

    # Upsert again with changed mutable fields and new topics.
    db.upsert_repo(
        conn,
        _make_repo(description="Now even faster", topics=["wasm"]),
    )
    db.commit(conn)

    row2 = db.repo_row(conn, 1)
    assert row2 is not None
    assert row2["description"] == "Now even faster"
    # first_seen must be preserved across upserts.
    assert row2["first_seen"] == row["first_seen"]

    topics2 = {
        r[0]
        for r in conn.execute(
            "SELECT topic FROM repo_topic WHERE repo_id = ?", (1,)
        ).fetchall()
    }
    assert topics2 == {"wasm"}

    # Only one repo row despite two upserts.
    count = conn.execute("SELECT COUNT(*) FROM repo").fetchone()[0]
    assert count == 1


def test_tracked_repo_ids(conn) -> None:
    db.upsert_repo(conn, _make_repo(repo_id=10, full_name="a/one"))
    db.upsert_repo(conn, _make_repo(repo_id=20, full_name="b/two"))
    db.commit(conn)
    ids = db.tracked_repo_ids(conn)
    assert set(ids) == {10, 20}


def test_snapshot_roundtrip_latest_earliest(conn) -> None:
    db.upsert_repo(conn, _make_repo())
    now = datetime.now(timezone.utc)
    t0 = _iso(now - timedelta(days=14))
    t1 = _iso(now - timedelta(days=7))
    t2 = _iso(now)

    db.insert_snapshot(conn, 1, t0, stars=100, forks=10)
    db.insert_snapshot(conn, 1, t1, stars=150, forks=12, open_issues=5)
    db.insert_snapshot(conn, 1, t2, stars=300, forks=20, subscribers=40)
    db.commit(conn)

    earliest = db.earliest_snapshot(conn, 1)
    latest = db.latest_snapshot(conn, 1)
    assert earliest is not None and latest is not None
    assert earliest["captured_at"] == t0
    assert earliest["stars"] == 100
    assert latest["captured_at"] == t2
    assert latest["stars"] == 300
    assert latest["forks"] == 20

    # snapshot_on_or_before picks the most recent snapshot at/before the cutoff.
    cutoff = _iso(now - timedelta(days=6))
    ref = db.snapshot_on_or_before(conn, 1, cutoff)
    assert ref is not None
    assert ref["captured_at"] == t1

    # Exactly-at boundary counts as on-or-before.
    ref_exact = db.snapshot_on_or_before(conn, 1, t1)
    assert ref_exact is not None
    assert ref_exact["captured_at"] == t1

    # Before any snapshot -> None.
    assert db.snapshot_on_or_before(conn, 1, _iso(now - timedelta(days=30))) is None


def test_insert_snapshot_replaces_on_same_timestamp(conn) -> None:
    db.upsert_repo(conn, _make_repo())
    ts = _iso(datetime.now(timezone.utc))
    db.insert_snapshot(conn, 1, ts, stars=100, forks=1)
    db.insert_snapshot(conn, 1, ts, stars=111, forks=2)
    db.commit(conn)

    rows = conn.execute(
        "SELECT * FROM snapshot WHERE repo_id = ? AND captured_at = ?", (1, ts)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["stars"] == 111


def test_snapshot_series_chronological(conn) -> None:
    db.upsert_repo(conn, _make_repo())
    now = datetime.now(timezone.utc)
    stamps = [_iso(now - timedelta(days=d)) for d in (3, 2, 1, 0)]
    for i, ts in enumerate(stamps):
        db.insert_snapshot(conn, 1, ts, stars=100 + i, forks=i)
    db.commit(conn)

    series = db.snapshot_series(conn, 1, limit=30)
    captured = [row["captured_at"] for row in series]
    assert captured == sorted(captured)
    assert len(series) == 4
    assert series[-1]["stars"] == 103


def test_write_metrics_replaces_edition(conn) -> None:
    db.upsert_repo(conn, _make_repo())
    edition = "2026-08-17"
    db.write_metrics(conn, edition, [(1, "new_stars_abs", 50.0, 1)])
    db.write_metrics(conn, edition, [(1, "new_stars_abs", 75.0, 1)])
    db.commit(conn)

    rows = conn.execute(
        "SELECT * FROM metric WHERE edition = ? AND name = ?",
        (edition, "new_stars_abs"),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["value"] == 75.0


def test_search_tracked_ranks_and_shapes(conn) -> None:
    now = datetime.now(timezone.utc)
    # Name hit (strongest), topic hit, and description hit across three repos.
    db.upsert_repo(
        conn,
        _make_repo(1, "acme/llm-server", description="fast serving", topics=["inference"]),
    )
    db.upsert_repo(
        conn,
        _make_repo(2, "acme/tools", description="general tools", topics=["llm", "rag"]),
    )
    db.upsert_repo(
        conn,
        _make_repo(3, "acme/notes", description="notes about llm usage", topics=["docs"]),
    )
    # An untracked repo must never surface.
    db.upsert_repo(conn, _make_repo(4, "acme/hidden", topics=["llm"]))
    conn.execute("UPDATE repo SET is_tracked = 0 WHERE id = 4")
    for rid, stars in ((1, 100), (2, 500), (3, 50), (4, 9999)):
        db.insert_snapshot(conn, rid, _iso(now), stars=stars, forks=5, commits_7d=3)
    db.commit(conn)

    results = db.search_tracked(conn, "llm", limit=10)
    names = [r["full_name"] for r in results]
    assert "acme/hidden" not in names  # untracked excluded
    # Name match (3) outranks topic match (2) outranks description match (1).
    assert names[0] == "acme/llm-server"
    assert set(names) == {"acme/llm-server", "acme/tools", "acme/notes"}
    for r in results:
        for key in ("full_name", "url", "stars", "forks", "commits_7d", "tags", "tracked"):
            assert key in r
        assert r["tracked"] is True

    # Blank query -> empty. Pagination honored.
    assert db.search_tracked(conn, "   ") == []
    assert len(db.search_tracked(conn, "llm", limit=1)) == 1
    assert len(db.search_tracked(conn, "llm", limit=1, offset=2)) == 1

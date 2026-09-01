"""Pure social-hype scoring: reads social_mention + metric tables, writes hype
metrics, and merges the new Buzz sections/badges into the render sections dict.

No network access. Everything here is computable offline once social_mention
rows exist — the same pure/testable split as score.py. Cold-start friendly:
posts carry their own timestamps, so hype is meaningful on the very first run.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from . import db
from .score import TOP_N, _parse_iso, format_deltas, zscore

# Per-platform blend weights (tunable). HN is the strongest signal.
DEFAULT_WEIGHTS: dict[str, float] = {
    "hn": 1.0,
    "reddit": 0.8,
    "bluesky": 0.6,
    "lobsters": 0.7,
    "mastodon": 0.5,
}

HALF_LIFE_HOURS = 48.0
WINDOW_DAYS = 7
MOMENTUM_WEIGHT = 0.6  # buzz_score = 0.6*momentum_z + 0.4*hype_z

# "Ahead of the curve": buzzing but stars haven't moved (yet).
AHEAD_MIN_HYPE_Z = 0.3
AHEAD_MAX_MOMENTUM_Z = 0.5

_PLATFORM_LABELS: dict[str, str] = {
    "hn": "HN",
    "reddit": "Reddit",
    "bluesky": "Bluesky",
    "lobsters": "Lobsters",
    "mastodon": "Mastodon",
}


# ---------------------------------------------------------------------------
# Small pure helpers (unit-test targets)
# ---------------------------------------------------------------------------


def recency_decay(age_hours: float, half_life_hours: float = HALF_LIFE_HOURS) -> float:
    """Exponential recency weight: 1.0 at age 0, 0.5 at one half-life, etc."""
    if half_life_hours <= 0:
        return 1.0
    age = max(age_hours, 0.0)
    return 0.5 ** (age / half_life_hours)


def blend(momentum_z: float, hype_z: float, w_momentum: float = MOMENTUM_WEIGHT) -> float:
    """Combine GitHub momentum and social hype z-scores into one buzz score."""
    return w_momentum * momentum_z + (1.0 - w_momentum) * hype_z


def platform_label(platform: str) -> str:
    """Human label for a platform key ('hn' -> 'HN')."""
    return _PLATFORM_LABELS.get(platform, platform.title())


# ---------------------------------------------------------------------------
# Hype computation -> metric table
# ---------------------------------------------------------------------------


def _ranked_rows(pairs: list[tuple[int, float]], name: str) -> list[tuple]:
    """(repo_id, value) pairs -> ranked metric rows (repo_id, name, value, rank)."""
    ordered = sorted(pairs, key=lambda pair: pair[1], reverse=True)
    return [
        (repo_id, name, float(value), rank)
        for rank, (repo_id, value) in enumerate(ordered, start=1)
    ]


def _momentum_by_repo(conn: sqlite3.Connection, edition: str) -> dict[int, float]:
    rows = conn.execute(
        "SELECT repo_id, value FROM metric WHERE edition = ? AND name = 'momentum_z'",
        (edition,),
    ).fetchall()
    return {int(r["repo_id"]): float(r["value"]) for r in rows}


def _platform_hype_for_repo(
    conn: sqlite3.Connection,
    repo_id: int,
    since_iso: str,
    now: datetime,
    half_life_hours: float,
) -> dict[str, float]:
    """Per-platform hype = Σ log1p(engagement) * recency_decay(age_hours)."""
    import math

    hype: dict[str, float] = {}
    for row in db.mentions_for_repo(conn, repo_id, since_iso):
        age_hours = (now - _parse_iso(row["posted_at"])).total_seconds() / 3600.0
        contribution = math.log1p(max(int(row["engagement"]), 0)) * recency_decay(
            age_hours, half_life_hours
        )
        hype[row["platform"]] = hype.get(row["platform"], 0.0) + contribution
    return hype


def compute_hype(
    conn: sqlite3.Connection,
    edition: str,
    half_life_hours: float = HALF_LIFE_HOURS,
    weights: dict[str, float] | None = None,
) -> None:
    """Compute per-platform hype, total hype_z, and buzz_score; persist to metric.

    Layers rows onto the edition already scored by ``score.compute_metrics``
    (via ``db.add_metrics``, which does not wipe existing rows). Safe to call
    when there are no mentions — it simply writes hype_z/buzz_score derived from
    zero hype (buzz_score then tracks momentum_z alone).
    """
    # NOTE on weekly-vs-monthly for the social layer: the hype window is a
    # single ~7-day recency-decayed pass (WINDOW_DAYS), and social_mention rows
    # are only collected for the current run — there is no 3-4 week mention
    # history to build a monthly buzz baseline against yet. So we intentionally
    # do NOT emit a "hype this week vs 4-week baseline" metric here rather than
    # fabricate one; the GitHub monthly % delta still surfaces on buzz cards via
    # merge_hype_sections (see _deltas_by_repo).
    weights = weights or DEFAULT_WEIGHTS
    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(days=WINDOW_DAYS)).replace(microsecond=0).isoformat()

    momentum = _momentum_by_repo(conn, edition)
    repo_ids = db.tracked_repo_ids(conn)

    total_hype: dict[int, float] = {}
    per_platform: dict[str, list[tuple[int, float]]] = {}
    for repo_id in repo_ids:
        platform_hype = _platform_hype_for_repo(
            conn, repo_id, since_iso, now, half_life_hours
        )
        total = 0.0
        for platform, value in platform_hype.items():
            total += weights.get(platform, 0.5) * value
            if value > 0:
                per_platform.setdefault(platform, []).append((repo_id, value))
        total_hype[repo_id] = total

    ordered_ids = list(repo_ids)
    hype_values = [total_hype.get(rid, 0.0) for rid in ordered_ids]
    hype_z_list = zscore(hype_values)
    hype_z = {rid: z for rid, z in zip(ordered_ids, hype_z_list)}

    buzz = {
        rid: blend(momentum.get(rid, 0.0), hype_z.get(rid, 0.0)) for rid in ordered_ids
    }

    rows: list[tuple] = []
    for platform, pairs in per_platform.items():
        rows += _ranked_rows(pairs, f"hype_{platform}")
    rows += _ranked_rows([(rid, hype_z[rid]) for rid in ordered_ids], "hype_z")
    rows += _ranked_rows([(rid, buzz[rid]) for rid in ordered_ids], "buzz_score")

    db.add_metrics(conn, edition, rows)
    db.commit(conn)


# ---------------------------------------------------------------------------
# Section + badge assembly (merged into score.compute_metrics output)
# ---------------------------------------------------------------------------


def _metric_map(conn: sqlite3.Connection, edition: str, name: str) -> dict[int, float]:
    rows = conn.execute(
        "SELECT repo_id, value FROM metric WHERE edition = ? AND name = ?",
        (edition, name),
    ).fetchall()
    return {int(r["repo_id"]): float(r["value"]) for r in rows}


def _repo_item(
    conn: sqlite3.Connection,
    repo_id: int,
    rank: int,
    value: float,
    value_label: str,
) -> dict[str, Any] | None:
    repo = db.repo_row(conn, repo_id)
    latest = db.latest_snapshot(conn, repo_id)
    if repo is None:
        return None
    spark = [int(r["stars"]) for r in db.snapshot_series(conn, repo_id, limit=30)]
    return {
        "rank": rank,
        "full_name": repo["full_name"],
        "url": f"https://github.com/{repo['full_name']}",
        "language": repo["language"],
        "stars": int(latest["stars"]) if latest is not None else 0,
        "value": value,
        "value_label": value_label,
        "description": repo["description"],
        "topics": db.topics_for_repo(conn, repo_id),
        "commits_7d": latest["commits_7d"] if latest is not None else None,
        "contributors_7d": latest["contributors_7d"] if latest is not None else None,
        "spark": spark,
    }


def _badges_by_repo(
    conn: sqlite3.Connection, since_iso: str
) -> dict[int, list[dict[str, Any]]]:
    """Per-repo hype badges: [{platform, count, top_url, label}], strongest first."""
    rows = conn.execute(
        """
        SELECT repo_id, platform, url, engagement FROM social_mention
        WHERE posted_at >= ?
        ORDER BY engagement DESC
        """,
        (since_iso,),
    ).fetchall()
    acc: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        repo_id = int(row["repo_id"])
        platform = row["platform"]
        by_platform = acc.setdefault(repo_id, {})
        entry = by_platform.get(platform)
        if entry is None:
            # Rows arrive engagement-desc, so the first url per platform is the top post.
            by_platform[platform] = {
                "platform": platform,
                "label": platform_label(platform),
                "count": 1,
                "top_url": row["url"],
            }
        else:
            entry["count"] += 1
    badges: dict[int, list[dict[str, Any]]] = {}
    for repo_id, by_platform in acc.items():
        ordered = sorted(
            by_platform.values(), key=lambda b: (b["count"], b["platform"]), reverse=True
        )
        badges[repo_id] = ordered
    return badges


def merge_hype_sections(
    conn: sqlite3.Connection, edition: str, sections: dict[str, Any]
) -> dict[str, Any]:
    """Attach per-card hype badges to a score.compute_metrics() sections dict.

    The product now leads with rankings, so the old standalone Buzz sections
    ("Buzz + Build", "Buzzing on social", "Ahead of the curve") are no longer
    prepended. What survives is the "being discussed" signal: per-card
    ``hype_badges`` (HN/Reddit/Lobsters mention counts) are attached to whichever
    leaderboard/breakout/riser cards have mentions. The section list is left
    exactly as score produced it — ``[leaderboard, breakouts, risers]`` — so the
    Leaderboard stays the page's first section.

    Backward compatible: with NO social_mention rows this returns *sections*
    unchanged (no badges), so the page renders exactly as before.
    """
    now = datetime.now(timezone.utc)
    since_iso = (now - timedelta(days=WINDOW_DAYS)).replace(microsecond=0).isoformat()

    has_social = conn.execute(
        "SELECT 1 FROM social_mention WHERE posted_at >= ? LIMIT 1", (since_iso,)
    ).fetchone()
    if has_social is None:
        return sections

    badges = _badges_by_repo(conn, since_iso)

    # Per-card hype badges on every existing item (leaderboard/breakouts/risers).
    section_list: list[dict[str, Any]] = sections.get("sections") or []
    name_to_id = {
        r["full_name"]: int(r["id"])
        for r in conn.execute("SELECT id, full_name FROM repo").fetchall()
    }

    def _attach_badges(items: list[dict[str, Any]]) -> None:
        for item in items:
            repo_id = name_to_id.get(item.get("full_name"))
            if repo_id is not None and repo_id in badges:
                item["hype_badges"] = badges[repo_id]

    for section in section_list:
        _attach_badges(section.get("items") or [])

    return sections

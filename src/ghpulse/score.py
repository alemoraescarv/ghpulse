"""Pure scoring engine: reads repo/snapshot tables, writes metric rows, builds sections.

No network access. Everything here is computable offline from the SQLite database.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence

from . import db
from .config import MIN_STARS_FOR_BREAKOUT, MIN_STARS_FOR_PCT

TOP_N = 15
BREAKOUT_MAX_AGE_DAYS = 30
MONTHLY_REF_DAYS = 30  # secondary window: ~30-day % change, alongside the weekly one

# Thresholds for the "risers to watch" anomaly footnote: star velocity is high
# but forks and issues are flat, which often indicates gamed or low-substance growth.
RISER_MIN_STARS_ABS = 150
RISER_MIN_VELOCITY = 15.0
RISER_MAX_FORK_RATIO = 0.02  # forks gained per star gained
RISER_MAX_ISSUE_DELTA = 1


# ---------------------------------------------------------------------------
# Small pure helpers (unit-test targets)
# ---------------------------------------------------------------------------


def pct_growth(now: int, ref: int) -> float:
    """Percentage growth from ref to now, guarding against division by zero."""
    return 100.0 * (now - ref) / max(ref, 1)


def zscore(values: Sequence[float]) -> list[float]:
    """Z-scores of a sequence. A constant (or empty/singleton) sequence maps to zeros."""
    n = len(values)
    if n == 0:
        return []
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    std = math.sqrt(variance)
    if std == 0.0:
        return [0.0 for _ in values]
    return [(v - mean) / std for v in values]


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating trailing Z and naive values (assumed UTC)."""
    cleaned = ts.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _star_bucket(stars: int) -> int:
    """log10 star bucket used for momentum z-scoring (0 for <10, 1 for 10-99, ...)."""
    return int(math.log10(max(stars, 1)))


# ---------------------------------------------------------------------------
# Per-repo computation
# ---------------------------------------------------------------------------


@dataclass
class _RepoStats:
    repo_id: int
    full_name: str
    language: str | None
    description: str | None
    topics: list[str]
    commits_7d: int | None
    contributors_7d: int | None
    stars_now: int
    stars_ref: int
    new_stars_abs: int
    star_growth_pct: float | None
    star_velocity: float
    breakout: float | None
    fork_velocity: float
    watcher_growth: float
    issue_delta: int
    fork_delta: int
    forks_now: int
    age_days: float
    span_days: float
    history_days: float
    momentum_z: float = 0.0
    # Commit week-over-week % growth vs the ~7-day reference snapshot's
    # commits_7d. None unless both windows have >=5 commits and >=5 days of span
    # (so a tiny/young signal never fabricates a misleading percentage).
    commit_growth_wk: float | None = None
    spark: list[int] = field(default_factory=list)
    # Secondary ~30-day window. None when no ~30-day-old snapshot exists yet
    # (never fabricated from a younger reference).
    star_growth_pct_30d: float | None = None
    star_velocity_30d: float | None = None
    new_stars_abs_30d: int | None = None


def _compute_repo_stats(
    conn: sqlite3.Connection, repo_id: int, ref_days: int
) -> _RepoStats | None:
    repo = db.repo_row(conn, repo_id)
    latest = db.latest_snapshot(conn, repo_id)
    if repo is None or latest is None:
        return None

    now_ts = _parse_iso(latest["captured_at"])
    cutoff = now_ts - timedelta(days=ref_days)
    ref = db.snapshot_on_or_before(conn, repo_id, cutoff.isoformat())
    if ref is None:
        ref = db.earliest_snapshot(conn, repo_id)
    if ref is None:
        ref = latest

    earliest = db.earliest_snapshot(conn, repo_id) or latest
    history_days = max(
        (now_ts - _parse_iso(earliest["captured_at"])).total_seconds() / 86400.0, 0.0
    )

    ref_ts = _parse_iso(ref["captured_at"])
    span_days = max((now_ts - ref_ts).total_seconds() / 86400.0, 0.0)

    stars_now = int(latest["stars"])
    stars_ref = int(ref["stars"])
    delta = stars_now - stars_ref

    forks_now = int(latest["forks"])
    forks_ref = int(ref["forks"])
    fork_delta = forks_now - forks_ref

    subs_now = latest["subscribers"]
    subs_ref = ref["subscribers"]
    watcher_growth = float((subs_now or 0) - (subs_ref or 0))

    issues_now = latest["open_issues"]
    issues_ref = ref["open_issues"]
    issue_delta = int((issues_now or 0) - (issues_ref or 0))

    day_div = max(span_days, 1.0)
    star_velocity = delta / day_div
    fork_velocity = fork_delta / day_div

    # Commit week-over-week %: diff the latest commits_7d against the ~7-day
    # reference snapshot's commits_7d. Gate on real signal in both windows so a
    # young/quiet repo never reads a fabricated percentage.
    commit_growth_wk: float | None = None
    try:
        commits_now = int(latest["commits_7d"]) if latest["commits_7d"] is not None else 0
    except (TypeError, ValueError):
        commits_now = 0
    try:
        commits_ref = int(ref["commits_7d"]) if ref["commits_7d"] is not None else 0
    except (TypeError, ValueError):
        commits_ref = 0
    if commits_now >= 5 and commits_ref >= 5 and span_days >= 5:
        commit_growth_wk = pct_growth(commits_now, commits_ref)

    # Weekly star growth is only meaningful when there is a GENUINELY earlier
    # snapshot to diff against. On a fresh cohort's first run the reference falls
    # back to the same snapshot (span_days == 0), which would otherwise read as a
    # misleading "0%". Treat that as UNAVAILABLE so the pill is hidden, not zero.
    growth: float | None = None
    if span_days > 0 and stars_ref >= MIN_STARS_FOR_PCT:
        growth = pct_growth(stars_now, stars_ref)

    age_days = max((now_ts - _parse_iso(repo["created_at"])).total_seconds() / 86400.0, 0.0)
    breakout: float | None = None
    if age_days < BREAKOUT_MAX_AGE_DAYS and stars_now >= MIN_STARS_FOR_BREAKOUT:
        breakout = stars_now / max(age_days, 1.0)

    spark = [int(row["stars"]) for row in db.snapshot_series(conn, repo_id, limit=30)]

    # --- secondary ~30-day window ---------------------------------------
    # Use snapshot_on_or_before(now-30d) with NO fallback: if the oldest
    # snapshot is younger than ~30 days this returns None and monthly stays
    # unavailable rather than being fabricated from a 7-day-old reference.
    growth_30d: float | None = None
    velocity_30d: float | None = None
    new_stars_30d: int | None = None
    cutoff_30 = now_ts - timedelta(days=MONTHLY_REF_DAYS)
    ref_30 = db.snapshot_on_or_before(conn, repo_id, cutoff_30.isoformat())
    if ref_30 is not None:
        stars_ref_30 = int(ref_30["stars"])
        delta_30 = stars_now - stars_ref_30
        span_30 = max(
            (now_ts - _parse_iso(ref_30["captured_at"])).total_seconds() / 86400.0,
            1.0,
        )
        new_stars_30d = delta_30
        velocity_30d = delta_30 / span_30
        if stars_ref_30 >= MIN_STARS_FOR_PCT:
            growth_30d = pct_growth(stars_now, stars_ref_30)

    return _RepoStats(
        repo_id=repo_id,
        full_name=repo["full_name"],
        language=repo["language"],
        description=repo["description"],
        topics=db.topics_for_repo(conn, repo_id),
        commits_7d=latest["commits_7d"],
        contributors_7d=latest["contributors_7d"],
        stars_now=stars_now,
        stars_ref=stars_ref,
        new_stars_abs=delta,
        star_growth_pct=growth,
        star_velocity=star_velocity,
        breakout=breakout,
        fork_velocity=fork_velocity,
        watcher_growth=watcher_growth,
        issue_delta=issue_delta,
        fork_delta=fork_delta,
        forks_now=forks_now,
        age_days=age_days,
        span_days=span_days,
        history_days=history_days,
        spark=spark,
        commit_growth_wk=commit_growth_wk,
        star_growth_pct_30d=growth_30d,
        star_velocity_30d=velocity_30d,
        new_stars_abs_30d=new_stars_30d,
    )


def _assign_momentum_z(stats: list[_RepoStats]) -> None:
    """Z-score star_velocity within log10(stars) buckets, in place."""
    buckets: dict[int, list[_RepoStats]] = {}
    for s in stats:
        buckets.setdefault(_star_bucket(s.stars_now), []).append(s)
    for members in buckets.values():
        scores = zscore([m.star_velocity for m in members])
        for member, z in zip(members, scores):
            member.momentum_z = z


def _is_suspicious_riser(s: _RepoStats) -> bool:
    """High star velocity while forks and issues stay flat."""
    if s.new_stars_abs < RISER_MIN_STARS_ABS or s.star_velocity < RISER_MIN_VELOCITY:
        return False
    fork_ratio = s.fork_delta / max(s.new_stars_abs, 1)
    return fork_ratio <= RISER_MAX_FORK_RATIO and s.issue_delta <= RISER_MAX_ISSUE_DELTA


# ---------------------------------------------------------------------------
# Section assembly
# ---------------------------------------------------------------------------


def format_deltas(
    week_pct: float | None, month_pct: float | None
) -> dict[str, str] | None:
    """Build the optional per-card `deltas` chip dict from weekly/monthly % growth.

    Pure and testable. Returns None when neither window is available; omits the
    ``month`` key when the ~30-day window is unavailable (no fabrication).
    """
    out: dict[str, str] = {}
    if week_pct is not None:
        out["week"] = f"{week_pct:+.0f}% wk"
    if month_pct is not None:
        out["month"] = f"{month_pct:+.0f}% mo"
    return out or None


def _item(rank: int, s: _RepoStats, value: float, value_label: str) -> dict[str, Any]:
    item: dict[str, Any] = {
        "rank": rank,
        "full_name": s.full_name,
        "url": f"https://github.com/{s.full_name}",
        "language": s.language,
        "stars": s.stars_now,
        "value": value,
        "value_label": value_label,
        "description": s.description,
        "topics": list(s.topics),
        "commits_7d": s.commits_7d,
        "contributors_7d": s.contributors_7d,
        "spark": list(s.spark),
    }
    # Per-repo multi-dimension metric bag consumed by the client-side Leaderboard
    # sorter (rendered onto each card as data-* attributes). None values are
    # genuinely unavailable and are omitted at the attribute layer.
    item["metrics"] = {
        "stars": s.stars_now,
        "gained_wk": s.new_stars_abs,
        "gained_mo": s.new_stars_abs_30d,
        "growth_wk": s.star_growth_pct,
        "growth_mo": s.star_growth_pct_30d,
        "momentum": round(s.momentum_z, 3),
        "star_velocity": round(s.star_velocity, 2),
        "forks": s.forks_now,
        "fork_delta_wk": s.fork_delta,
        "commits_7d": s.commits_7d or 0,
        "span_days": round(s.span_days, 1),
        "commit_growth_wk": s.commit_growth_wk,
    }
    deltas = format_deltas(s.star_growth_pct, s.star_growth_pct_30d)
    if deltas is not None:
        item["deltas"] = deltas
    return item


def _build_section(
    key: str,
    title: str,
    subtitle: str,
    candidates: list[tuple[_RepoStats, float]],
    label_fn: Callable[[_RepoStats, float], str],
    top_n: int = TOP_N,
) -> dict[str, Any]:
    ordered = sorted(candidates, key=lambda pair: pair[1], reverse=True)[:top_n]
    items = [
        _item(rank, s, value, label_fn(s, value))
        for rank, (s, value) in enumerate(ordered, start=1)
    ]
    return {"key": key, "title": title, "subtitle": subtitle, "items": items}


# ---------------------------------------------------------------------------
# Reader-controlled Leaderboard
# ---------------------------------------------------------------------------

# The dimensions the reader can rank by, in display order. Each maps to the
# per-repo sort value used both to select the union of candidates (top per_dim
# per dimension) and to server-sort by the default dimension.
LEADERBOARD_DIMS = ("momentum", "growth", "gained", "forks", "commits", "stars")


def _lb_dim_value(s: _RepoStats, dim: str) -> float | None:
    """Sort value for a repo under a leaderboard dimension, or None if unavailable."""
    if dim == "momentum":
        return s.momentum_z
    if dim == "growth":
        return s.star_growth_pct
    if dim == "gained":
        return float(s.new_stars_abs)
    if dim == "forks":
        return float(s.fork_delta)
    if dim == "commits":
        c = s.commits_7d or 0
        # Require some traction (>=200 stars) so commit-spam bots — repos with
        # thousands of automated commits but almost no stars — never top the
        # "most active" ranking developers rely on to find worthwhile projects.
        return float(c) if c > 0 and s.stars_now >= 200 else None
    if dim == "stars":
        return float(s.stars_now)
    return None


def _lb_label(s: _RepoStats, dim: str) -> str:
    """Server-side value label for the DEFAULT dimension (momentum or commits).

    The client-side sorter rewrites this on load for whichever dimension the
    reader selects; only the default needs a sensible no-JS fallback.
    """
    if dim == "commits":
        c = s.commits_7d or 0
        if s.commit_growth_wk is not None:
            return f"{c:,} commits/wk · {s.commit_growth_wk:+.0f}% vs last wk"
        return f"{c:,} commits/wk"
    if dim == "growth" and s.star_growth_pct is not None:
        return f"{s.star_growth_pct:+.1f}% wk"
    if dim == "gained":
        return f"+{s.new_stars_abs:,} ★ this week"
    if dim == "forks":
        return f"+{s.fork_delta:,} forks/wk"
    if dim == "stars":
        return f"★ {s.stars_now:,}"
    # momentum default
    return f"z {s.momentum_z:+.2f} · {s.star_velocity:,.0f} ★/day"


def build_leaderboard(
    stats: list[_RepoStats], per_dim: int = 30, cap: int = 120
) -> dict[str, Any]:
    """Build the single reader-controlled Leaderboard section (pure, offline).

    Returns ``{"key": "leaderboard", "items": [...], "dims": {...},
    "default_dim": str}``. The item set is the UNION of the top ``per_dim`` repos
    under each dimension (skipping repos whose value is unavailable), de-duped by
    repo and capped at ``cap``. Items are server-sorted by the default dimension
    ("momentum" when there is week history, else "commits") so the page is useful
    before the client-side sorter runs. ``dims`` reports per-dimension (and,
    where relevant, monthly-window) availability so the selector can disable
    dimensions that need more snapshot history.
    """
    has_week_history = any(s.new_stars_abs != 0 or s.momentum_z != 0 for s in stats)
    default_dim = "momentum" if has_week_history else "commits"

    dims: dict[str, dict[str, bool]] = {
        "growth": {
            "available": any(s.star_growth_pct is not None for s in stats),
            "month": any(s.star_growth_pct_30d is not None for s in stats),
        },
        "gained": {
            "available": has_week_history,
            "month": any(s.new_stars_abs_30d is not None for s in stats),
        },
        "momentum": {"available": has_week_history},
        "forks": {"available": True},
        "commits": {"available": any((s.commits_7d or 0) > 0 for s in stats)},
        "stars": {"available": True},
    }

    # Union of the top per_dim repos under each dimension (first insertion wins;
    # de-duped by repo id). Skips repos whose dimension value is unavailable.
    selected: dict[int, _RepoStats] = {}
    for dim in LEADERBOARD_DIMS:
        scored = [(s, _lb_dim_value(s, dim)) for s in stats]
        scored = [(s, v) for s, v in scored if v is not None]
        scored.sort(key=lambda pair: (-pair[1], pair[0].full_name))
        for s, _v in scored[:per_dim]:
            selected.setdefault(s.repo_id, s)

    chosen = list(selected.values())
    chosen.sort(
        key=lambda s: (
            _lb_dim_value(s, default_dim) is None,
            -(_lb_dim_value(s, default_dim) or 0.0),
            s.full_name,
        )
    )
    chosen = chosen[:cap]

    items = [
        _item(rank, s, float(_lb_dim_value(s, default_dim) or 0.0), _lb_label(s, default_dim))
        for rank, s in enumerate(chosen, start=1)
    ]
    return {
        "key": "leaderboard",
        "items": items,
        "dims": dims,
        "default_dim": default_dim,
    }


def compute_metrics(
    conn: sqlite3.Connection, edition: str, ref_days: int = 7
) -> dict[str, Any]:
    """Compute weekly trend metrics for every tracked repo and persist them.

    Returns the sections dict consumed by render.render_edition.
    """
    stats: list[_RepoStats] = []
    for repo_id in db.tracked_repo_ids(conn):
        s = _compute_repo_stats(conn, repo_id, ref_days)
        if s is not None:
            stats.append(s)

    _assign_momentum_z(stats)

    # --- persist per-metric ranked rows -----------------------------------
    def _ranked_rows(pairs: list[tuple[_RepoStats, float]], name: str) -> list[tuple]:
        ordered = sorted(pairs, key=lambda pair: pair[1], reverse=True)
        return [
            (s.repo_id, name, float(value), rank)
            for rank, (s, value) in enumerate(ordered, start=1)
        ]

    metric_rows: list[tuple] = []
    metric_rows += _ranked_rows([(s, float(s.new_stars_abs)) for s in stats], "new_stars_abs")
    metric_rows += _ranked_rows(
        [(s, s.star_growth_pct) for s in stats if s.star_growth_pct is not None],
        "star_growth_pct",
    )
    metric_rows += _ranked_rows([(s, s.star_velocity) for s in stats], "star_velocity")
    metric_rows += _ranked_rows(
        [(s, s.breakout) for s in stats if s.breakout is not None], "breakout"
    )
    metric_rows += _ranked_rows([(s, s.fork_velocity) for s in stats], "fork_velocity")
    metric_rows += _ranked_rows([(s, s.watcher_growth) for s in stats], "watcher_growth")
    metric_rows += _ranked_rows([(s, s.momentum_z) for s in stats], "momentum_z")
    metric_rows += _ranked_rows(
        [(s, s.star_velocity) for s in stats if _is_suspicious_riser(s)], "risers_watch"
    )
    # Secondary ~30-day window: only repos that actually have a ~30-day baseline.
    metric_rows += _ranked_rows(
        [(s, float(s.new_stars_abs_30d)) for s in stats if s.new_stars_abs_30d is not None],
        "new_stars_abs_30d",
    )
    metric_rows += _ranked_rows(
        [(s, s.star_growth_pct_30d) for s in stats if s.star_growth_pct_30d is not None],
        "star_growth_pct_30d",
    )
    metric_rows += _ranked_rows(
        [(s, s.star_velocity_30d) for s in stats if s.star_velocity_30d is not None],
        "star_velocity_30d",
    )
    db.write_metrics(conn, edition, metric_rows)
    db.commit(conn)

    # --- assemble render sections -----------------------------------------
    # A single reader-controlled Leaderboard replaces the old fixed Trending /
    # Biggest-gains / Fastest-growing sections (and the cold-start section swap):
    # the reader picks the ranking dimension client-side. Breakouts and Risers to
    # watch remain as their own curated sections.
    leaderboard = build_leaderboard(stats)
    leaderboard_section = {
        "key": "leaderboard",
        "title": "Leaderboard",
        "subtitle": (
            "Rank this week's cohort by whichever signal you care about — "
            "momentum, % growth, stars gained, forks, commits, or size."
        ),
        "items": leaderboard["items"],
    }

    sections: list[dict[str, Any]] = [
        leaderboard_section,
        _build_section(
            "breakouts",
            "Breakouts",
            f"New repos (<{BREAKOUT_MAX_AGE_DAYS} days old, ≥{MIN_STARS_FOR_BREAKOUT} stars) by stars per day of life",
            [(s, s.breakout) for s in stats if s.breakout is not None],
            lambda s, v: f"{v:,.0f} ★/day · {s.age_days:.0f} days old",
        ),
        _build_section(
            "risers_watch",
            "Risers to watch",
            "Anomaly footnote: star velocity is high but forks and issues are flat — growth may not reflect real usage",
            [(s, s.star_velocity) for s in stats if _is_suspicious_riser(s)],
            lambda s, v: f"{v:,.0f} ★/day, forks & issues flat",
        ),
    ]

    # Reflect the weakest data behind the numbers: a mixed cohort where most
    # repos have only a day or two of history should not claim high confidence
    # just because one repo has been tracked for weeks.
    days_of_history = min((s.history_days for s in stats), default=0.0)

    return {
        "edition": edition,
        "cohort_size": len(stats),
        "days_of_history": int(days_of_history),
        "generated_at": db.now_iso(),
        "sections": sections,
        "leaderboard_dims": leaderboard["dims"],
        "leaderboard_default": leaderboard["default_dim"],
    }


def latest_edition(conn: sqlite3.Connection) -> str | None:
    """Most recent edition present in the metric table, or None."""
    row = conn.execute("SELECT MAX(edition) AS edition FROM metric").fetchone()
    if row is None:
        return None
    return row["edition"]

"""Repository discovery via sharded GitHub search queries.

Builds a small set of sharded search queries (to stay under the 1000-result
search cap per query), runs them, and upserts every hit into the repo table.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from ghpulse import db
from ghpulse.config import (
    STAR_RANGE_SHARDS,
    WATCHED_LANGUAGES,
    WATCHED_TOPICS,
    Settings,
)

# Star bands for established repos with recent activity (the weekly "movers").
# Each band is a separate query to stay under the 1000-result search cap.
ACTIVE_STAR_BANDS: list[str] = [">5000", "2000..5000", "1000..2000", "500..1000", "200..500"]

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import sqlite3

    from ghpulse.http import GitHubClient


def _window(field: str, since: str, until: str | None) -> str:
    """GitHub date qualifier over an open (``field:>since``) or half-open
    (``field:since..until``) window."""
    if until:
        return f"{field}:{since}..{until}"
    return f"{field}:>{since}"


def build_queries(since: str, settings: Settings, until: str | None = None) -> list[str]:
    """Return the sharded search query strings for one discovery window.

    Pure function: same inputs always yield the same queries, so it is
    unit-testable without network or database.

    ``since``/``until`` bound the date window: with ``until=None`` the queries
    match everything after ``since`` (``created:>since``); with ``until`` set
    they match a half-open range (``created:since..until``) so a month window
    can exclude the days a week window already covers.

    Builds a bounded cohort of ~thousands of repos (each query is capped at
    1000 results, and results union/dedupe by repo id):
    - New repos in-window, one query per star-range shard.
    - Established movers pushed in-window, one query per star band.
    - New in-window repos per watched language (broadens coverage a lot).
    - Every watched topic, recently active in-window.
    """
    queries: list[str] = []

    # New repos created in-window, sharded by star range.
    for shard in STAR_RANGE_SHARDS:
        queries.append(f"{_window('created', since, until)} stars:{shard}")

    # Established repos with recent activity, one query per star band.
    for band in ACTIVE_STAR_BANDS:
        queries.append(f"{_window('pushed', since, until)} stars:{band}")

    # New in-window repos per watched language (big breadth win).
    for lang in WATCHED_LANGUAGES:
        queries.append(f"{_window('created', since, until)} language:{lang} stars:>10")

    # Every watched topic, recently active and non-trivial.
    for topic in WATCHED_TOPICS:
        queries.append(f"topic:{topic} {_window('pushed', since, until)} stars:>50")

    return queries


def _item_to_repo(item: dict[str, Any]) -> dict[str, Any]:
    """Map a GitHub /search/repositories item to the db.upsert_repo dict."""
    license_obj = item.get("license") or {}
    owner_obj = item.get("owner") or {}
    return {
        "id": item["id"],
        "full_name": item["full_name"],
        "owner": owner_obj.get("login", ""),
        "language": item.get("language"),
        "created_at": item.get("created_at"),
        "description": item.get("description"),
        "homepage": item.get("homepage"),
        "license": license_obj.get("spdx_id"),
        "topics": item.get("topics", []) or [],
    }


# GitHub search occasionally returns a transient 5xx / timeout on deep pages.
# One flaky query must never abort a whole scan, so we retry a couple times and
# then skip that query, keeping everything discovered so far.
_TRANSIENT = ("error 500", "error 502", "error 503", "error 504", "error 429", "time")


def _search_with_retry(
    client: "GitHubClient", query: str, attempts: int = 3
) -> list[dict[str, Any]]:
    for i in range(attempts):
        try:
            return client.search_repositories(query, max_pages=5)
        except RuntimeError as exc:
            msg = str(exc).lower()
            transient = any(sig in msg for sig in _TRANSIENT)
            if transient and i < attempts - 1:
                time.sleep(2 * (i + 1))  # 2s, 4s backoff
                continue
            # Out of retries (or a non-transient error): skip this one query,
            # log it, and keep the rest of the scan going.
            print(f"discover: skipping query after error — {query!r}: {exc}")
            return []
    return []


def discover(
    client: "GitHubClient",
    conn: "sqlite3.Connection",
    windows: "str | list[tuple[str, str | None]]",
    settings: Settings,
) -> int:
    """Run all discovery queries across one or more date windows and upsert every
    hit; return the unique repo count.

    ``windows`` is either a single ``since`` date (open window, back-compat) or a
    list of ``(since, until)`` tuples — e.g. ``[(week_ago, None), (month_ago,
    week_ago)]`` to scan the last 7 days *and* the prior 8–30 days without the
    two windows fighting over each query's 500-result cap.
    """
    if isinstance(windows, str):
        windows = [(windows, None)]
    seen: set[int] = set()
    # Build every window's queries, then dedupe identical query strings so a
    # window boundary never double-runs the same search.
    queries: list[str] = []
    for since, until in windows:
        queries.extend(build_queries(since, settings, until))
    for query in dict.fromkeys(queries):
        # Up to 5 pages (500 repos) per query; unioned + deduped by repo id.
        # Resilient: a transient GitHub 5xx/timeout retries then skips, never
        # aborting the whole scan.
        items = _search_with_retry(client, query)
        for item in items:
            try:
                repo = _item_to_repo(item)
            except (KeyError, TypeError):
                continue
            db.upsert_repo(conn, repo)
            seen.add(repo["id"])
        db.commit(conn)
    return len(seen)

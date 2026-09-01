"""Daily snapshot collection.

Batches all tracked repos into GraphQL queries (aliased r0..rN) and writes one
snapshot row per repo. If a GraphQL batch fails, falls back to per-repo REST.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ghpulse import db

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import sqlite3

    from ghpulse.http import GitHubClient

_REPO_FIELDS = """
    stargazerCount
    forkCount
    watchers {{ totalCount }}
    issues(states: OPEN) {{ totalCount }}
    releases(last: 1) {{ nodes {{ publishedAt }} }}
    defaultBranchRef {{
      target {{
        ... on Commit {{
          history(since: "{since}") {{ totalCount }}
        }}
      }}
    }}
"""


def _repo_names(conn: "sqlite3.Connection", repo_ids: list[int]) -> dict[int, tuple[str, str]]:
    """Resolve repo_id -> (owner, name) from the repo table."""
    out: dict[int, tuple[str, str]] = {}
    for repo_id in repo_ids:
        row = db.repo_row(conn, repo_id)
        if row is None:
            continue
        full_name = row["full_name"]
        if not full_name or "/" not in full_name:
            continue
        owner, _, name = full_name.partition("/")
        out[repo_id] = (owner, name)
    return out


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_batch_query(batch: list[tuple[int, str, str]], since: str) -> str:
    """Build one GraphQL query with aliases r0..rN for a batch of repos."""
    fields = _REPO_FIELDS.format(since=since)
    parts: list[str] = []
    for i, (_repo_id, owner, name) in enumerate(batch):
        parts.append(
            f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{{fields}}}"
        )
    return "query {\n" + "\n".join(parts) + "\n}"


def _snapshot_from_graphql(
    conn: "sqlite3.Connection", repo_id: int, captured_at: str, node: dict[str, Any]
) -> None:
    releases = (node.get("releases") or {}).get("nodes") or []
    last_release_at = releases[0].get("publishedAt") if releases else None

    commits_7d: int | None = None
    branch = node.get("defaultBranchRef") or {}
    target = branch.get("target") or {}
    history = target.get("history") or {}
    if "totalCount" in history:
        commits_7d = history["totalCount"]

    db.insert_snapshot(
        conn,
        repo_id,
        captured_at,
        stars=node.get("stargazerCount", 0),
        forks=node.get("forkCount", 0),
        subscribers=(node.get("watchers") or {}).get("totalCount"),
        open_issues=(node.get("issues") or {}).get("totalCount"),
        commits_7d=commits_7d,
        last_release_at=last_release_at,
    )


def _snapshot_from_rest(
    conn: "sqlite3.Connection", repo_id: int, captured_at: str, data: dict[str, Any]
) -> None:
    db.insert_snapshot(
        conn,
        repo_id,
        captured_at,
        stars=data.get("stargazers_count", 0),
        forks=data.get("forks_count", 0),
        subscribers=data.get("subscribers_count"),
        open_issues=data.get("open_issues_count"),
    )


def snapshot_all(
    client: "GitHubClient",
    conn: "sqlite3.Connection",
    captured_at: str,
    batch_size: int = 50,
) -> int:
    """Snapshot every tracked repo at ``captured_at``; return snapshot count.

    Uses GraphQL batches of ``batch_size`` repos; on a batch-level GraphQL
    failure, falls back to per-repo REST for that batch.
    """
    repo_ids = db.tracked_repo_ids(conn)
    names = _repo_names(conn, repo_ids)
    targets: list[tuple[int, str, str]] = [
        (rid, names[rid][0], names[rid][1]) for rid in repo_ids if rid in names
    ]

    since_dt = _parse_iso(captured_at) - timedelta(days=7)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    count = 0
    for start in range(0, len(targets), batch_size):
        batch = targets[start : start + batch_size]
        data: dict[str, Any] | None
        try:
            # partial_ok: a single NOT_FOUND alias (renamed/deleted/private
            # repo) must not abort the whole batch — GitHub still returns data
            # for the aliases that resolved.
            data = client.graphql(_build_batch_query(batch, since), partial_ok=True)
        except Exception:
            data = None

        missing: list[tuple[int, str, str]] = []
        if data is None:
            # Whole batch failed at the transport/query level; REST every repo.
            missing = list(batch)
        else:
            for i, (repo_id, owner, name) in enumerate(batch):
                node = data.get(f"r{i}")
                if not node:
                    missing.append((repo_id, owner, name))
                    continue
                _snapshot_from_graphql(conn, repo_id, captured_at, node)
                count += 1

        # Per-repo REST fallback only for the aliases GraphQL could not fill.
        # follow_redirects=True lets a renamed repo's 301 resolve to its new
        # location instead of yielding an unparseable body.
        for repo_id, owner, name in missing:
            try:
                rest = client.get(f"/repos/{owner}/{name}")
            except Exception:
                continue
            if isinstance(rest, dict):
                _snapshot_from_rest(conn, repo_id, captured_at, rest)
                count += 1

        db.commit(conn)

    return count

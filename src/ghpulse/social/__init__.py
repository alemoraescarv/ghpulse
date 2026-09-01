"""Social-media hype layer: fetch cross-platform mentions of tracked repos.

Each platform lives in its own module behind the `SocialSource` protocol, so
adding or removing one is a single file. Sources run only for the top-N GitHub
repos (rate-limit etiquette) and each is independently skippable — a dead API
never breaks the run, it just drops that platform's contribution, logged.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from .. import db
from .base import Mention, SocialSource, make_client
from .hackernews import HackerNews
from .lobsters import Lobsters
from .reddit import Reddit

log = logging.getLogger("ghpulse.social")

__all__ = [
    "Mention",
    "SocialSource",
    "HackerNews",
    "Lobsters",
    "Reddit",
    "default_sources",
    "fetch_social",
]


def default_sources(settings: Any) -> list[SocialSource]:
    """The MVP source set: HN + Lobsters (zero-auth) + Reddit (OAuth).

    Reddit is included but disables itself when no credentials are configured.
    Bluesky was dropped from the default set (it 403s at scale); its module is
    still on disk but is never queried.
    """
    return [
        HackerNews(),
        Lobsters(),
        Reddit(
            client_id=getattr(settings, "reddit_client_id", None),
            client_secret=getattr(settings, "reddit_client_secret", None),
        ),
    ]


def _top_repo_rows(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Top-N tracked repos by most recent star count (a cheap momentum proxy)."""
    return conn.execute(
        """
        SELECT r.* FROM repo r
        JOIN (
          SELECT s.repo_id, s.stars,
                 ROW_NUMBER() OVER (PARTITION BY s.repo_id ORDER BY s.captured_at DESC) AS rn
          FROM snapshot s
        ) latest ON latest.repo_id = r.id AND latest.rn = 1
        WHERE r.is_tracked = 1
        ORDER BY latest.stars DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def fetch_social(
    conn: sqlite3.Connection,
    sources: list[SocialSource] | None,
    settings: Any,
    limit: int = 200,
) -> int:
    """Fetch and store social mentions for the top-N tracked repos.

    Returns the number of mentions inserted. Never raises for a single
    source/repo failure — those are logged and skipped.
    """
    if sources is None:
        sources = default_sources(settings)
    active = [s for s in sources if s.enabled(settings)]
    if not active:
        log.info("no enabled social sources")
        return 0

    repos = _top_repo_rows(conn, limit)
    captured_at = db.now_iso()
    inserted = 0
    with make_client() as client:
        for repo_row in repos:
            for source in active:
                try:
                    mentions = source.search(client, repo_row)
                except Exception as exc:  # pragma: no cover - defensive belt-and-braces
                    log.warning("source %s crashed on %s: %s", source.name, repo_row["full_name"], exc)
                    continue
                for m in mentions:
                    db.insert_mention(
                        conn,
                        repo_id=m.repo_id,
                        platform=m.platform,
                        post_id=m.post_id,
                        url=m.url,
                        title=m.title,
                        engagement=m.engagement,
                        posted_at=m.posted_at,
                        captured_at=captured_at,
                    )
                    inserted += 1
    db.commit(conn)
    log.info("social: inserted %d mentions across %d repos", inserted, len(repos))
    return inserted

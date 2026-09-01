"""Hacker News source via the Algolia HN Search API (no auth).

https://hn.algolia.com/api/v1/search?query=<term>  — returns points, comment
counts, and timestamps. Best free signal in the layer.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .base import Mention, SocialSource, WINDOW_DAYS, get_json, iso_utc, repo_search_terms

_API = "https://hn.algolia.com/api/v1/search"


class HackerNews(SocialSource):
    name = "hn"
    needs_auth = False

    def search(self, client: httpx.Client, repo_row: Any) -> list[Mention]:
        repo_id = int(repo_row["id"])
        full_name, repo_url, name = repo_search_terms(repo_row)
        cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
        cutoff_epoch = int(cutoff.timestamp())

        mentions: dict[str, Mention] = {}
        # Exact repo URL is the strong signal; bare name is a weak fallback.
        for query in (repo_url, name):
            payload = get_json(
                client,
                _API,
                params={
                    "query": query,
                    "tags": "(story,comment)",
                    "numericFilters": f"created_at_i>{cutoff_epoch}",
                    "hitsPerPage": 50,
                },
                context=f"hn:{full_name}",
            )
            if not payload:
                continue
            for hit in payload.get("hits") or []:
                m = _to_mention(repo_id, hit)
                if m is not None:
                    mentions[m.post_id] = m
        return list(mentions.values())


def _to_mention(repo_id: int, hit: dict[str, Any]) -> Mention | None:
    post_id = hit.get("objectID")
    if not post_id:
        return None
    created_i = hit.get("created_at_i")
    if created_i is not None:
        try:
            posted = datetime.fromtimestamp(int(created_i), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    else:
        created_at = hit.get("created_at")
        if not created_at:
            return None
        try:
            posted = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except ValueError:
            return None
    points = int(hit.get("points") or 0)
    comments = int(hit.get("num_comments") or 0)
    title = hit.get("title") or hit.get("story_title") or hit.get("comment_text")
    return Mention(
        repo_id=repo_id,
        platform="hn",
        post_id=str(post_id),
        url=f"https://news.ycombinator.com/item?id={post_id}",
        title=(str(title)[:200] if title else None),
        engagement=points + comments,
        posted_at=iso_utc(posted),
    )

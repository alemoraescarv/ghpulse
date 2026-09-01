"""Lobsters source via lobste.rs search.json (no auth).

https://lobste.rs/search.json?q=<term>&what=stories&order=newest
Small but high-signal-per-post dev audience.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .base import Mention, SocialSource, WINDOW_DAYS, get_json, iso_utc, repo_search_terms

_API = "https://lobste.rs/search.json"


class Lobsters(SocialSource):
    name = "lobsters"
    needs_auth = False

    def search(self, client: httpx.Client, repo_row: Any) -> list[Mention]:
        repo_id = int(repo_row["id"])
        full_name, repo_url, name = repo_search_terms(repo_row)
        cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

        mentions: dict[str, Mention] = {}
        for query in (repo_url, name):
            payload = get_json(
                client,
                _API,
                params={"q": query, "what": "stories", "order": "newest"},
                context=f"lobsters:{full_name}",
            )
            if not payload:
                continue
            # search.json historically returns a bare list; tolerate {"stories": [...]}.
            stories = payload if isinstance(payload, list) else payload.get("stories") or []
            for story in stories:
                m = _to_mention(repo_id, story, cutoff)
                if m is not None:
                    mentions[m.post_id] = m
        return list(mentions.values())


def _to_mention(repo_id: int, story: dict[str, Any], cutoff: datetime) -> Mention | None:
    if not isinstance(story, dict):
        return None
    short_id = story.get("short_id")
    if not short_id:
        return None
    created_at = story.get("created_at")
    if not created_at:
        return None
    try:
        posted = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    if posted < cutoff:
        return None
    engagement = int(story.get("score") or 0) + int(story.get("comment_count") or 0)
    url = story.get("short_id_url") or story.get("comments_url")
    title = story.get("title")
    return Mention(
        repo_id=repo_id,
        platform="lobsters",
        post_id=str(short_id),
        url=str(url) if url else None,
        title=(str(title)[:200] if title else None),
        engagement=engagement,
        posted_at=iso_utc(posted),
    )

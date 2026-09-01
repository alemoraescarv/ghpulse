"""Bluesky source via the public AT Protocol API (no auth for public reads).

https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=<term>
The modern X replacement for dev chatter — free and searchable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .base import Mention, SocialSource, WINDOW_DAYS, get_json, iso_utc, repo_search_terms

_API = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"


class Bluesky(SocialSource):
    name = "bluesky"
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
                params={"q": query, "limit": 25, "sort": "top"},
                context=f"bluesky:{full_name}",
            )
            if not payload:
                continue
            for post in payload.get("posts") or []:
                m = _to_mention(repo_id, post, cutoff)
                if m is not None:
                    mentions[m.post_id] = m
        return list(mentions.values())


def _to_mention(repo_id: int, post: dict[str, Any], cutoff: datetime) -> Mention | None:
    uri = post.get("uri")
    if not uri:
        return None
    record = post.get("record") or {}
    created_at = record.get("createdAt")
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
    engagement = (
        int(post.get("likeCount") or 0)
        + int(post.get("repostCount") or 0)
        + int(post.get("replyCount") or 0)
    )
    text = record.get("text")
    handle = (post.get("author") or {}).get("handle")
    rkey = str(uri).rsplit("/", 1)[-1]
    url = f"https://bsky.app/profile/{handle}/post/{rkey}" if handle else None
    return Mention(
        repo_id=repo_id,
        platform="bluesky",
        post_id=str(uri),
        url=url,
        title=(str(text)[:200] if text else None),
        engagement=engagement,
        posted_at=iso_utc(posted),
    )

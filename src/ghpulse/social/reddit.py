"""Reddit source via an OAuth app (REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET).

If credentials are absent the source disables itself gracefully and returns [].
With creds it authenticates (client-credentials grant) and searches for the repo
URL across Reddit. https://oauth.reddit.com/search.json?q=url:<repo_url>
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .base import (
    Mention,
    SocialSource,
    USER_AGENT,
    WINDOW_DAYS,
    get_json,
    iso_utc,
    repo_search_terms,
)

log = logging.getLogger("ghpulse.social")

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_SEARCH_URL = "https://oauth.reddit.com/search"


class Reddit(SocialSource):
    name = "reddit"
    needs_auth = True

    def __init__(self, client_id: str | None = None, client_secret: str | None = None) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._token_failed = False

    def enabled(self, settings: Any) -> bool:
        cid = self.client_id or getattr(settings, "reddit_client_id", None)
        secret = self.client_secret or getattr(settings, "reddit_client_secret", None)
        return bool(cid and secret)

    def _access_token(self, client: httpx.Client) -> str | None:
        if self._token is not None or self._token_failed:
            return self._token
        if not (self.client_id and self.client_secret):
            self._token_failed = True
            return None
        try:
            resp = client.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self.client_id, self.client_secret),
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code >= 400:
                log.warning("reddit token HTTP %s", resp.status_code)
                self._token_failed = True
                return None
            self._token = str(resp.json().get("access_token")) or None
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("reddit token failed: %s", exc)
            self._token_failed = True
            return None
        if not self._token:
            self._token_failed = True
        return self._token

    def search(self, client: httpx.Client, repo_row: Any) -> list[Mention]:
        token = self._access_token(client)
        if not token:
            return []
        repo_id = int(repo_row["id"])
        full_name, repo_url, name = repo_search_terms(repo_row)
        cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
        headers = {"Authorization": f"bearer {token}", "User-Agent": USER_AGENT}

        mentions: dict[str, Mention] = {}
        for query in (f"url:{repo_url}", name):
            payload = get_json(
                client,
                _SEARCH_URL,
                params={"q": query, "limit": 25, "sort": "top", "t": "week"},
                headers=headers,
                context=f"reddit:{full_name}",
            )
            if not payload:
                continue
            children = (payload.get("data") or {}).get("children") or []
            for child in children:
                m = _to_mention(repo_id, child.get("data") or {}, cutoff)
                if m is not None:
                    mentions[m.post_id] = m
        return list(mentions.values())


def _to_mention(repo_id: int, data: dict[str, Any], cutoff: datetime) -> Mention | None:
    post_id = data.get("id")
    if not post_id:
        return None
    created_utc = data.get("created_utc")
    if created_utc is None:
        return None
    try:
        posted = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None
    if posted < cutoff:
        return None
    engagement = int(data.get("score") or 0) + int(data.get("num_comments") or 0)
    permalink = data.get("permalink")
    url = f"https://www.reddit.com{permalink}" if permalink else data.get("url")
    title = data.get("title")
    return Mention(
        repo_id=repo_id,
        platform="reddit",
        post_id=str(post_id),
        url=str(url) if url else None,
        title=(str(title)[:200] if title else None),
        engagement=engagement,
        posted_at=iso_utc(posted),
    )

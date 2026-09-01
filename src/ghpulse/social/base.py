"""Shared types and helpers for social sources.

A `SocialSource` knows how to search one platform for posts mentioning a repo
and return normalized `Mention` records. Every source is independently
skippable: any network/API failure is logged and yields an empty list so a dead
API never breaks the weekly run.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("ghpulse.social")

# Public reads are lenient, but be a good citizen with a descriptive UA.
USER_AGENT = "ghpulse/0.1 (+https://github.com/ghpulse; social-hype-layer)"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)

# Only mentions from the last WINDOW_DAYS count toward hype (weekly edition).
WINDOW_DAYS = 7


@dataclass
class Mention:
    """One normalized social post referencing a tracked repo."""

    repo_id: int
    platform: str
    post_id: str
    url: str | None
    title: str | None
    engagement: int
    posted_at: str  # ISO-8601 UTC


def make_client() -> httpx.Client:
    """A shared httpx client for social sources (descriptive UA, redirects on)."""
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


def repo_search_terms(repo_row: Any) -> tuple[str, str, str]:
    """Return (full_name, repo_url, name) search anchors for a repo row.

    - full_name  e.g. 'astral-sh/uv'
    - repo_url   e.g. 'github.com/astral-sh/uv'  (the exact join key)
    - name       e.g. 'uv'                        (weaker fallback)
    """
    full_name = str(repo_row["full_name"])
    repo_url = f"github.com/{full_name}"
    name = full_name.split("/", 1)[-1]
    return full_name, repo_url, name


def iso_utc(dt: datetime) -> str:
    """Normalize a datetime to an ISO-8601 UTC string (second precision)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def get_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    context: str = "",
) -> Any | None:
    """GET *url* and parse JSON, returning None on ANY network/API/parse error.

    This is the single choke point that makes every source safe: callers treat
    None as "this platform contributed nothing this run" and move on.
    """
    try:
        resp = client.get(url, params=params, headers=headers)
        if resp.status_code >= 400:
            log.warning("social GET %s -> HTTP %s (%s)", url, resp.status_code, context)
            return None
        return resp.json()
    except (httpx.HTTPError, ValueError) as exc:  # ValueError covers JSON decode
        log.warning("social GET %s failed (%s): %s", url, context, exc)
        return None


class SocialSource(ABC):
    """A searchable social platform. One subclass per platform, one file each."""

    name: str = "base"
    needs_auth: bool = False

    def enabled(self, settings: Any) -> bool:  # noqa: D401 - simple predicate
        """Whether this source can run given the current settings.

        Zero-auth sources are always enabled; auth sources override this to
        disable themselves gracefully when credentials are absent.
        """
        return True

    @abstractmethod
    def search(self, client: httpx.Client, repo_row: Any) -> list[Mention]:
        """Return mentions of *repo_row* from the last WINDOW_DAYS. Never raises."""
        raise NotImplementedError

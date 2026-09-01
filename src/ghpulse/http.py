"""Synchronous GitHub API client for ghpulse.

Wraps httpx.Client with:
  - REST GET (core family), with optional ETag caching into the http_cache
    table when a sqlite3 connection is provided,
  - repository search with pagination (search family),
  - GraphQL POST (graphql family),
  - /rate_limit inspection.

Never requires a token at construction time; auth-required calls raise a
clear RuntimeError when no token is available.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

import httpx

from .ratelimit import RateLimiter

BASE_URL = "https://api.github.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class GitHubClient:
    """Thin sync client over the GitHub REST and GraphQL APIs."""

    def __init__(
        self,
        token: str | None,
        limiter: RateLimiter | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        self.token = token
        self.limiter = limiter or RateLimiter()
        self.conn = conn
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "ghpulse",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=BASE_URL,
            headers=headers,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitHubClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _require_token(self, what: str) -> None:
        if not self.token:
            raise RuntimeError(
                f"A GitHub token is required for {what}. Set GITHUB_TOKEN in the "
                "environment or in ~/.config/ghpulse/env (GITHUB_TOKEN=...), "
                "or use `ghpulse demo` for an offline demo."
            )

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any] | None) -> str:
        if not params:
            return path
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{path}?{qs}"

    def _cache_lookup(self, key: str) -> tuple[str, str] | None:
        """Return (etag, body) for a cached URL, or None."""
        if self.conn is None:
            return None
        try:
            row = self.conn.execute(
                "SELECT etag, body FROM http_cache WHERE url = ?", (key,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        etag, body = row[0], row[1]
        if etag is None or body is None:
            return None
        return str(etag), str(body)

    def _cache_store(self, key: str, etag: str | None, body: str) -> None:
        if self.conn is None or not etag:
            return
        fetched_at = datetime.now(timezone.utc).isoformat()
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO http_cache (url, etag, body, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                (key, etag, body, fetched_at),
            )
            self.conn.commit()
        except sqlite3.Error:
            # Cache is best-effort; never fail a request over it.
            pass

    @staticmethod
    def _raise_for_status(resp: httpx.Response, context: str) -> None:
        if resp.status_code < 400:
            return
        if resp.status_code in (401, 403):
            # 403 is also GitHub's secondary rate-limit signal; surface that
            # distinctly so callers don't mistake throttling for bad creds.
            remaining = resp.headers.get("x-ratelimit-remaining")
            if resp.status_code == 403 and remaining == "0":
                raise RuntimeError(
                    f"GitHub rate limit exhausted (403) during {context}. "
                    "Wait for the reset window or reduce request volume."
                )
            raise RuntimeError(
                f"GitHub rejected credentials ({resp.status_code}) during "
                f"{context}. Check GITHUB_TOKEN."
            )
        detail = ""
        try:
            payload = resp.json()
            detail = payload.get("message", "") if isinstance(payload, dict) else ""
        except (json.JSONDecodeError, ValueError):
            detail = resp.text[:200]
        raise RuntimeError(
            f"GitHub API error {resp.status_code} during {context}: {detail}"
        )

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Core REST GET, e.g. get('/repos/octocat/hello-world').

        Uses conditional requests (ETag) backed by the http_cache table when
        a database connection was provided.
        """
        if not path.startswith("/"):
            path = "/" + path
        key = self._cache_key(path, params)
        cached = self._cache_lookup(key)

        headers: dict[str, str] = {}
        if cached is not None:
            headers["If-None-Match"] = cached[0]

        self.limiter.before("core")
        resp = self._client.get(path, params=params, headers=headers or None)
        self.limiter.update_from_headers("core", resp.headers)

        if resp.status_code == 304 and cached is not None:
            return json.loads(cached[1])

        self._raise_for_status(resp, f"GET {path}")
        body_text = resp.text
        self._cache_store(key, resp.headers.get("etag"), body_text)
        return resp.json()

    def search_repositories(
        self,
        q: str,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 100,
        max_pages: int = 3,
    ) -> list[dict]:
        """Paginate /search/repositories for query *q*.

        Stops at max_pages, at an empty page, or when GitHub's 1000-result
        search window is exhausted.  Returns the raw item dicts.
        """
        items: list[dict] = []
        for page in range(1, max_pages + 1):
            params: dict[str, Any] = {
                "q": q,
                "sort": sort,
                "order": order,
                "per_page": per_page,
                "page": page,
            }
            self.limiter.before("search")
            resp = self._client.get("/search/repositories", params=params)
            self.limiter.update_from_headers("search", resp.headers)

            if resp.status_code == 422:
                # Past the end of the search window; treat as exhausted.
                break
            self._raise_for_status(resp, f"search q={q!r} page={page}")

            payload = resp.json()
            page_items = payload.get("items") or []
            if not page_items:
                break
            items.extend(page_items)

            total = payload.get("total_count", 0)
            if len(items) >= min(int(total), 1000):
                break
        return items

    def search_page(
        self,
        q: str,
        page: int = 1,
        per_page: int = 20,
        sort: str = "best-match",
    ) -> list[dict]:
        """Fetch ONE page of /search/repositories for query *q*.

        Unlike :meth:`search_repositories` this never loops pages — the caller
        drives pagination. Reuses the same request + rate-limit path. Returns the
        raw item dicts (an empty list past the search window / on an empty page).
        """
        s = str(sort or "best-match").strip().lower()
        # "best match" is expressed to the API as an empty sort.
        api_sort = "" if s in ("best-match", "best match", "") else s
        params: dict[str, Any] = {
            "q": q,
            "per_page": int(per_page),
            "page": int(page),
        }
        if api_sort:
            params["sort"] = api_sort
            params["order"] = "desc"

        self.limiter.before("search")
        resp = self._client.get("/search/repositories", params=params)
        self.limiter.update_from_headers("search", resp.headers)

        if resp.status_code == 422:
            # Past the end of the search window; treat as exhausted.
            return []
        self._raise_for_status(resp, f"search_page q={q!r} page={page}")
        payload = resp.json()
        return payload.get("items") or []

    def graphql(
        self, query: str, variables: dict | None = None, *, partial_ok: bool = False
    ) -> dict:
        """POST to /graphql; returns the `data` dict, raising on errors.

        GitHub returns partial `data` alongside an `errors` entry when only some
        aliases fail (e.g. a NOT_FOUND for a renamed/deleted/private repo). When
        ``partial_ok`` is True and a usable `data` object is present, the partial
        result is returned instead of discarding the whole response; callers can
        then handle the missing aliases individually.
        """
        self._require_token("GraphQL queries")

        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        self.limiter.before("graphql")
        resp = self._client.post("/graphql", json=payload)
        self.limiter.update_from_headers("graphql", resp.headers)
        self._raise_for_status(resp, "GraphQL request")

        body = resp.json()
        errors = body.get("errors")
        data = body.get("data")
        if errors:
            if partial_ok and isinstance(data, dict):
                return data
            messages = "; ".join(
                str(e.get("message", e)) for e in errors if isinstance(e, dict)
            ) or str(errors)
            raise RuntimeError(f"GraphQL errors: {messages}")
        if not isinstance(data, dict):
            raise RuntimeError("GraphQL response contained no data object")
        return data

    def rate_limit(self) -> dict:
        """GET /rate_limit (does not count against any budget)."""
        resp = self._client.get("/rate_limit")
        self._raise_for_status(resp, "GET /rate_limit")
        return resp.json()

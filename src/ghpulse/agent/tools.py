"""Agent tools: read-only GitHub research callables + their JSON schemas.

Each tool is a plain Python function that takes a live ``GitHubClient`` (from
``ghpulse.http``) plus keyword arguments and returns a **string** — a compact,
model-friendly rendering of the result. Tools are:

  - ``search_repos(query, sort, limit)`` -> compact JSON list of matching repos
  - ``get_repo(full_name)``              -> key fields for one repo
  - ``fetch_readme(full_name)``          -> trimmed README text
  - ``fetch_url(url)``                   -> trimmed text of an arbitrary page

Design rules (important):
  * Every tool is READ-ONLY and fully exception-guarded — it returns an
    ``"error: ..."`` string on any failure and NEVER raises. This keeps the
    tool-calling loop robust against a flaky network or a bad argument the model
    hallucinated.
  * Return values are capped in length so a huge README can't blow up the model
    context window.

The ``TOOL_SCHEMAS`` list is the OpenAI/Ollama function-calling tool format that
qwen2.5 and llama3.1 understand natively. ``execute_tool`` dispatches a
(name, args) pair to the matching callable.
"""

from __future__ import annotations

import base64
import inspect
import ipaddress
import json
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

# Caps so tool output can never flood the model context window.
SEARCH_LIMIT_DEFAULT = 5
SEARCH_LIMIT_MAX = 15
DESC_CAP = 300
README_CAP = 4000
URL_CAP = 4000
URL_TIMEOUT = 20.0

# Common argument aliases small models emit, mapped to the tools' real params.
_ARG_ALIASES = {
    "repo": "full_name",
    "repository": "full_name",
    "name": "full_name",
    "q": "query",
    "search": "query",
}

# Sort values GitHub's repo search accepts (anything else -> "best match").
_VALID_SORTS = {"stars", "forks", "updated", "help-wanted-issues", "best-match"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _trim(text: str, cap: int) -> str:
    text = text or ""
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + " …[truncated]"


def _compact_repo(item: dict[str, Any]) -> dict[str, Any]:
    """Pull the handful of fields worth feeding a model from a repo payload."""
    return {
        "full_name": item.get("full_name"),
        "url": item.get("html_url"),
        "stars": item.get("stargazers_count"),
        "forks": item.get("forks_count"),
        "language": item.get("language"),
        "topics": (item.get("topics") or [])[:8],
        "description": _trim(item.get("description") or "", DESC_CAP),
        "pushed_at": item.get("pushed_at"),
        "open_issues": item.get("open_issues_count"),
        "archived": item.get("archived"),
    }


# --------------------------------------------------------------------------- #
# the tools (each takes the GitHubClient first, returns a string)
# --------------------------------------------------------------------------- #


def search_repos(
    client: Any, query: str, sort: str = "stars", limit: int = SEARCH_LIMIT_DEFAULT
) -> str:
    """Search GitHub repositories; return a compact JSON list (capped)."""
    try:
        if not query or not str(query).strip():
            return "error: search_repos needs a non-empty 'query'."
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = SEARCH_LIMIT_DEFAULT
        n = max(1, min(n, SEARCH_LIMIT_MAX))
        s = str(sort or "stars").strip().lower()
        if s not in _VALID_SORTS:
            s = "stars"
        # "best-match" is expressed to the API as an empty sort.
        api_sort = "" if s == "best-match" else s
        items = client.search_repositories(
            str(query).strip(), sort=api_sort, per_page=min(n, 50), max_pages=1
        )
        compact = [_compact_repo(it) for it in items[:n]]
        if not compact:
            return f"no repositories matched query={query!r}."
        return json.dumps(compact, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - tools never raise
        return f"error: search_repos failed: {exc}"


def get_repo(client: Any, full_name: str = "") -> str:
    """Fetch key fields for one repo (owner/name); return a JSON object."""
    try:
        fn = str(full_name or "").strip().strip("/")
        if fn.count("/") != 1:
            return "error: get_repo needs 'full_name' like 'owner/repo'."
        data = client.get(f"/repos/{fn}")
        if not isinstance(data, dict):
            return f"error: unexpected response for {fn}."
        return json.dumps(_compact_repo(data), ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return f"error: get_repo failed: {exc}"


def fetch_readme(client: Any, full_name: str = "") -> str:
    """Fetch and decode a repo's README, trimmed to a safe length."""
    try:
        fn = str(full_name or "").strip().strip("/")
        if fn.count("/") != 1:
            return "error: fetch_readme needs 'full_name' like 'owner/repo'."
        data = client.get(f"/repos/{fn}/readme")
        if not isinstance(data, dict):
            return f"error: unexpected README response for {fn}."
        content = data.get("content") or ""
        encoding = data.get("encoding") or ""
        if encoding == "base64":
            try:
                text = base64.b64decode(content).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return f"error: could not decode README for {fn}."
        else:
            text = str(content)
        text = text.strip()
        if not text:
            return f"{fn} has an empty README."
        return _trim(text, README_CAP)
    except Exception as exc:  # noqa: BLE001
        return f"error: fetch_readme failed: {exc}"


def _is_blocked_host(host: str) -> bool:
    """True for loopback / private / link-local / internal hosts (SSRF guard)."""
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return True
    if h in {"localhost"} or h.endswith(".local") or h.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except ValueError:
        # Not an IP literal (a normal hostname) — allow; blocked names handled above.
        return False


def fetch_url(client: Any, url: str = "") -> str:
    """Fetch an arbitrary URL's text (for researching a finding), trimmed.

    Uses a plain httpx GET (not the GitHub client) so the model can read a
    project's homepage or docs. Read-only and exception-guarded. Private,
    loopback, and link-local hosts are refused so the model cannot be steered
    at localhost services (e.g. Ollama) or cloud metadata endpoints.
    """
    try:
        u = str(url or "").strip()
        if not (u.startswith("http://") or u.startswith("https://")):
            return "error: fetch_url needs an http(s) 'url'."
        if _is_blocked_host(urlparse(u).hostname or ""):
            return "error: fetch_url refuses private/loopback/link-local hosts."
        resp = httpx.get(
            u,
            timeout=URL_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "ghpulse-agent"},
        )
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype and "json" not in ctype:
            return f"skipped: {u} is {ctype or 'non-text'}, not readable text."
        return _trim(resp.text, URL_CAP)
    except Exception as exc:  # noqa: BLE001
        return f"error: fetch_url failed: {exc}"


# --------------------------------------------------------------------------- #
# dispatch registry + JSON schemas (OpenAI/Ollama tool format)
# --------------------------------------------------------------------------- #

TOOLS: dict[str, Callable[..., str]] = {
    "search_repos": search_repos,
    "get_repo": get_repo,
    "fetch_readme": fetch_readme,
    "fetch_url": fetch_url,
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_repos",
            "description": (
                "Search GitHub for repositories matching a query. Returns a "
                "compact JSON list with full_name, stars, language, topics and "
                "description. Use this FIRST to find candidates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "GitHub search query, e.g. 'vector database rust' or "
                            "'language:python topic:llm'."
                        ),
                    },
                    "sort": {
                        "type": "string",
                        "enum": sorted(_VALID_SORTS),
                        "description": "Ranking; 'stars' is a good default.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": f"How many results (1-{SEARCH_LIMIT_MAX}).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_repo",
            "description": (
                "Fetch key fields (stars, language, topics, description, last "
                "push, open issues) for one repo given 'owner/repo'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "full_name": {
                        "type": "string",
                        "description": "Repository as 'owner/repo'.",
                    }
                },
                "required": ["full_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_readme",
            "description": (
                "Read a repository's README (trimmed) to research what it does "
                "before advising the user. Give 'owner/repo'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "full_name": {
                        "type": "string",
                        "description": "Repository as 'owner/repo'.",
                    }
                },
                "required": ["full_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch the readable text of an http(s) URL (e.g. a project's "
                "homepage or docs) to research a finding. Text/HTML/JSON only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "An http(s) URL to read.",
                    }
                },
                "required": ["url"],
            },
        },
    },
]


def execute_tool(name: str, args: dict[str, Any] | None, client: Any) -> str:
    """Dispatch a tool call to its callable; always returns a string."""
    func = TOOLS.get(name)
    if func is None:
        return f"error: unknown tool {name!r}."
    raw = dict(args or {})
    # Small models often name args imperfectly — accept common aliases and drop
    # any keys the tool doesn't take, so a malformed call hits the tool's own
    # friendly guard (e.g. "needs full_name") instead of a raw TypeError.
    for alias, canonical in _ARG_ALIASES.items():
        if alias in raw and canonical not in raw:
            raw[canonical] = raw[alias]
    try:
        accepted = {p for p in inspect.signature(func).parameters if p != "client"}
    except (TypeError, ValueError):
        accepted = set(raw)
    kwargs = {k: v for k, v in raw.items() if k in accepted}
    try:
        return func(client, **kwargs)
    except TypeError as exc:
        # Bad/extra arguments from the model — degrade instead of raising.
        return f"error: bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001 - belt and suspenders
        return f"error: {name} failed: {exc}"

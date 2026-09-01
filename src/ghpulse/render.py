"""Render weekly editions to static, self-contained HTML pages."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape
from markupsafe import Markup

from . import db, hype, score, tags

_EDITION_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_LANG_COLORS: dict[str, str] = {
    "python": "#3572A5",
    "typescript": "#3178C6",
    "javascript": "#F1E05A",
    "rust": "#DEA584",
    "go": "#00ADD8",
    "c++": "#F34B7D",
    "c": "#555555",
    "zig": "#EC915C",
    "swift": "#F05138",
    "kotlin": "#A97BFF",
    "java": "#B07219",
    "ruby": "#701516",
    "c#": "#178600",
    "shell": "#89E051",
    "html": "#E34C26",
    "css": "#563D7C",
}
_DEFAULT_LANG_COLOR = "#8B949E"


def lang_color(language: str | None) -> str:
    """Return a GitHub-ish accent color for a language name."""
    if not language:
        return _DEFAULT_LANG_COLOR
    return _LANG_COLORS.get(str(language).lower(), _DEFAULT_LANG_COLOR)


def compact_number(value: Any) -> str:
    """Format 12345 as '12.3k' for star counts and similar."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else ""
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{int(n)}"


def sparkline_svg(spark: Any, width: int = 120, height: int = 28) -> Markup:
    """Build an inline SVG sparkline from a list of ints. Empty-safe."""
    try:
        values = [float(v) for v in (spark or [])]
    except (TypeError, ValueError):
        values = []
    # A 2-point spark is just a straight diagonal — meaningless and it reads as
    # a placeholder. Only draw a real trend line once >=3 snapshots exist.
    if len(values) < 3:
        return Markup("")
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pad = 2.5
    inner_w = width - 2 * pad
    inner_h = height - 2 * pad
    step = inner_w / (len(values) - 1)
    pts: list[tuple[float, float]] = []
    for i, v in enumerate(values):
        x = pad + i * step
        y = pad + inner_h * (1.0 - (v - lo) / span)
        pts.append((round(x, 1), round(y, 1)))
    points = " ".join(f"{x},{y}" for x, y in pts)
    last_x, last_y = pts[-1]
    svg = (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}"'
        f' preserveAspectRatio="none" role="img" aria-hidden="true">'
        f'<polyline fill="none" stroke="currentColor" stroke-width="1.5"'
        f' stroke-linecap="round" stroke-linejoin="round" points="{points}"/>'
        f'<circle cx="{last_x}" cy="{last_y}" r="2" fill="currentColor"/>'
        f"</svg>"
    )
    return Markup(svg)


_MD_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LEADING_JUNK_RE = re.compile(r"^[\s\W_]+", re.UNICODE)

_NO_DESC = "No description provided."


def harden_description(description: Any, limit: int = 140) -> str:
    """Clean a repo description for display. Deterministic and empty-safe.

    Strips leading emoji/badges/markdown, collapses whitespace, and truncates at
    roughly ``limit`` chars on a word boundary with an ellipsis. Falls back to a
    fixed placeholder when nothing usable remains.
    """
    if not description:
        return _NO_DESC
    text = str(description)
    # Drop markdown images/badges entirely; keep the link text of md links.
    text = _MD_IMG_RE.sub(" ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    # Collapse all whitespace runs to single spaces.
    text = " ".join(text.split())
    # Strip a leading run of emoji / punctuation / markdown markers.
    text = _LEADING_JUNK_RE.sub("", text)
    text = text.strip()
    if not text:
        return _NO_DESC
    if len(text) > limit:
        cut = text[:limit].rstrip()
        # Back up to the last word boundary so we never split a word.
        if " " in cut:
            cut = cut[: cut.rfind(" ")].rstrip()
        text = cut + "…"
    return text or _NO_DESC


# ---------------------------------------------------------------------------
# News view — deterministic, offline blurbs derived from the same sections
# ---------------------------------------------------------------------------

_PCT_RE = re.compile(r"[-+]?\d+")


def _growth_class(pct: int | None) -> str:
    """Map a signed percent to an up/down/flat CSS class."""
    if pct is None:
        return "flat"
    if pct > 0:
        return "up"
    if pct < 0:
        return "down"
    return "flat"


def _growth_pill(pct: int | None, window: str, title: str) -> dict[str, Any] | None:
    """Build one ▲/▼ growth pill dict, or None when the window is absent."""
    if pct is None:
        return None
    cls = _growth_class(pct)
    sym = "▲" if pct > 0 else ("▼" if pct < 0 else "▬")
    return {
        "cls": cls,
        "label": f"{sym} {abs(pct)}% {window}",
        "title": title,
    }


def build_metric_pills(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the ordered metric-pill row for a card. Never emits absent data.

    Fixed order, max 4: ★ stars, ▲/▼ N% wk, ▲/▼ N% mo, one activity pill.
    """
    pills: list[dict[str, Any]] = []
    stars = item.get("stars")
    if stars is not None:
        pills.append(
            {"cls": "stars", "label": f"★ {compact_number(stars)}", "title": "Total stars"}
        )

    deltas = item.get("deltas") or {}
    wk = _growth_pill(_parse_pct(deltas.get("week")), "wk", "Star growth this week")
    if wk is not None:
        pills.append(wk)
    mo = _growth_pill(_parse_pct(deltas.get("month")), "mo", "Star growth this month")
    if mo is not None:
        pills.append(mo)

    commits = item.get("commits_7d")
    contributors = item.get("contributors_7d")
    try:
        commits_n = int(commits) if commits is not None else 0
    except (TypeError, ValueError):
        commits_n = 0
    try:
        contrib_n = int(contributors) if contributors is not None else 0
    except (TypeError, ValueError):
        contrib_n = 0
    if commits_n >= 1:
        pills.append(
            {"cls": "activity", "label": f"⌁ {commits_n} commits/wk",
             "title": "Commits in the last 7 days"}
        )
    elif contrib_n >= 1:
        pills.append(
            {"cls": "activity", "label": f"👥 {contrib_n} devs/wk",
             "title": "Active contributors in the last 7 days"}
        )
    return pills


# ---------------------------------------------------------------------------
# Leaderboard — data-* metric attributes + selector pills (deterministic)
# ---------------------------------------------------------------------------

# Reader-facing dimension order + labels for the rank selector. Kept in lockstep
# with score.LEADERBOARD_DIMS and the client-side sorter's DIMS map.
_LEADERBOARD_PILLS: list[tuple[str, str]] = [
    ("momentum", "Trending"),
    ("growth", "% growth"),
    ("gained", "Stars gained"),
    ("forks", "Forks"),
    ("commits", "Commits"),
    ("stars", "Most-starred"),
]


def leaderboard_data_attrs(item: dict[str, Any]) -> Markup:
    """Render a leaderboard card's data-* metric attributes.

    Absent attributes mean the metric is genuinely unavailable (e.g. no weekly/
    monthly window yet), so the client-side sorter sinks that card to the bottom
    for that dimension. ``data-tags`` is emitted by the card markup itself and is
    intentionally not repeated here. Pure and exception-safe.
    """
    m = item.get("metrics") or {}
    parts: list[str] = []

    def add(attr: str, val: Any) -> None:
        parts.append(f'{attr}="{val}"')

    add("data-stars", int(m.get("stars") or 0))
    add("data-gained-wk", int(m.get("gained_wk") or 0))
    if m.get("gained_mo") is not None:
        add("data-gained-mo", int(m["gained_mo"]))
    if m.get("growth_wk") is not None:
        add("data-growth-wk", f'{float(m["growth_wk"]):.2f}')
    if m.get("growth_mo") is not None:
        add("data-growth-mo", f'{float(m["growth_mo"]):.2f}')
    add("data-momentum", m.get("momentum", 0))
    add("data-velocity", m.get("star_velocity", 0))
    add("data-forks", int(m.get("forks") or 0))
    add("data-fork-delta-wk", int(m.get("fork_delta_wk") or 0))
    add("data-commits", int(m.get("commits_7d") or 0))
    if m.get("commit_growth_wk") is not None:
        add("data-commit-growth-wk", f'{float(m["commit_growth_wk"]):.2f}')
    return Markup(" ".join(parts))


def leaderboard_pills(dims: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Selector pill descriptors (dim id, label, availability) in display order."""
    dims = dims or {}
    out: list[dict[str, Any]] = []
    for dim, label in _LEADERBOARD_PILLS:
        avail = bool((dims.get(dim) or {}).get("available", True))
        out.append({"dim": dim, "label": label, "available": avail})
    return out


def news_pills(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact pill row for a news item: ★ stars + the weekly growth pill."""
    pills: list[dict[str, Any]] = []
    stars = item.get("stars")
    if stars is not None:
        pills.append(
            {"cls": "stars", "label": f"★ {compact_number(stars)}", "title": "Total stars"}
        )
    wk = _growth_pill(item.get("week_pct"), "wk", "Star growth this week")
    if wk is not None:
        pills.append(wk)
    return pills


def _parse_pct(text: Any) -> int | None:
    """Pull a signed integer percent out of a delta chip like '+14% wk'.

    Returns None when nothing numeric is present. Pure and exception-safe.
    """
    if text is None:
        return None
    match = _PCT_RE.search(str(text))
    if match is None:
        return None
    try:
        return int(match.group(0))
    except (TypeError, ValueError):
        return None


def _news_headline(name: str, week_pct: int | None, month_pct: int | None) -> str:
    """Compose a plain-English headline from the weekly/monthly star growth."""
    if week_pct is not None:
        if week_pct == 0:
            head = f"{name} held steady this week"
        else:
            direction = "up" if week_pct > 0 else "down"
            head = f"{name} is {direction} {abs(week_pct)}% this week"
        if month_pct is not None:
            if month_pct == 0:
                head += " (flat this month)"
            elif month_pct > 0:
                head += f" ({month_pct}% this month)"
            else:
                head += f" (down {abs(month_pct)}% this month)"
        return head
    if month_pct is not None:
        if month_pct == 0:
            return f"{name} held steady this month"
        direction = "up" if month_pct > 0 else "down"
        return f"{name} is {direction} {abs(month_pct)}% this month"
    return f"{name} is on the move this week"


def _news_takeaway(description: Any, limit: int = 160) -> str:
    """One-line takeaway from the repo description (collapsed + trimmed)."""
    if not description:
        return ""
    text = " ".join(str(description).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def build_news_items(sections: Any) -> list[dict[str, Any]]:
    """Turn the top mover of each section into a short, deterministic news blurb.

    Pure and offline (no LLM, no I/O). Accepts either the full metrics dict
    ``{"sections": [...]}`` or a bare list of section dicts. Produces exactly one
    item per top mover (the rank-1 item of each section), de-duplicated by
    ``full_name`` so a repo that tops several sections is only reported once.
    Each blurb carries the weekly % and, when available, the monthly % in its
    headline. Empty-safe: returns ``[]`` when there is nothing to report.
    """
    if isinstance(sections, dict):
        section_list = sections.get("sections") or []
    else:
        section_list = sections or []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for section in section_list:
        section_items = (section or {}).get("items") or []
        if not section_items:
            continue
        top = section_items[0]  # already rank-ordered by score._build_section
        name = top.get("full_name")
        if not name or name in seen:
            continue
        seen.add(name)

        deltas = top.get("deltas") or {}
        week_pct = _parse_pct(deltas.get("week"))
        month_pct = _parse_pct(deltas.get("month"))

        tag_ids = tags.classify(top)
        news_item: dict[str, Any] = {
            "full_name": name,
            "url": top.get("url") or f"https://github.com/{name}",
            "headline": _news_headline(name, week_pct, month_pct),
            "takeaway": _news_takeaway(top.get("description")),
            "language": top.get("language"),
            "stars": top.get("stars"),
            "week_pct": week_pct,
            "month_pct": month_pct,
            "value_label": top.get("value_label"),
            "hype_badges": top.get("hype_badges") or [],
            "spark": list(top.get("spark") or []),
            "section_key": section.get("key"),
            "section_title": section.get("title"),
            "tags": tag_ids,
            "tag_meta": tags.tag_meta(tag_ids),
        }
        news_item["pills"] = news_pills(news_item)
        items.append(news_item)
    return items


# ---------------------------------------------------------------------------
# News view — GROUPED thematic trend stories (deterministic, offline)
# ---------------------------------------------------------------------------

# One short "signal" phrase per topical category, used to close each trend
# paragraph with a sentence about rising developer interest. Deterministic.
_GROUP_SIGNALS: dict[str, str] = {
    "ai-agents": "agent harnesses and tooling",
    "context-rag": "retrieval and long-term memory",
    "llm-infra": "running and serving models locally",
    "ui-ux": "polished, glassy interfaces",
    "dev-tools": "faster developer workflows",
    "web-frontend": "modern web frontends",
    "backend-infra": "backend and API infrastructure",
    "data-ml": "data and machine-learning workflows",
    "security": "security and hardening",
    "databases": "storage and query engines",
    "devops-cloud": "cloud-native operations",
    "lang-compilers": "languages, compilers, and runtimes",
    "mobile": "mobile app development",
    "general": "a broad mix of projects",
}

# Clause separators, longest first, so the "what it does" phrase is the first
# meaningful clause of a focused blurb.
_CLAUSE_SEPS = (" — ", "—", "; ", ";", ": ", ":", ", ", ",")


def _short_name(full_name: Any) -> str:
    """Repo short name (drop the owner/ prefix)."""
    name = str(full_name or "")
    return name.split("/")[-1] if "/" in name else name


def _first_clause(text: Any, max_words: int = 9) -> str:
    """First meaningful clause of a blurb, lowercased for mid-sentence use."""
    full = " ".join(str(text or "").split())
    if not full:
        return ""
    t = full
    for sep in _CLAUSE_SEPS:
        idx = t.find(sep)
        if idx > 0:
            t = t[:idx]
            break
    # A very short leading clause ("Get fast") loses meaning — keep the fuller
    # (word-capped) blurb instead so the named action stays grounded.
    if len(t.split()) < 3:
        t = full
    t = t.strip().rstrip(".!;:,")
    words = t.split()
    if len(words) > max_words:
        t = " ".join(words[:max_words])
    if t:
        t = t[0].lower() + t[1:]
    return t


def _join_natural(parts: list[str]) -> str:
    """Join clauses with commas and a trailing 'and' — Oxford style."""
    parts = [p for p in parts if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def _group_paragraph(label: str, tag_id: str, count: int, repos: list[dict[str, Any]]) -> str:
    """Compose a deterministic 2-3 sentence trend story naming the top repos.

    Grounded: only repos actually in the group are named, each paired with what
    it DOES (the first clause of its focused blurb). Wording varies by count
    bucket but carries no randomness.
    """
    label_l = label  # keep the category's proper casing (e.g. "AI Agents")
    if count >= 5:
        lead = f"A strong week for {label_l}: {count} projects are trending."
        trend = "keeps climbing"
    elif count >= 3:
        lead = f"{count} {label_l} projects are trending this week."
        trend = "is picking up"
    else:
        lead = f"Steady interest in {label_l} — {count} projects are trending this week."
        trend = "is holding steady"

    parts: list[str] = []
    for repo in repos[:3]:
        clause = _first_clause(repo.get("description"))
        short = _short_name(repo.get("full_name"))
        if clause and short:
            parts.append(f"{clause} ({short})")
        elif short:
            parts.append(short)
    named = _join_natural(parts)
    named_sentence = ""
    if named:
        named_sentence = named[0].upper() + named[1:] + "."

    phrase = _GROUP_SIGNALS.get(tag_id, "this space")
    signal = f"Developer interest in {phrase} {trend}."

    return " ".join(s for s in (lead, named_sentence, signal) if s)


def _group_repo(item: dict[str, Any], week_pct: int | None) -> dict[str, Any]:
    """Compact per-repo row carried inside a news group."""
    full_name = item.get("full_name")
    repo: dict[str, Any] = {
        "full_name": full_name,
        "url": item.get("url") or (f"https://github.com/{full_name}" if full_name else "#"),
        "description": item.get("description"),
        "language": item.get("language"),
        "stars": item.get("stars"),
        "week_pct": week_pct,
        "deltas": item.get("deltas") or {},
        "tags": item.get("tags") or [],
        "tag_meta": item.get("tag_meta") or [],
        "hype_badges": item.get("hype_badges") or [],
    }
    repo["pills"] = news_pills(repo)
    return repo


# ---------------------------------------------------------------------------
# News view — ranking-based trend groups (forks / commits / % growth)
# ---------------------------------------------------------------------------

# How many repos each ranking trend group lists (kept compact).
_RANKING_GROUP_SIZE = 6


def _item_metric(item: dict[str, Any], key: str) -> Any:
    """Read one value out of a card's ``metrics`` bag, tolerating absence."""
    return (item.get("metrics") or {}).get(key)


def _rank_repo(item: dict[str, Any], metric_pill: dict[str, Any]) -> dict[str, Any]:
    """Compact per-repo row for a ranking group: relevant metric + stars pills."""
    full_name = item.get("full_name")
    stars = item.get("stars")
    pills: list[dict[str, Any]] = [metric_pill]
    if stars is not None:
        pills.append(
            {"cls": "stars", "label": f"★ {compact_number(stars)}", "title": "Total stars"}
        )
    return {
        "full_name": full_name,
        "url": item.get("url") or (f"https://github.com/{full_name}" if full_name else "#"),
        "description": item.get("description"),
        "language": item.get("language"),
        "stars": stars,
        "tags": item.get("tags") or [],
        "tag_meta": item.get("tag_meta") or [],
        "hype_badges": item.get("hype_badges") or [],
        "pills": pills,
    }


def _ranking_group(
    tag_id: str,
    emoji: str,
    label: str,
    lead: str,
    candidates: list[tuple[dict[str, Any], float]],
    metric_pill_fn: Any,
    metric_phrase_fn: Any,
) -> dict[str, Any] | None:
    """Build one ranking trend group, or None when fewer than 2 repos qualify.

    ``candidates`` are ``(item, value)`` pairs already filtered to repos that
    genuinely carry the metric. Deterministic: sorted by value desc then name.
    """
    if len(candidates) < 2:
        return None
    ordered = sorted(candidates, key=lambda p: (-p[1], str(p[0].get("full_name") or "")))
    top = ordered[:_RANKING_GROUP_SIZE]
    repos = [_rank_repo(item, metric_pill_fn(v)) for item, v in top]

    parts: list[str] = []
    for item, v in top[:3]:
        short = _short_name(item.get("full_name"))
        if not short:
            continue
        clause = _first_clause(item.get("description"))
        phrase = metric_phrase_fn(v)
        parts.append(f"{short} ({clause}, {phrase})" if clause else f"{short} ({phrase})")
    named = _join_natural(parts)
    paragraph = f"{lead} {named}." if named else lead

    seen_tags: list[str] = []
    for item, _v in top:
        tag_ids = item.get("tags") or ["general"]
        pt = tag_ids[0] if tag_ids else "general"
        if pt not in seen_tags:
            seen_tags.append(pt)

    count = len(repos)
    return {
        "tag_id": tag_id,
        "data_tags": " ".join(seen_tags) if seen_tags else "general",
        "label": label,
        "emoji": emoji,
        "count": count,
        "repos": repos,
        "headline": f"{emoji} {label} — {count} this week",
        "paragraph": paragraph,
        "static_head": True,
    }


def _build_ranking_groups(items_in_order: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ranking-led trend groups (most forked / most active / fastest % growth).

    Pure and offline: derives everything from each card's ``metrics`` bag. A
    group is emitted only when >= 2 repos genuinely carry its signal, so on a
    cold start (no fork/commit/percent history) it yields nothing.
    """
    groups: list[dict[str, Any]] = []

    # --- Most forked -------------------------------------------------------
    # Prefer weekly fork activity when any history exists; else absolute forks.
    fork_history = any(int(_item_metric(it, "fork_delta_wk") or 0) != 0 for it in items_in_order)
    if fork_history:
        fork_cands = [
            (it, float(_item_metric(it, "fork_delta_wk") or 0)) for it in items_in_order
        ]
        fork_cands = [(it, v) for it, v in fork_cands if v > 0]
        forks = _ranking_group(
            "most_forked", "\U0001f374", "Most forked this week",
            "Forks are piling up fastest on",
            fork_cands,
            lambda v: {"cls": "activity", "label": f"\U0001f374 +{int(v):,} forks/wk",
                       "title": "Forks gained this week"},
            lambda v: f"+{int(v):,} forks/wk",
        )
    else:
        fork_cands = [(it, float(_item_metric(it, "forks") or 0)) for it in items_in_order]
        fork_cands = [(it, v) for it, v in fork_cands if v > 0]
        forks = _ranking_group(
            "most_forked", "\U0001f374", "Most forked this week",
            "The most-forked projects in this week's cohort are",
            fork_cands,
            lambda v: {"cls": "activity", "label": f"\U0001f374 {compact_number(v)} forks",
                       "title": "Total forks"},
            lambda v: f"{compact_number(v)} forks",
        )
    if forks is not None:
        groups.append(forks)

    # --- Most active (commits) --------------------------------------------
    commit_cands = [
        (it, float(_item_metric(it, "commits_7d") or 0)) for it in items_in_order
    ]
    commit_cands = [(it, v) for it, v in commit_cands if v > 0]
    active = _ranking_group(
        "most_active", "⚡", "Most active this week",
        "The busiest repos by commits this week are",
        commit_cands,
        lambda v: {"cls": "activity", "label": f"⚡ {int(v):,} commits/wk",
                   "title": "Commits in the last 7 days"},
        lambda v: f"{int(v):,} commits",
    )
    if active is not None:
        groups.append(active)

    # --- Fastest % star growth (only when weekly % history exists) ---------
    growth_history = any(_item_metric(it, "growth_wk") is not None for it in items_in_order)
    if growth_history:
        growth_cands = [
            (it, float(_item_metric(it, "growth_wk")))
            for it in items_in_order
            if _item_metric(it, "growth_wk") is not None
        ]
        growth_cands = [(it, v) for it, v in growth_cands if v > 0]
        growth = _ranking_group(
            "fastest_growth", "\U0001f4c8", "Fastest % star growth",
            "The fastest-growing by star percentage this week:",
            growth_cands,
            lambda v: {"cls": _growth_class(int(round(v))),
                       "label": f"\U0001f4c8 {v:+.0f}% wk", "title": "Star growth this week"},
            lambda v: f"{v:+.0f}%",
        )
        if growth is not None:
            groups.append(growth)

    return groups


def build_news_groups(
    sections: Any,
    group_blurbs: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Group the edition's trending repos by primary tag into trend stories.

    Pure and offline (no LLM, no I/O). Accepts either the full metrics dict
    ``{"sections": [...]}`` or a bare list of section dicts whose items have
    already been classified (carry ``tags``/``tag_meta``) and had their focused
    ``description`` blurb applied by :func:`render_edition`. Repos are
    de-duplicated by ``full_name`` (first occurrence wins) and grouped by their
    PRIMARY tag (``tags[0]``).

    Each group with >= 2 repos becomes ``{tag_id, label, emoji, count, repos,
    headline, paragraph}`` with repos ordered by weekly star % desc then stars
    desc. Groups are ordered by count desc then tag priority. Singleton-tag
    repos collect into a trailing ``_also`` group so nothing is dropped. When a
    stored LLM paragraph exists in ``group_blurbs`` it is preferred over the
    deterministic one. Empty-safe: returns ``[]`` when there is nothing to show.
    """
    if isinstance(sections, dict):
        section_list = sections.get("sections") or []
    else:
        section_list = sections or []
    group_blurbs = group_blurbs or {}

    # Distinct repos, first occurrence wins, in edition order.
    order: list[str] = []
    by_name: dict[str, dict[str, Any]] = {}
    week_by_name: dict[str, int | None] = {}
    for section in section_list:
        for item in (section or {}).get("items") or []:
            full_name = item.get("full_name")
            if not full_name or full_name in by_name:
                continue
            by_name[full_name] = item
            order.append(full_name)
            deltas = item.get("deltas") or {}
            week_by_name[full_name] = _parse_pct(deltas.get("week"))

    # Bucket by primary tag.
    primary: dict[str, list[str]] = {}
    for full_name in order:
        item = by_name[full_name]
        tag_ids = item.get("tags") or tags.classify(item)
        pid = tag_ids[0] if tag_ids else "general"
        primary.setdefault(pid, []).append(full_name)

    def _sort_repos(names: list[str]) -> list[dict[str, Any]]:
        rows = [_group_repo(by_name[n], week_by_name.get(n)) for n in names]
        rows.sort(
            key=lambda r: (
                r["week_pct"] is None,
                -(r["week_pct"] or 0),
                -(r["stars"] or 0),
                r["full_name"],
            )
        )
        return rows

    groups: list[dict[str, Any]] = []
    singletons: list[str] = []
    for tag_id, names in primary.items():
        if len(names) < 2:
            singletons.extend(names)
            continue
        repos = _sort_repos(names)
        label = tags.LABELS.get(tag_id, tag_id)
        emoji = tags.EMOJI.get(tag_id, "📦")
        count = len(repos)
        paragraph = group_blurbs.get(tag_id) or _group_paragraph(label, tag_id, count, repos)
        groups.append(
            {
                "tag_id": tag_id,
                "data_tags": tag_id,
                "label": label,
                "emoji": emoji,
                "count": count,
                "repos": repos,
                "headline": f"{emoji} {label} — {count} trending this week",
                "paragraph": paragraph,
            }
        )

    # Themed groups first (by count, then tag priority); the uncategorised
    # "General" bucket always sorts last so a trends view leads with real themes.
    groups.sort(
        key=lambda g: (g["tag_id"] == "general", -g["count"], tags.priority_index(g["tag_id"]))
    )

    if singletons:
        repos = _sort_repos(singletons)
        # data-tags for the catch-all card = union of the singletons' primary
        # tags so the topical filter still reveals it for a matching tag.
        seen_tags: list[str] = []
        for r in repos:
            rt = r["tags"][0] if r["tags"] else "general"
            if rt not in seen_tags:
                seen_tags.append(rt)
        count = len(repos)
        names_join = _join_natural([_short_name(r["full_name"]) for r in repos[:4]])
        extra = "" if count <= 4 else f" and {count - 4} more"
        paragraph = (
            f"{count} more projects are each trending on their own this week"
            + (f": {names_join}{extra}." if names_join else ".")
        )
        groups.append(
            {
                "tag_id": "_also",
                "data_tags": " ".join(seen_tags),
                "label": "Also trending",
                "emoji": "✨",
                "count": count,
                "repos": repos,
                "headline": f"✨ Also trending — {count}",
                "paragraph": paragraph,
                "static_head": True,
            }
        )

    # Ranking-led trend groups (most forked / most active / fastest % growth)
    # lead the News view — the rankings devs care about — ahead of the topical
    # tag groups. Derived from the same shown cohort's metric bags; empty on cold
    # start (no fork/commit/percent history), so nothing is fabricated.
    ranking_groups = _build_ranking_groups([by_name[n] for n in order])
    return ranking_groups + groups


_DEFAULT_FOLLOWUP = "Want me to dig into any of these? Try one:"
_DEFAULT_SUGGESTIONS = (
    "Which of these reduce context in agent sessions?",
    "Fastest-growing Rust projects this week?",
    "What's new in RAG?",
)


def _canned_conversation() -> tuple[str, list[str]]:
    """The canned follow-up + suggestion chips, owned by the demo seeder.

    Lazily imported so :mod:`render` stays cheap and never hard-depends on the
    demo module; falls back to local defaults if the demo copy is unavailable.
    """
    try:
        from .demo import DEMO_FOLLOWUP, DEMO_SUGGESTIONS

        return DEMO_FOLLOWUP, list(DEMO_SUGGESTIONS)
    except Exception:  # pragma: no cover - import edge case
        return _DEFAULT_FOLLOWUP, list(_DEFAULT_SUGGESTIONS)


def summary_extras(
    summary: dict[str, Any] | None,
    tag_bar: list[dict[str, Any]] | None,
    section_list: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Attach a deterministic conversational ``followup`` + ``suggestions`` list.

    Pure and offline. When a summary already carries these keys (e.g. from a
    future LLM path) they are left untouched; otherwise a small, grounded set of
    follow-up question chips is derived from the edition's own tags/languages so
    the explainer can invite the reader into the top chat bar. Empty-safe:
    returns ``summary`` unchanged (possibly ``None``).
    """
    if not summary:
        return summary
    if summary.get("followup") and summary.get("suggestions"):
        return summary

    canned_followup, canned_suggestions = _canned_conversation()
    tag_bar = tag_bar or []
    section_list = section_list or []
    tag_ids = {t.get("id") for t in tag_bar}

    # Most common language across this edition's cards (deterministic tiebreak).
    lang_counts: dict[str, int] = {}
    for section in section_list:
        for item in section.get("items") or []:
            lang = item.get("language")
            if lang:
                lang_counts[str(lang)] = lang_counts.get(str(lang), 0) + 1
    top_lang = None
    if lang_counts:
        top_lang = sorted(lang_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    suggestions: list[str] = []
    if {"ai-agents", "context-rag"} & tag_ids:
        suggestions.append("Which of these reduce context in agent sessions?")
    if top_lang:
        suggestions.append(f"Fastest-growing {top_lang.title()} projects this week?")
    else:
        suggestions.append("What's the fastest-growing project this week?")
    if {"context-rag"} & tag_ids:
        suggestions.append("What's new in RAG?")
    else:
        suggestions.append("What's driving the buzz this week?")

    # Deterministic de-dupe, cap at 3, never empty (fall back to canned copy).
    seen: set[str] = set()
    deduped: list[str] = []
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    if not deduped:
        deduped = list(canned_suggestions)
    summary = dict(summary)
    summary["followup"] = summary.get("followup") or canned_followup
    summary["suggestions"] = summary.get("suggestions") or deduped[:3]
    return summary


def _confidence(days_of_history: Any) -> dict[str, Any]:
    """Map days_of_history to a confidence label for the template banner."""
    try:
        days = int(days_of_history) if days_of_history is not None else 0
    except (TypeError, ValueError):
        days = 0
    if days >= 7:
        return {
            "low": False,
            "level": "solid",
            "label": f"Solid: {days} days of snapshot history behind these numbers.",
        }
    return {
        "low": True,
        "level": "early",
        "label": (
            f"Early data: only {days} day(s) of snapshot history — deltas are measured "
            "against the earliest snapshot available and will firm up over the week."
        ),
    }


def _build_env() -> Environment:
    loaders: list[Any] = []
    try:
        loaders.append(PackageLoader("ghpulse", "templates"))
    except Exception:  # pragma: no cover - packaging edge cases
        pass
    loaders.append(FileSystemLoader(str(Path(__file__).resolve().parent / "templates")))
    env = Environment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(enabled_extensions=("html", "j2"), default=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["sparkline"] = sparkline_svg
    env.filters["compact"] = compact_number
    env.filters["lang_color"] = lang_color
    return env


def list_editions(site_dir: Path) -> list[str]:
    """Edition directory names under site_dir, newest first."""
    site_dir = Path(site_dir)
    if not site_dir.exists():
        return []
    editions = [
        p.name
        for p in site_dir.iterdir()
        if p.is_dir() and _EDITION_DIR_RE.match(p.name) and (p / "index.html").exists()
    ]
    return sorted(editions, reverse=True)


_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _edition_date_label(edition: str) -> str:
    """Format an edition dir name 'YYYY-MM-DD' as a short 'Aug 12' label."""
    m = _EDITION_DIR_RE.match(str(edition or ""))
    if not m:
        return str(edition or "")
    try:
        y, mo, d = (int(x) for x in edition.split("-"))
        return f"{_MONTHS[mo - 1]} {d}"
    except (ValueError, IndexError):
        return str(edition)


def build_edition_timeline(
    site_dir: Path,
    current_edition: str,
    max_nodes: int = 8,
) -> list[dict[str, Any]]:
    """Ordered (oldest -> newest) list of edition nodes for the timeline strip.

    Discovers already-rendered editions under ``site_dir`` (via
    :func:`list_editions`) and always folds in ``current_edition`` even when its
    ``index.html`` has not been written yet. Caps to the ``max_nodes`` most
    recent, marks the current edition, and gives each node a relative link to a
    sibling edition (``../<edition>/index.html``). Always returns at least the
    single current node so the timeline renders gracefully for a first edition.
    """
    names = set(list_editions(site_dir))
    if _EDITION_DIR_RE.match(str(current_edition or "")):
        names.add(current_edition)
    # Most-recent first, cap, then flip to oldest -> newest for display.
    recent = sorted(names, reverse=True)[:max_nodes]
    recent.sort()
    nodes: list[dict[str, Any]] = []
    for ed in recent:
        nodes.append(
            {
                "edition": ed,
                "date_label": _edition_date_label(ed),
                "link": f"../{ed}/index.html",
                "is_current": ed == current_edition,
            }
        )
    return nodes


def _render_index(env: Environment, site_dir: Path) -> Path:
    editions = list_editions(site_dir)  # newest first
    latest = editions[0] if editions else None
    # Rich rows for the calendar/timeline index (newest first for the list).
    rows: list[dict[str, Any]] = []
    for ed in editions:
        y, mo = "", ""
        m = _EDITION_DIR_RE.match(ed)
        if m:
            parts = ed.split("-")
            y = parts[0]
            try:
                mo = _MONTHS[int(parts[1]) - 1]
            except (ValueError, IndexError):
                mo = parts[1]
        rows.append(
            {
                "edition": ed,
                "date_label": _edition_date_label(ed),
                "month": mo,
                "year": y,
                "link": f"{ed}/index.html",
                "is_latest": ed == latest,
            }
        )
    tmpl = env.get_template("index.html.j2")
    html_text = tmpl.render(editions=editions, latest=latest, rows=rows)
    out = Path(site_dir) / "index.html"
    out.write_text(html_text, encoding="utf-8")
    return out


def render_edition(
    conn: Any,
    edition: str,
    settings: Any,
    sections: dict[str, Any] | None = None,
) -> Path:
    """Render one edition page and rebuild the site index. Returns the edition html path."""
    if sections is None:
        sections = score.compute_metrics(conn, edition)
        # Fold in any social hype already computed for this edition (no-op if none).
        sections = hype.merge_hype_sections(conn, edition, sections)

    env = _build_env()
    site_dir = Path(settings.site_dir)
    edition_dir = site_dir / edition
    edition_dir.mkdir(parents=True, exist_ok=True)

    days_of_history = sections.get("days_of_history")
    section_list = sections.get("sections") or []

    # --- Topical tags + metric pills (deterministic, offline) --------------
    # Classify every card once, remember tags per repo, and harden the copy.
    name_to_tags: dict[str, list[str]] = {}
    tag_repo_names: dict[str, set[str]] = {}
    all_repo_names: set[str] = set()
    for section in section_list:
        is_leaderboard = section.get("key") == "leaderboard"
        for item in section.get("items") or []:
            tag_ids = tags.classify(item)
            item["tags"] = tag_ids
            item["tag_meta"] = tags.tag_meta(tag_ids)
            item["metric_pills"] = build_metric_pills(item)
            # Leaderboard cards carry the full metric bag as data-* attributes so
            # the client-side sorter can re-rank by any dimension without a fetch.
            if is_leaderboard:
                item["lb_data"] = leaderboard_data_attrs(item)
            # Focused "what it DOES" blurb when the LLM refine pass produced one
            # (keyed by the description hash); else the cleaned GitHub description.
            full_name = item.get("full_name")
            raw_desc = item.get("description")
            blurb = None
            if full_name:
                try:
                    blurb = db.get_blurb(conn, full_name, db.desc_hash(raw_desc))
                except Exception:  # pragma: no cover - blurb is best-effort chrome
                    blurb = None
            item["description"] = blurb or harden_description(raw_desc)
            if full_name:
                name_to_tags[full_name] = tag_ids
                all_repo_names.add(full_name)
                for cid in tag_ids:
                    tag_repo_names.setdefault(cid, set()).add(full_name)

    # Distinct repo count per tag; order by count desc then priority.
    tag_bar: list[dict[str, Any]] = []
    for cid, names in tag_repo_names.items():
        count = len(names)
        if count < 1:
            continue
        tag_bar.append(
            {
                "id": cid,
                "label": tags.LABELS.get(cid, cid),
                "emoji": tags.EMOJI.get(cid, "📦"),
                "count": count,
            }
        )
    tag_bar.sort(key=lambda t: (-t["count"], tags.priority_index(t["id"])))

    # News view derives from the same section data — pure, offline, no LLM.
    news_items = build_news_items(section_list)
    # Give news items the SAME repo's tags via the full_name -> tags map so the
    # client-side filter behaves identically across both views.
    for n in news_items:
        tag_ids = name_to_tags.get(n.get("full_name"))
        if tag_ids is not None:
            n["tags"] = tag_ids
            n["tag_meta"] = tags.tag_meta(tag_ids)

    # Grouped thematic trend stories for the News view. Items already carry
    # tags/tag_meta and their focused blurb (set above). Prefer any stored LLM
    # trend paragraph; else the deterministic one is composed inline.
    group_blurbs: dict[str, str] = {}
    try:
        group_blurbs = db.get_group_blurbs(conn, edition)
    except Exception:  # pragma: no cover - group blurbs are best-effort chrome
        group_blurbs = {}
    news_groups = build_news_groups(section_list, group_blurbs=group_blurbs)

    # P3: stored LLM explainer for this edition (absent -> card omitted entirely).
    summary = None
    try:
        row = db.get_summary(conn, edition)
        if row is not None:
            summary = {
                "text": row["text"],
                "model": row["model"],
                "generated_at": row["generated_at"],
            }
    except Exception:  # pragma: no cover - summary is best-effort chrome
        summary = None

    # Turn the explainer into the opening turn of a conversation: attach a
    # deterministic follow-up question + suggestion chips grounded on this
    # edition's own tags/languages (no LLM, no I/O).
    summary = summary_extras(summary, tag_bar, section_list)

    # Panel port for the top chat bar's grounded local-LLM Q&A (CORS to panel).
    try:
        panel_port = int(getattr(settings, "panel_port", 8765) or 8765)
    except (TypeError, ValueError):
        panel_port = 8765

    # Week-by-week edition timeline (server-side, offline). Discovers sibling
    # editions already on disk and always includes the current one.
    timeline = build_edition_timeline(site_dir, edition)

    # Honest backend label for the status pill (was hard-coded "demo"). Reflects
    # the configured LLM so users can see what the Ask bar actually runs on.
    llm_mode = (getattr(settings, "llm", "off") or "off").lower()
    if llm_mode == "ollama":
        backend_label = f"Ollama · {getattr(settings, 'ollama_model', '') or 'local'}"
    elif llm_mode == "anthropic":
        backend_label = f"Claude · {getattr(settings, 'claude_model', '') or 'claude-sonnet-5'}"
    elif llm_mode == "auto":
        backend_label = "auto (Ollama → Claude)"
    else:
        backend_label = "off"

    tmpl = env.get_template("edition.html.j2")
    html_text = tmpl.render(
        edition=sections.get("edition", edition),
        cohort_size=sections.get("cohort_size", 0),
        generated_at=sections.get("generated_at", ""),
        days_of_history=days_of_history,
        confidence=_confidence(days_of_history),
        sections=section_list,
        news_items=news_items,
        news_groups=news_groups,
        tag_bar=tag_bar,
        total_repos=len(all_repo_names),
        has_items=any((s.get("items") or []) for s in section_list),
        summary=summary,
        timeline=timeline,
        backend_label=backend_label,
        panel_port=panel_port,
        leaderboard_dims=sections.get("leaderboard_dims") or {},
        leaderboard_default=sections.get("leaderboard_default") or "momentum",
        leaderboard_pills=leaderboard_pills(sections.get("leaderboard_dims")),
    )
    out = edition_dir / "index.html"
    out.write_text(html_text, encoding="utf-8")

    _render_index(env, site_dir)
    return out

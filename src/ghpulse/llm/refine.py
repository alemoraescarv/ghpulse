"""P4: LLM-refined "focused description" per repo — a punchy 'what it DOES' line.

For each repo shown in an edition we ask the selected backend for one benefit-
oriented line (at most ~12 words, verb-first) grounded ONLY on the repo's own
description + topics. Results are cached in ``repo_blurb`` keyed by
``desc_hash(description)`` so a render never re-calls the model and a description
edit invalidates the old blurb. The chat call is INJECTABLE so tests run offline
with a scripted fake; with no backend configured/available this is a no-op and
the cleaned GitHub description keeps showing.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .. import db

# A chat function: (system, prompt) -> completion text ("" on failure/refusal).
ChatFn = Callable[[str, str], str]

REFINE_SYSTEM = (
    "You write ONE punchy line saying what a software project DOES, for a "
    "developer skimming a tech-news feed. Rules: at most 12 words; start with a "
    "verb; say the concrete action + what it's for; no marketing fluff, no the "
    "project name, no quotes, no trailing period; base it ONLY on the given "
    "description and topics — never invent capabilities."
)

MAX_WORDS = 14  # hard reject ceiling (validation); the prompt asks for <=12

_REFUSAL_MARKERS = (
    "i cannot",
    "i can't",
    "i'm sorry",
    "i am sorry",
    "as an ai",
    "i'm unable",
    "i am unable",
    "cannot help",
    "unable to",
)


def _refine_prompt(item: dict[str, Any]) -> str:
    """Compose the grounded user content for one repo."""
    full_name = item.get("full_name", "?")
    description = (item.get("description") or "").strip() or "(none)"
    topics = ", ".join(item.get("topics") or []) or "(none)"
    language = item.get("language") or "(unknown)"
    return (
        f"Project: {full_name}\n"
        f"Language: {language}\n"
        f"Topics: {topics}\n"
        f"Description: {description}\n\n"
        "Write the one-line focused description now."
    )


def _clean(raw: str) -> str:
    """Strip quotes/whitespace/trailing punctuation from a model line."""
    text = (raw or "").strip()
    # If the model returned multiple lines, keep the first non-empty one.
    for line in text.splitlines():
        if line.strip():
            text = line.strip()
            break
    # Strip surrounding quotes/backticks and leading list markers.
    text = text.strip().strip("`").strip('"').strip("'").strip()
    text = text.lstrip("-*•").strip()
    # Drop a single trailing sentence-ending punctuation.
    text = text.rstrip().rstrip(".!;:").rstrip()
    return text


def _validate(raw: str) -> str | None:
    """Return a clean blurb, or None if the model output must be rejected.

    Rejects empty output, over-long output (> MAX_WORDS words), and obvious
    refusals. A rejected result makes the caller fall back to the hardened
    GitHub description.
    """
    text = _clean(raw)
    if not text:
        return None
    if len(text.split()) > MAX_WORDS:
        return None
    low = text.casefold()
    if any(marker in low for marker in _REFUSAL_MARKERS):
        return None
    return text


def _shown_repos(conn: sqlite3.Connection, edition: str) -> dict[str, dict[str, Any]]:
    """Distinct repos shown in the edition, mapped full_name -> item dict.

    Rebuilds the same sections the renderer consumes (score metrics + folded-in
    social hype), then keeps the first item seen per full_name. No network.
    """
    from .. import hype, score  # local import keeps this module light

    sections = score.compute_metrics(conn, edition)
    sections = hype.merge_hype_sections(conn, edition, sections)
    seen: dict[str, dict[str, Any]] = {}
    for section in sections.get("sections") or []:
        for item in section.get("items") or []:
            full_name = item.get("full_name")
            if full_name and full_name not in seen:
                seen[full_name] = item
    return seen


def refine_descriptions(
    conn: sqlite3.Connection,
    edition: str,
    settings: Any,
    chat: ChatFn | None = None,
    limit: int = 80,
    model: str | None = None,
) -> int:
    """Generate + cache focused blurbs for the repos shown in an edition.

    Returns the number of blurbs written (0 when no backend is available or when
    every shown repo is already cached). Cache hits are skipped. On a rejected /
    empty model line the hardened GitHub description is stored instead, so a bad
    model never blanks a card and we never re-call it on the next render.
    """
    from ..render import harden_description  # local import avoids cycles

    if chat is None:
        backend = _select_backend(settings)
        if backend is None or not backend.available():
            return 0
        chat = backend.summarize
        model = model or backend.name
    model = model or "llm"

    written = 0
    for full_name, item in list(_shown_repos(conn, edition).items())[:limit]:
        description = item.get("description")
        key = db.desc_hash(description)
        if db.get_blurb(conn, full_name, key) is not None:
            continue  # cache hit — keep the stored blurb
        try:
            raw = chat(REFINE_SYSTEM, _refine_prompt(item)) or ""
        except Exception:
            raw = ""
        blurb = _validate(raw) or harden_description(description)
        db.upsert_blurb(conn, full_name, key, blurb, model, db.now_iso())
        written += 1
    if written:
        db.commit(conn)
    return written


def _select_backend(settings: Any):
    """Lazy import of select_backend to avoid an import cycle with the package."""
    from . import select_backend

    return select_backend(settings)


# ---------------------------------------------------------------------------
# News view — LLM-refined thematic trend paragraphs (optional, cached per group)
# ---------------------------------------------------------------------------

GROUP_SYSTEM = (
    "You write ONE short trend-story paragraph for a developer skimming a "
    "tech-news feed, in a grounded, matter-of-fact voice. Rules: at most 45 "
    "words; 2-3 sentences; name only the projects you are given and describe "
    "each using ONLY its provided blurb; state the count; never invent projects "
    "or capabilities; no marketing hype, no quotes, no bullet lists."
)

GROUP_MAX_WORDS = 55  # hard reject ceiling; the prompt asks for <= 45


def _group_prompt(group: dict[str, Any]) -> str:
    """Compose grounded user content for one topical group."""
    label = group.get("label", "?")
    count = group.get("count", 0)
    lines = [f"Theme: {label}", f"Count trending this week: {count}", "Projects:"]
    for repo in group.get("repos") or []:
        name = repo.get("full_name", "?")
        blurb = (repo.get("description") or "").strip() or "(no description)"
        lines.append(f"- {name}: {blurb}")
    lines.append(
        "\nWrite the one-paragraph trend story now (<=45 words), naming the top "
        "2-3 projects and what each does, then one sentence on rising interest."
    )
    return "\n".join(lines)


def _validate_group(raw: str) -> str | None:
    """Return a clean trend paragraph, or None if the model output is rejected.

    Rejects empty output, over-long output (> GROUP_MAX_WORDS words) and obvious
    refusals. A rejected result makes the caller keep the deterministic paragraph.
    """
    text = (raw or "").strip().strip("`").strip('"').strip("'").strip()
    if not text:
        return None
    if len(text.split()) > GROUP_MAX_WORDS:
        return None
    low = text.casefold()
    if any(marker in low for marker in _REFUSAL_MARKERS):
        return None
    return text


def _grouped(conn: sqlite3.Connection, edition: str) -> list[dict[str, Any]]:
    """Recompute the edition's news groups with focused blurbs applied. No network."""
    from .. import render, tags  # local import keeps this module light

    sections = _shown_sections(conn, edition)
    for section in sections.get("sections") or []:
        for item in section.get("items") or []:
            tag_ids = tags.classify(item)
            item["tags"] = tag_ids
            item["tag_meta"] = tags.tag_meta(tag_ids)
            full_name = item.get("full_name")
            raw_desc = item.get("description")
            blurb = None
            if full_name:
                try:
                    blurb = db.get_blurb(conn, full_name, db.desc_hash(raw_desc))
                except Exception:
                    blurb = None
            item["description"] = blurb or render.harden_description(raw_desc)
    return render.build_news_groups(sections.get("sections") or [])


def _shown_sections(conn: sqlite3.Connection, edition: str) -> dict[str, Any]:
    """Recompute the sections dict (GitHub + hype) for an edition — no network."""
    from .. import hype, score

    sections = score.compute_metrics(conn, edition)
    return hype.merge_hype_sections(conn, edition, sections)


def refine_news_groups(
    conn: sqlite3.Connection,
    edition: str,
    settings: Any,
    chat: ChatFn | None = None,
    model: str | None = None,
) -> int:
    """Rewrite each News group's trend paragraph in the LLM's voice, cached.

    Returns the number of group paragraphs written (0 when no backend is
    available or there are no multi-repo groups). The catch-all ``_also`` group
    is skipped. On rejected/empty output nothing is stored for that group, so the
    deterministic paragraph keeps showing. The chat call is INJECTABLE for
    offline tests; with no backend configured this is a no-op.
    """
    if chat is None:
        backend = _select_backend(settings)
        if backend is None or not backend.available():
            return 0
        chat = backend.summarize
        model = model or backend.name
    model = model or "llm"

    written = 0
    for group in _grouped(conn, edition):
        tag_id = group.get("tag_id")
        if not tag_id or tag_id == "_also":
            continue
        try:
            raw = chat(GROUP_SYSTEM, _group_prompt(group)) or ""
        except Exception:
            raw = ""
        blurb = _validate_group(raw)
        if blurb is None:
            continue  # keep the deterministic paragraph
        db.upsert_group_blurb(conn, edition, tag_id, blurb, model, db.now_iso())
        written += 1
    if written:
        db.commit(conn)
    return written

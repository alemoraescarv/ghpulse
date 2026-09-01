"""Grounded Q&A over the CURRENT edition's trending repos ("Ask about this week").

Unlike the agentic GitHub researcher (``ghpulse.agent``) which goes live to
GitHub, this feature answers STRICTLY from the repos already gathered and shown
on this week's news page — a small retrieval-augmented step over the edition's
own data. We rebuild the same sections the renderer consumes (score metrics +
folded-in social hype), compress each shown repo into a compact context line
(focused blurb, tags, stars, weekly/monthly growth, which section/buzz), and ask
the selected backend to recommend the most relevant repos BY NAME from that list.

The chat call is INJECTABLE (default = the selected backend's ``summarize``) so
tests run fully offline with a scripted fake. With no backend configured or
available, this is a friendly no-op: it returns a note telling the user to enable
a local model (Ollama) or Claude, and ``grounded=False``. Everything is wrapped
so it never raises — a failure yields a friendly error string, never an
exception to the caller.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from .. import db

# A chat function: (system, prompt) -> completion text ("" on failure/refusal).
ChatFn = Callable[[str, str], str]

ASK_SYSTEM = (
    "You are answering a developer's question using ONLY the provided list of "
    "this week's trending GitHub repositories. Recommend the most relevant repos "
    "BY NAME from the list, briefly saying why (using each repo's own "
    "description). If nothing in the list fits, say so plainly — do NOT invent "
    "repos or facts not in the list. Be concise: at most ~80 words."
)

_NO_BACKEND_NOTE = (
    "Ask-about-this-week needs a local model. Enable a local model (Ollama) or "
    "Claude in the control panel's LLM backend picker, then ask again. This "
    "feature answers only from the repos already on your news page — it never "
    "goes to the network."
)


def _shown_repos(conn: sqlite3.Connection, edition: str) -> list[dict[str, Any]]:
    """Distinct repos shown in the edition, in rank/buzz order (first-seen wins).

    Rebuilds the same sections the renderer consumes (score metrics + folded-in
    social hype), then keeps the first item seen per full_name and remembers the
    section title it first appeared in. No network.
    """
    from .. import hype, score  # local import keeps this module light

    sections = score.compute_metrics(conn, edition)
    sections = hype.merge_hype_sections(conn, edition, sections)
    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for section in sections.get("sections") or []:
        title = section.get("title") or section.get("key") or "section"
        for item in section.get("items") or []:
            full_name = item.get("full_name")
            if not full_name or full_name in seen:
                continue
            entry = dict(item)
            entry["_section"] = title
            seen[full_name] = entry
            order.append(full_name)
    return [seen[name] for name in order]


def _repo_line(conn: sqlite3.Connection, index: int, item: dict[str, Any]) -> str:
    """One compact, citable context line for a repo the model can reason over."""
    from .. import tags  # local import keeps this module light
    from ..render import harden_description

    full_name = item.get("full_name", "?")
    raw_desc = item.get("description")
    blurb = None
    try:
        blurb = db.get_blurb(conn, full_name, db.desc_hash(raw_desc))
    except Exception:  # pragma: no cover - blurb lookup is best-effort
        blurb = None
    blurb = blurb or harden_description(raw_desc)

    label_names = [
        tags.LABELS.get(cid, cid) for cid in (item.get("tags") or tags.classify(item))
    ]

    parts: list[str] = [f"{index}. {full_name} — {blurb}"]
    if label_names:
        parts.append(f"[{', '.join(label_names)}]")
    stars = item.get("stars")
    if stars is not None:
        parts.append(f"★{stars}")
    deltas = item.get("deltas") or {}
    growth = [v for v in (deltas.get("week"), deltas.get("month")) if v]
    if growth:
        parts.append("(" + ", ".join(growth) + ")")
    section = item.get("_section")
    if section:
        parts.append(f"section: {section}")
    badges = item.get("hype_badges") or []
    if badges:
        parts.append(
            "buzz: " + ", ".join(
                f"{b.get('label')} {b.get('count')}" for b in badges if isinstance(b, dict)
            )
        )
    return " ".join(parts)


def build_context(
    conn: sqlite3.Connection, edition: str, limit: int = 60
) -> tuple[str, list[str]]:
    """Return ``(numbered_repo_list_text, full_names)`` for an edition. No network.

    ``full_names`` is aligned with the numbered list and used to detect citations
    in the model's answer. Capped to ``limit`` by rank/buzz order.
    """
    repos = _shown_repos(conn, edition)[: max(0, int(limit))]
    lines: list[str] = []
    names: list[str] = []
    for i, item in enumerate(repos, start=1):
        lines.append(_repo_line(conn, i, item))
        names.append(item.get("full_name", ""))
    return "\n".join(lines), names


def _select_backend(settings: Any):
    """Lazy import of select_backend to avoid an import cycle with the package."""
    from . import select_backend

    return select_backend(settings)


def _cited(answer: str, names: list[str]) -> list[str]:
    """Which context full_names appear (by full name or short name) in the answer."""
    low = (answer or "").lower()
    out: list[str] = []
    for name in names:
        if not name:
            continue
        short = name.split("/")[-1].lower()
        if name.lower() in low or (len(short) >= 3 and short in low):
            if name not in out:
                out.append(name)
    return out


def answer_over_edition(
    conn: sqlite3.Connection,
    edition: str,
    question: str,
    settings: Any,
    chat: ChatFn | None = None,
    limit: int = 60,
) -> dict[str, Any]:
    """Answer *question* strictly from the edition's shown repos (grounded RAG).

    Returns ``{"answer": str, "cited": [full_name, ...], "model": str,
    "grounded": bool}``. Never raises. With no backend configured/available it
    returns a friendly note and ``grounded=False``. The ``chat`` call is
    injectable so tests run offline with a scripted fake.
    """
    try:
        q = str(question or "").strip()
        if not q:
            return {
                "answer": "Ask a question about this week's trending repos, e.g. "
                "“which repo runs models locally?”.",
                "cited": [],
                "model": "",
                "grounded": False,
            }

        model = "llm"
        if chat is None:
            backend = _select_backend(settings)
            if backend is None or not backend.available():
                return {
                    "answer": _NO_BACKEND_NOTE,
                    "cited": [],
                    "model": "",
                    "grounded": False,
                }
            chat = backend.summarize
            model = getattr(backend, "name", "llm") or "llm"

        context, names = build_context(conn, edition, limit=limit)
        if not context.strip():
            return {
                "answer": "There are no repos on this week's page yet — run a "
                "refresh (or `ghpulse demo`) first, then ask again.",
                "cited": [],
                "model": model,
                "grounded": False,
            }

        prompt = (
            "This week's trending GitHub repositories:\n"
            f"{context}\n\n"
            f"Question: {q}"
        )
        try:
            raw = chat(ASK_SYSTEM, prompt) or ""
        except Exception:  # noqa: BLE001 - degrade instead of raising
            raw = ""

        answer = (raw or "").strip()
        if not answer:
            return {
                "answer": "I couldn't find a good match for that among this week's "
                "repos. Try rephrasing, or switch to “Search GitHub” to look live.",
                "cited": [],
                "model": model,
                "grounded": True,
            }

        return {
            "answer": answer,
            "cited": _cited(answer, names),
            "model": model,
            "grounded": True,
        }
    except Exception as exc:  # noqa: BLE001 - never propagate to the caller
        return {
            "answer": f"Ask-about-this-week hit an unexpected error and stopped: "
            f"{exc}. Please try again.",
            "cited": [],
            "model": "",
            "grounded": False,
        }

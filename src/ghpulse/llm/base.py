"""LLM backend contract + a pure digest builder for the "what happened" explainer.

The digest is a compact plain-text/markdown rendering of an edition's top movers
(a handful per section, including the Buzz layer and the monthly % deltas) that is
fed to whichever backend is selected. ``build_digest`` is pure and unit-testable —
it takes the same sections dict that render/edition.html.j2 consume and returns a
string; it makes no network calls.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# How many repos to include per section in the digest fed to the model.
DIGEST_ITEMS_PER_SECTION = 5

SYSTEM_PROMPT = (
    "You are a sharp, concise tech-news editor. You are given this week's GitHub "
    "trend data (momentum, star growth, breakouts, and cross-platform social buzz). "
    "Write a short 'what happened in tech this week' brief: 2-4 tight paragraphs, "
    "plain prose, no bullet lists, no preamble. Name the notable repos, explain the "
    "story the numbers tell, call out divergences (buzz without stars, or stars "
    "without substance), and note weekly-vs-monthly momentum where it's telling. "
    "Do not invent facts beyond the data provided."
)


@runtime_checkable
class LLMBackend(Protocol):
    """A pluggable summarizer backend.

    Implementations must never raise from ``available()`` or ``summarize()`` — a
    dead/absent backend returns False / "" so a run is never broken by the LLM
    layer being unreachable.
    """

    name: str

    def available(self) -> bool:
        """True if this backend can currently be called (key present, server up)."""
        ...

    def summarize(self, system: str, prompt: str) -> str:
        """Return the model's completion, or "" on any error/refusal."""
        ...


def _fmt_item(item: dict[str, Any]) -> str:
    rank = item.get("rank")
    name = item.get("full_name", "?")
    label = item.get("value_label", "")
    lang = item.get("language") or ""
    stars = item.get("stars")
    deltas = item.get("deltas") or {}
    parts: list[str] = [f"  {rank}. {name}"]
    if lang:
        parts.append(f"[{lang}]")
    if stars is not None:
        parts.append(f"★{stars}")
    if label:
        parts.append(f"— {label}")
    delta_bits = [v for v in (deltas.get("week"), deltas.get("month")) if v]
    if delta_bits:
        parts.append("(" + ", ".join(delta_bits) + ")")
    badges = item.get("hype_badges") or []
    if badges:
        parts.append(
            "buzz: " + ", ".join(f"{b.get('label')} {b.get('count')}" for b in badges)
        )
    return " ".join(parts)


def build_digest(
    sections: dict[str, Any], items_per_section: int = DIGEST_ITEMS_PER_SECTION
) -> str:
    """Render an edition's sections dict into a compact digest for the model.

    Pure: no I/O, deterministic for a given sections dict.
    """
    lines: list[str] = []
    edition = sections.get("edition", "")
    cohort = sections.get("cohort_size", 0)
    history = sections.get("days_of_history", 0)
    lines.append(f"GHPulse edition {edition}".strip())
    lines.append(
        f"Cohort: {cohort} tracked repos · {history} day(s) of snapshot history"
    )
    lines.append("")
    for section in sections.get("sections") or []:
        items = section.get("items") or []
        if not items:
            continue
        title = section.get("title", section.get("key", "section"))
        lines.append(f"## {title}")
        subtitle = section.get("subtitle")
        if subtitle:
            lines.append(f"({subtitle})")
        for item in items[:items_per_section]:
            lines.append(_fmt_item(item))
        lines.append("")
    return "\n".join(lines).strip() + "\n"

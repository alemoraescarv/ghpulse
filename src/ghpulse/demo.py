"""Deterministic offline demo seeder.

Populates the real repo/snapshot tables with ~45 believable repos and two
snapshots each (edition-7d and edition) so the real score + render paths can be
exercised with no network and no token. Seeded with random.Random(1234) so the
output is identical on every run.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db
from .config import Settings

_RNG_SEED = 1234
_SOCIAL_SEED = 5678


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# Deterministic social profiles: full_name -> {platform: (num_posts, (lo, hi))}.
# These power the per-card hype badges (HN/Reddit/Lobsters mention chips) on the
# leaderboard/breakout/riser cards. Bluesky was removed as a source.
_SOCIAL_PROFILES: dict[str, dict[str, tuple[int, tuple[int, int]]]] = {
    "ollama/ollama": {"hn": (4, (200, 600)), "reddit": (3, (100, 400))},
    "astral-sh/uv": {"hn": (5, (300, 800)), "lobsters": (2, (20, 60))},
    "ggerganov/llama.cpp": {"hn": (3, (150, 500)), "reddit": (2, (80, 300))},
    "langchain-ai/langchain": {"reddit": (3, (60, 220))},
    "agentsmith/crewpilot": {"hn": (2, (90, 260))},
    "skunkworks/nanorag": {"hn": (3, (120, 380)), "lobsters": (2, (15, 55))},
    "mcphub/gateway": {"hn": (2, (70, 210)), "lobsters": (1, (10, 40))},
    "plateau-io/steadystate": {"hn": (5, (400, 900)), "reddit": (3, (60, 180))},
    "student-dev/first-crate": {"hn": (4, (300, 700)), "lobsters": (1, (20, 50))},
    "quietcorner/textutils": {"lobsters": (2, (12, 45))},
}

_POST_URLS: dict[str, str] = {
    "hn": "https://news.ycombinator.com/item?id=demo{n}",
    "reddit": "https://www.reddit.com/r/programming/comments/demo{n}/",
    "lobsters": "https://lobste.rs/s/demo{n}",
}


def _seed_social(
    conn: sqlite3.Connection,
    name_to_id: dict[str, int],
    edition_dt: datetime,
    seed_offset: int = 0,
) -> None:
    """Seed deterministic social_mention rows so the per-card hype badges render offline."""
    rng = random.Random(_SOCIAL_SEED + seed_offset)
    now = datetime.now(timezone.utc)
    captured_at = _iso(now)
    serial = 0
    for full_name, profile in _SOCIAL_PROFILES.items():
        repo_id = name_to_id.get(full_name)
        if repo_id is None:
            continue
        for platform, (num_posts, (lo, hi)) in profile.items():
            for i in range(num_posts):
                serial += 1
                engagement = rng.randint(lo, hi)
                # Recent, in-window posts (within the last ~6 days), never in the future.
                age_hours = rng.uniform(2.0, 150.0)
                posted_at = _iso(now - timedelta(hours=age_hours))
                db.insert_mention(
                    conn,
                    repo_id=repo_id,
                    platform=platform,
                    post_id=f"{platform}-{repo_id}-{i}",
                    url=_POST_URLS[platform].format(n=serial),
                    title=f"{full_name} on {platform}",
                    engagement=engagement,
                    posted_at=posted_at,
                    captured_at=captured_at,
                )
    db.commit(conn)


# (full_name, language, topics, description)
# fields: kind controls growth shape in _seed_plan below.
_STEADY = [
    ("pytorch/pytorch", "python", ["ai-agents", "llm"], "Tensors and dynamic neural networks with strong GPU acceleration."),
    ("kubernetes/kubernetes", "go", ["kubernetes"], "Production-grade container scheduling and management."),
    ("rust-lang/rust", "rust", ["rust"], "Empowering everyone to build reliable and efficient software."),
    ("microsoft/typescript", "typescript", [], "TypeScript is a superset of JavaScript that compiles to clean JS."),
    ("ziglang/zig", "zig", [], "General-purpose programming language and toolchain."),
    ("apple/swift", "swift", [], "The Swift programming language."),
    ("denoland/deno", "rust", ["wasm"], "A modern runtime for JavaScript and TypeScript."),
    ("ollama/ollama", "go", ["llm"], "Get up and running with large language models locally."),
    ("langchain-ai/langchain", "python", ["llm", "rag"], "Build context-aware reasoning applications."),
    ("ggerganov/llama.cpp", "c++", ["llm"], "LLM inference in C/C++ with minimal dependencies."),
    ("tauri-apps/tauri", "rust", ["rust"], "Build smaller, faster, more secure desktop apps with a web frontend."),
    ("vercel/next.js", "typescript", [], "The React framework for the web."),
]

_SOLID = [
    ("astral-sh/uv", "rust", ["rust"], "An extremely fast Python package and project manager, written in Rust."),
    ("modelcontext/mcp-server-kit", "typescript", ["mcp", "llm"], "Batteries-included toolkit for building Model Context Protocol servers."),
    ("wasmforge/wasmtime-lite", "rust", ["wasm", "rust"], "A slim, embeddable WebAssembly runtime for edge workloads."),
    ("k8s-tools/karpenter-dash", "go", ["kubernetes"], "Real-time autoscaling dashboards for Kubernetes clusters."),
    ("openrobotics/armlink", "c++", ["robotics"], "Deterministic real-time control middleware for robot arms."),
    ("swiftvision/metalcam", "swift", ["robotics"], "On-device camera perception pipelines built on Metal."),
    ("zigtools/zls-next", "zig", [], "A fast, incremental language server for Zig."),
    ("tinystack/litequeue", "python", [], "A tiny durable job queue backed by SQLite."),
    ("graphql-forge/persistiq", "typescript", [], "Persisted-query tooling and cache layer for GraphQL at the edge."),
    ("datatide/duckflow", "python", ["rag"], "Composable data pipelines on DuckDB with incremental materialization."),
]

_FAST_PCT = [
    ("agentsmith/crewpilot", "python", ["ai-agents", "llm"], "Orchestrate cooperative agent crews with typed tool contracts."),
    ("ragworks/chunkforge", "python", ["rag", "llm"], "Layout-aware chunking and evaluation harness for RAG pipelines."),
    ("edgekit/wasm-router", "rust", ["wasm"], "Sub-millisecond HTTP routing compiled to WebAssembly."),
    ("mcphub/gateway", "go", ["mcp"], "A multiplexing gateway that federates MCP servers behind one endpoint."),
    ("neuraldock/promptvault", "typescript", ["llm"], "Versioned prompt registry with diffing and eval hooks."),
    ("robostack/simbridge", "c++", ["robotics"], "Bridge real robot controllers into physics simulators with one config."),
    ("ferrous-labs/axum-htmx", "rust", ["rust"], "First-class htmx helpers and extractors for axum."),
    ("kubewise/drift-detect", "go", ["kubernetes"], "Detect and reconcile configuration drift across fleets of clusters."),
]

_BREAKOUTS = [
    ("launchlab/instantgrid", "typescript", ["wasm"], "A spreadsheet engine that runs entirely in WebAssembly, launched last week."),
    ("newwave/agentlens", "python", ["ai-agents", "llm"], "X-ray tracing and replay for multi-agent LLM runs."),
    ("freshcode/zig-audio", "zig", [], "Real-time audio DSP graph library for Zig, days old and climbing."),
    ("skunkworks/nanorag", "rust", ["rag", "llm"], "A single-binary RAG stack: embed, index, and serve in 12 MB."),
    ("firstlight/swiftserve", "swift", [], "Server-side Swift micro-framework with zero-dependency deploys."),
]

_SUSPICIOUS = [
    ("growthhackr/star-magnet", "javascript", ["llm"], "An 'awesome list' aggregator enjoying a suspiciously smooth star curve."),
    ("viralbits/ai-todo-9000", "typescript", ["ai-agents"], "Yet another AI todo app rocketing up the charts overnight."),
    ("moonshot-ml/hypetrain", "python", ["llm"], "One-file model wrapper trending hard while forks and issues stay flat."),
]

_TINY = [
    ("hobbyist/dotfiles-plus", "javascript", [], "Personal dotfiles manager; tiny repo that doubled from a tweet."),
    ("student-dev/first-crate", "rust", ["rust"], "A learning-project crate that went from 2 to 20 stars."),
    ("weekend-hacks/py-sundial", "python", [], "Calculate sundial layouts; small but growing fast in percentage terms."),
]

_SLEEPY = [
    ("archivedish/old-faithful", "javascript", [], "A once-popular utility now in maintenance mode."),
    ("plateau-io/steadystate", "go", ["kubernetes"], "Feature-complete operator with a flat star curve."),
    ("quietcorner/textutils", "python", [], "Boring-but-reliable text helpers; barely moves week to week."),
    ("stalwart-oss/csvkitql", "python", [], "SQL over CSV files; mature and slow-moving."),
]


def seed_demo(
    conn: sqlite3.Connection,
    settings: Settings,
    edition: str | None = None,
    seed_offset: int = 0,
) -> str:
    """Seed deterministic demo data: ~45 repos, two snapshots each. Returns the edition date.

    ``seed_offset`` shifts the deterministic RNG so distinct weekly editions can
    carry slightly varied (but still fully reproducible) numbers. The default of
    0 reproduces the canonical single-edition demo exactly.
    """
    rng = random.Random(_RNG_SEED + seed_offset)

    if edition is None:
        edition = datetime.now(timezone.utc).date().isoformat()
    edition_dt = datetime.fromisoformat(edition).replace(
        hour=7, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
    )
    ref_dt = edition_dt - timedelta(days=7)
    month_dt = edition_dt - timedelta(days=30)  # ~30-day baseline for monthly %

    # (roster, kind) pairs; kind drives the growth model below.
    plans: list[tuple[list[tuple[str, str, list[str], str]], str]] = [
        (_STEADY, "steady"),
        (_SOLID, "solid"),
        (_FAST_PCT, "fast_pct"),
        (_BREAKOUTS, "breakout"),
        (_SUSPICIOUS, "suspicious"),
        (_TINY, "tiny"),
        (_SLEEPY, "sleepy"),
    ]

    repo_id = 9_000_000
    count = 0
    name_to_id: dict[str, int] = {}
    for roster, kind in plans:
        for full_name, language, topics, description in roster:
            repo_id += 1
            count += 1
            name_to_id[full_name] = repo_id

            if kind == "breakout":
                age_days = rng.randint(8, 20)  # young, but older than the ref snapshot
            elif kind == "tiny":
                age_days = rng.randint(40, 300)
            else:
                age_days = rng.randint(200, 2500)
            created_at = _iso(edition_dt - timedelta(days=age_days))

            # Reference-week stars and this week's delta, by cohort.
            if kind == "steady":
                stars_ref = rng.randint(20_000, 90_000)
                delta = rng.randint(150, 900)
            elif kind == "solid":
                stars_ref = rng.randint(1_200, 9_000)
                delta = rng.randint(120, 1_400)
            elif kind == "fast_pct":
                stars_ref = rng.randint(150, 1_800)
                delta = int(stars_ref * rng.uniform(0.25, 0.9))
            elif kind == "breakout":
                stars_ref = rng.randint(5, 60)
                delta = rng.randint(180, 1_100)
            elif kind == "suspicious":
                stars_ref = rng.randint(300, 1_500)
                delta = rng.randint(450, 1_300)
            elif kind == "tiny":
                stars_ref = rng.randint(2, 30)  # below MIN_STARS_FOR_PCT: floor test
                delta = rng.randint(5, 25)
            else:  # sleepy
                stars_ref = rng.randint(800, 6_000)
                delta = rng.randint(0, 8)
            stars_now = stars_ref + delta

            # ~30-day-ago star count (only meaningful for repos older than a month;
            # breakouts are younger than 30 days, so they get no monthly baseline
            # and monthly % correctly stays unavailable). month_extra is the growth
            # from -30d to -7d, so new_stars_abs_30d = delta + month_extra >= delta
            # and monthly growth lands >= weekly growth for these repos.
            if kind == "steady":
                month_extra = rng.randint(300, 1_800)
            elif kind == "solid":
                month_extra = rng.randint(200, 2_500)
            elif kind == "fast_pct":
                month_extra = int(stars_ref * rng.uniform(0.3, 1.2))
            elif kind == "suspicious":
                month_extra = rng.randint(500, 1_600)
            elif kind == "tiny":
                month_extra = rng.randint(3, 20)
            else:  # sleepy (breakout is skipped below)
                month_extra = rng.randint(0, 10)
            stars_month = max(1, stars_ref - month_extra)

            forks_ref = max(1, int(stars_ref * rng.uniform(0.08, 0.2)))
            subs_ref = max(1, int(stars_ref * rng.uniform(0.01, 0.05)))
            issues_ref = max(0, int(stars_ref * rng.uniform(0.005, 0.03)))

            if kind == "suspicious":
                # Stars soar, everything else is flat: the anomaly-footnote cohort.
                fork_delta = rng.randint(0, max(1, int(delta * 0.01)))
                subs_delta = rng.randint(0, 2)
                issue_delta = rng.randint(-1, 1)
                commits_7d = rng.randint(0, 3)
            elif kind == "sleepy":
                fork_delta, subs_delta, issue_delta = 0, 0, rng.randint(-2, 1)
                commits_7d = rng.randint(0, 2)
            else:
                fork_delta = max(0, int(delta * rng.uniform(0.06, 0.18)))
                subs_delta = max(0, int(delta * rng.uniform(0.01, 0.05)))
                issue_delta = rng.randint(0, max(1, delta // 40))
                commits_7d = rng.randint(5, 120)

            owner = full_name.split("/", 1)[0]
            db.upsert_repo(
                conn,
                {
                    "id": repo_id,
                    "full_name": full_name,
                    "owner": owner,
                    "language": language,
                    "created_at": created_at,
                    "description": description,
                    "homepage": None,
                    "license": rng.choice(["MIT", "Apache-2.0", "BSD-3-Clause", None]),
                    "topics": topics,
                },
            )

            # Third snapshot ~30 days back (skip breakouts — they didn't exist yet).
            if age_days > 30:
                forks_month = max(1, int(stars_month * rng.uniform(0.08, 0.2)))
                subs_month = max(1, int(stars_month * rng.uniform(0.01, 0.05)))
                issues_month = max(0, int(stars_month * rng.uniform(0.005, 0.03)))
                db.insert_snapshot(
                    conn,
                    repo_id,
                    _iso(month_dt),
                    stars=stars_month,
                    forks=forks_month,
                    subscribers=subs_month,
                    open_issues=issues_month,
                    commits_7d=commits_7d,
                    merged_prs_total=rng.randint(0, 4000),
                    releases_90d=rng.randint(0, 12),
                    last_release_at=None,
                    contributors_7d=rng.randint(0, 40),
                )

            db.insert_snapshot(
                conn,
                repo_id,
                _iso(ref_dt),
                stars=stars_ref,
                forks=forks_ref,
                subscribers=subs_ref,
                open_issues=issues_ref,
                commits_7d=commits_7d,
                merged_prs_total=rng.randint(0, 4000),
                releases_90d=rng.randint(0, 12),
                last_release_at=None,
                contributors_7d=rng.randint(0, 40),
            )
            db.insert_snapshot(
                conn,
                repo_id,
                _iso(edition_dt),
                stars=stars_now,
                forks=forks_ref + fork_delta,
                subscribers=subs_ref + subs_delta,
                open_issues=max(0, issues_ref + issue_delta),
                commits_7d=commits_7d,
                merged_prs_total=rng.randint(0, 4000),
                releases_90d=rng.randint(0, 12),
                last_release_at=None,
                contributors_7d=rng.randint(0, 40),
            )

    _seed_social(conn, name_to_id, edition_dt, seed_offset=seed_offset)
    _seed_summary(conn, edition)
    _seed_blurbs(conn)

    db.commit(conn)
    return edition


# A believable, fully-offline explainer so `ghpulse demo` shows the card with no
# network and no real LLM call. Bylined "demo (offline)" so it's never mistaken
# for a real model's output.
_DEMO_SUMMARY = (
    "The local-LLM and agent tooling wave kept setting the pace this week. "
    "astral-sh/uv and ollama/ollama drew the loudest Hacker News threads while "
    "still adding stars in bulk, and ggerganov/llama.cpp rode a steady reddit "
    "hum — the rare case where the buzz and the build line up. Monthly "
    "momentum tells the fuller story: several of these repos are up far more "
    "over the last month than the last week, so the trend is a climb, not a "
    "one-week spike.\n\n"
    "The breakout column is where the genuinely new names surfaced. "
    "skunkworks/nanorag and launchlab/instantgrid are only days old yet already "
    "clearing the stars-per-day bar, and they carry no 30-day history — a "
    "reminder that the fastest risers are, by definition, too young to have a "
    "monthly baseline.\n\n"
    "Watch the divergences. plateau-io/steadystate and student-dev/first-crate "
    "are lighting up social with flat or tiny star curves — talked about, "
    "not yet starred, the earliest signal worth tracking. On the other side, the "
    "‘risers to watch’ cohort (star velocity high, forks and issues "
    "flat) looks more like manufactured momentum than real adoption. Treat those "
    "numbers with a raised eyebrow."
)


# Canned conversational tail for the offline demo: the explainer's opening chat
# turn ends by inviting the reader into the top chat bar with a follow-up
# question and a row of clickable suggestion chips. These are the canonical
# canned strings; render.summary_extras grounds/augments them per edition.
DEMO_FOLLOWUP = "Want me to dig into any of these? Try one:"
DEMO_SUGGESTIONS = (
    "Which of these reduce context in agent sessions?",
    "Fastest-growing Rust projects this week?",
    "What's new in RAG?",
)


def _seed_summary(conn: sqlite3.Connection, edition: str) -> None:
    """Seed a deterministic canned explainer (no network, no real LLM).

    The follow-up question + suggestion chips shown under the explainer come
    from :data:`DEMO_FOLLOWUP` / :data:`DEMO_SUGGESTIONS`, applied at render time
    by :func:`ghpulse.render.summary_extras` (the ``summary`` DB row itself only
    stores the explainer text + model).
    """
    db.upsert_summary(conn, edition, _DEMO_SUMMARY, "demo (offline)")


# Canned "focused description" blurbs — the punchy 'what it DOES' line the user
# sees on each card. Fully offline; mirrors what the LLM refine pass would write
# (verb-first, <=12 words, grounded on the repo's own description/topics). Keyed
# by full_name; stored under desc_hash() of that repo's seeded description so the
# renderer picks them up exactly like real refined blurbs.
_DEMO_BLURBS: dict[str, str] = {
    # steady
    "pytorch/pytorch": "Build and train neural networks with GPU-accelerated tensors",
    "kubernetes/kubernetes": "Schedule and manage containers across production clusters",
    "rust-lang/rust": "Build fast, memory-safe software without a garbage collector",
    "microsoft/typescript": "Add static types to JavaScript and catch bugs early",
    "ziglang/zig": "Write low-level systems code with a simpler C toolchain",
    "apple/swift": "Build fast, safe apps across Apple platforms and servers",
    "denoland/deno": "Run JavaScript and TypeScript securely with no config",
    "ollama/ollama": "Run large language models locally with one command",
    "langchain-ai/langchain": "Orchestrate multi-step AI agent workflows",
    "ggerganov/llama.cpp": "Run LLM inference on your CPU with tiny dependencies",
    "tauri-apps/tauri": "Ship tiny, secure desktop apps with a web frontend",
    "vercel/next.js": "Build production React apps with routing and rendering built in",
    # solid
    "astral-sh/uv": "Install and manage Python projects blazingly fast",
    "modelcontext/mcp-server-kit": "Build Model Context Protocol servers with batteries included",
    "wasmforge/wasmtime-lite": "Embed a slim WebAssembly runtime into edge workloads",
    "k8s-tools/karpenter-dash": "Watch Kubernetes autoscaling live from a dashboard",
    "openrobotics/armlink": "Control robot arms with deterministic real-time middleware",
    "swiftvision/metalcam": "Run camera perception on-device with Metal pipelines",
    "zigtools/zls-next": "Get fast, incremental language-server hints for Zig",
    "tinystack/litequeue": "Queue durable background jobs on plain SQLite",
    "graphql-forge/persistiq": "Cache and persist GraphQL queries at the edge",
    "datatide/duckflow": "Build incremental data pipelines on DuckDB",
    # fast_pct
    "agentsmith/crewpilot": "Coordinate cooperative AI agents with typed tool contracts",
    "ragworks/chunkforge": "Chunk and evaluate documents for better RAG pipelines",
    "edgekit/wasm-router": "Route HTTP in sub-milliseconds compiled to WebAssembly",
    "mcphub/gateway": "Federate many MCP servers behind one endpoint",
    "neuraldock/promptvault": "Version and diff prompts with built-in eval hooks",
    "robostack/simbridge": "Bridge real robot controllers into physics simulators",
    "ferrous-labs/axum-htmx": "Add first-class htmx helpers to axum web apps",
    "kubewise/drift-detect": "Detect and reconcile config drift across clusters",
    # breakouts
    "launchlab/instantgrid": "Run a spreadsheet engine entirely in WebAssembly",
    "newwave/agentlens": "Trace and replay multi-agent LLM runs step by step",
    "freshcode/zig-audio": "Build real-time audio DSP graphs in Zig",
    "skunkworks/nanorag": "Embed, index, and serve RAG from a single binary",
    "firstlight/swiftserve": "Deploy server-side Swift services with zero dependencies",
    # suspicious
    "growthhackr/star-magnet": "Aggregate awesome-lists into one curated index",
    "viralbits/ai-todo-9000": "Manage your todo list with an AI assistant",
    "moonshot-ml/hypetrain": "Wrap any model behind a single-file inference API",
    # tiny
    "hobbyist/dotfiles-plus": "Manage and sync your dotfiles across machines",
    "student-dev/first-crate": "Learn Rust by shipping a tiny published crate",
    "weekend-hacks/py-sundial": "Calculate sundial layouts for any latitude",
    # sleepy
    "archivedish/old-faithful": "Handle everyday utility tasks with a stable helper",
    "plateau-io/steadystate": "Operate a feature-complete Kubernetes controller",
    "quietcorner/textutils": "Clean and transform text with reliable helpers",
    "stalwart-oss/csvkitql": "Query CSV files with plain SQL",
}


def _seed_blurbs(conn: sqlite3.Connection) -> None:
    """Seed canned focused blurbs keyed by desc_hash of each repo's description.

    Fully offline. The renderer looks up ``get_blurb(full_name, desc_hash(desc))``
    so keying by the SAME seeded description makes these show on the cards.
    """
    rosters = (_STEADY, _SOLID, _FAST_PCT, _BREAKOUTS, _SUSPICIOUS, _TINY, _SLEEPY)
    generated_at = db.now_iso()
    for roster in rosters:
        for full_name, _language, _topics, description in roster:
            blurb = _DEMO_BLURBS.get(full_name)
            if not blurb:
                continue
            db.upsert_blurb(
                conn,
                full_name,
                db.desc_hash(description),
                blurb,
                "demo (offline)",
                generated_at,
            )

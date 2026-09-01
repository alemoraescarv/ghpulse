"""Typer CLI for ghpulse: discover, snapshot, score, and render GitHub trends."""

from __future__ import annotations

import http.server
import socketserver
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer

from ghpulse import (
    agent,
    collect,
    config,
    db,
    demo,
    discover,
    http as gh_http,
    hype,
    llm,
    panel,
    ratelimit,
    render,
    score,
    social,
)

app = typer.Typer(name="ghpulse", help="Snapshot GitHub daily and render a weekly tech news page.")


def _most_recent_monday() -> str:
    """Return the most recent Monday (today if today is Monday) as YYYY-MM-DD."""
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    return monday.isoformat()




def _setup() -> tuple[config.Settings, sqlite3.Connection]:
    settings = config.get_settings()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    return settings, conn


def _client(settings: config.Settings, conn: sqlite3.Connection) -> gh_http.GitHubClient:
    limiter = ratelimit.RateLimiter()
    return gh_http.GitHubClient(settings.token, limiter=limiter, conn=conn)


def _require_token(settings: config.Settings) -> None:
    if settings.token is None:
        typer.echo(
            "No GitHub token found. Set GITHUB_TOKEN or add GITHUB_TOKEN=... to "
            "~/.config/ghpulse/env.\nTip: run `ghpulse demo` to try ghpulse fully "
            "offline with seeded data."
        )
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Print paths and, if a token is present, current API rate budgets."""
    settings, conn = _setup()
    typer.echo(f"home:    {settings.home}")
    typer.echo(f"db:      {settings.db_path}")
    typer.echo(f"site:    {settings.site_dir}")
    typer.echo(f"config:  {settings.config_dir}")
    if settings.token is None:
        typer.echo("token:   none (demo mode ok — try `ghpulse demo`)")
    else:
        typer.echo("token:   present")
        client = _client(settings, conn)
        info = client.rate_limit()
        resources = info.get("resources", info)
        for family in ("core", "search", "graphql"):
            budget = resources.get(family)
            if budget:
                typer.echo(
                    f"  {family}: {budget.get('remaining')}/{budget.get('limit')} remaining"
                )
    # LLM explainer backend status (available() is key-only for anthropic and a
    # fast localhost probe for ollama — no expensive network calls).
    backend = llm.select_backend(settings)
    if backend is None:
        typer.echo("llm:     off (set GHPULSE_LLM=ollama|anthropic|auto to enable)")
    else:
        state = "available" if backend.available() else "unavailable"
        typer.echo(f"llm:     {settings.llm} -> {backend.name} ({state})")
    # P4: free local agent + control panel status (cheap, offline-safe probes).
    ollama_up = agent.base.ollama_available(settings.ollama_url)
    typer.echo(
        f"agent:   model {settings.agent_model} via {settings.ollama_url} "
        f"({'reachable' if ollama_up else 'offline — run `ghpulse setup` or start Ollama'})"
    )
    typer.echo(f"panel:   `ghpulse panel` on http://127.0.0.1:{settings.panel_port}/")
    conn.close()


@app.command("discover")
def discover_cmd(
    since: Optional[str] = typer.Argument(None, help="Window start (YYYY-MM-DD); default scans the last 7 and 30 days."),
) -> None:
    """Run sharded search queries and track newly found repos."""
    settings, conn = _setup()
    _require_token(settings)
    if since:
        windows = [(since, None)]
        label = f"since {since}"
    else:
        today = datetime.now(timezone.utc).date()
        week_ago = (today - timedelta(days=7)).isoformat()
        month_ago = (today - timedelta(days=30)).isoformat()
        windows = [(week_ago, None), (month_ago, week_ago)]
        label = "last 7 + 30 days"
    client = _client(settings, conn)
    count = discover.discover(client, conn, windows, settings)
    db.commit(conn)
    typer.echo(f"discovered {count} repos ({label})")
    conn.close()


@app.command()
def daily() -> None:
    """Snapshot all tracked repos at the current time."""
    settings, conn = _setup()
    _require_token(settings)
    client = _client(settings, conn)
    captured_at = db.now_iso()
    count = collect.snapshot_all(client, conn, captured_at)
    db.commit(conn)
    typer.echo(f"snapshotted {count} repos at {captured_at}")
    conn.close()


@app.command()
def weekly(render_site: bool = typer.Option(True, "--render/--no-render", help="Render the site after scoring.")) -> None:
    """Discover + snapshot + compute metrics (+ render)."""
    settings, conn = _setup()
    _require_token(settings)
    # Scan two disjoint windows: the last 7 days AND the prior 8–30 days, so we
    # catch both this week's new/active repos and the month's slower movers
    # (which also feeds the monthly % metrics) without either window eating the
    # other's per-query result cap.
    today = datetime.now(timezone.utc).date()
    week_ago = (today - timedelta(days=7)).isoformat()
    month_ago = (today - timedelta(days=30)).isoformat()
    windows = [(week_ago, None), (month_ago, week_ago)]
    client = _client(settings, conn)
    discovered = discover.discover(client, conn, windows, settings)
    captured_at = db.now_iso()
    snapshotted = collect.snapshot_all(client, conn, captured_at)
    edition = datetime.now(timezone.utc).date().isoformat()
    sections = score.compute_metrics(conn, edition)
    db.commit(conn)
    # Social hype is best-effort: a dead API must never fail the weekly run.
    mentions = 0
    try:
        mentions = social.fetch_social(conn, None, settings)
        hype.compute_hype(conn, edition)
        sections = hype.merge_hype_sections(conn, edition, sections)
    except Exception as exc:  # pragma: no cover - resilience path
        typer.echo(f"social layer skipped: {exc}")
    # LLM explainer is best-effort: a summarize failure must never fail weekly.
    explained = False
    try:
        explained = llm.summarize_edition(conn, edition, settings) is not None
    except Exception as exc:  # pragma: no cover - resilience path
        typer.echo(f"summary skipped: {exc}")
    # Focused per-repo blurbs are best-effort too: never fail weekly on refine.
    refined = 0
    try:
        refined = llm.refine_descriptions(conn, edition, settings)
    except Exception as exc:  # pragma: no cover - resilience path
        typer.echo(f"refine skipped: {exc}")
    # News trend paragraphs are best-effort too: never fail weekly on group refine.
    grouped = 0
    try:
        grouped = llm.refine_news_groups(conn, edition, settings)
    except Exception as exc:  # pragma: no cover - resilience path
        typer.echo(f"group refine skipped: {exc}")
    summary = (
        f"edition {edition}: discovered {discovered}, snapshotted {snapshotted}, "
        f"cohort {sections.get('cohort_size')}, mentions {mentions}, "
        f"explained {explained}, refined {refined}, grouped {grouped}"
    )
    if render_site:
        out = render.render_edition(conn, edition, settings, sections=sections)
        summary += f", rendered {out}"
    typer.echo(summary)
    conn.close()


@app.command("render")
def render_cmd(
    edition: Optional[str] = typer.Argument(None, help="Edition date (YYYY-MM-DD); defaults to latest."),
) -> None:
    """Render an edition's static page (and rebuild the site index)."""
    settings, conn = _setup()
    target = edition or score.latest_edition(conn)
    if target is None:
        typer.echo("no editions found — run `ghpulse weekly` or `ghpulse demo` first")
        raise typer.Exit(code=1)
    out = render.render_edition(conn, target, settings)
    typer.echo(str(out))
    conn.close()


@app.command("social")
def social_cmd(
    edition: Optional[str] = typer.Argument(None, help="Edition date (YYYY-MM-DD); defaults to latest."),
    limit: int = typer.Option(200, help="Number of top repos to search for mentions."),
) -> None:
    """Fetch social mentions, compute hype, and re-render the edition."""
    settings, conn = _setup()
    target = edition or score.latest_edition(conn)
    if target is None:
        typer.echo("no editions found — run `ghpulse weekly` or `ghpulse demo` first")
        raise typer.Exit(code=1)
    mentions = social.fetch_social(conn, None, settings, limit=limit)
    hype.compute_hype(conn, target)
    db.commit(conn)
    out = render.render_edition(conn, target, settings)
    typer.echo(f"edition {target}: {mentions} mentions, rendered {out}")
    conn.close()


@app.command("summarize")
def summarize_cmd(
    edition: Optional[str] = typer.Option(
        None, "--edition", help="Edition date (YYYY-MM-DD); defaults to latest."
    ),
) -> None:
    """Build the digest, run the selected LLM backend, store the summary, re-render."""
    settings, conn = _setup()
    target = edition or score.latest_edition(conn)
    if target is None:
        typer.echo("no editions found — run `ghpulse weekly` or `ghpulse demo` first")
        raise typer.Exit(code=1)
    backend = llm.select_backend(settings)
    if backend is None:
        typer.echo(
            "LLM is off — set GHPULSE_LLM=ollama|anthropic|auto (see `ghpulse doctor`)"
        )
        raise typer.Exit(code=1)
    text = llm.summarize_edition(conn, target, settings)
    if text is None:
        typer.echo(
            f"backend {backend.name} produced no summary (unavailable or empty)"
        )
    else:
        typer.echo(f"summary stored for {target} via {backend.name} ({len(text)} chars)")
    out = render.render_edition(conn, target, settings)
    typer.echo(str(out))
    conn.close()


@app.command("refine")
def refine_cmd(
    edition: Optional[str] = typer.Option(
        None, "--edition", help="Edition date (YYYY-MM-DD); defaults to latest."
    ),
) -> None:
    """Generate focused 'what it DOES' blurbs for shown repos, then re-render."""
    settings, conn = _setup()
    target = edition or score.latest_edition(conn)
    if target is None:
        typer.echo("no editions found — run `ghpulse weekly` or `ghpulse demo` first")
        raise typer.Exit(code=1)
    backend = llm.select_backend(settings)
    if backend is None:
        typer.echo(
            "LLM is off — set GHPULSE_LLM=ollama|anthropic|auto to enable focused "
            "descriptions (see `ghpulse doctor`). GitHub descriptions still show."
        )
        raise typer.Exit(code=1)
    written = llm.refine_descriptions(conn, target, settings)
    grouped = llm.refine_news_groups(conn, target, settings)
    typer.echo(
        f"refined {written} repo blurb(s) and {grouped} news group(s) for "
        f"{target} via {backend.name}"
    )
    out = render.render_edition(conn, target, settings)
    typer.echo(str(out))
    conn.close()


@app.command("demo")
def demo_cmd(
    serve: bool = typer.Option(False, "--serve", help="Serve the site after rendering."),
    weeks: int = typer.Option(5, "--weeks", help="How many weekly editions to render."),
) -> None:
    """Seed deterministic offline demo data and render ~5 weekly editions.

    Renders one full edition per week (oldest -> newest, ending on the most
    recent Monday) so the edition timeline is populated and clickable. Each week
    uses its own in-memory database seeded with a per-week offset, so numbers
    vary slightly week to week while staying fully deterministic and offline.
    """
    settings, base_conn = _setup()
    base_conn.close()  # each edition renders from its own isolated in-memory DB

    weeks = max(1, weeks)
    anchor = datetime.fromisoformat(_most_recent_monday()).date()
    # Oldest -> newest so the newest edition is rendered last (and sorts current).
    week_dates = [
        (anchor - timedelta(weeks=k)).isoformat() for k in range(weeks - 1, -1, -1)
    ]

    def _render_one(offset: int, edition: str) -> Path:
        conn = db.connect(":memory:")
        db.init_db(conn)
        demo.seed_demo(conn, settings, edition=edition, seed_offset=offset)
        sections = score.compute_metrics(conn, edition)
        hype.compute_hype(conn, edition)
        sections = hype.merge_hype_sections(conn, edition, sections)
        db.commit(conn)
        out = render.render_edition(conn, edition, settings, sections=sections)
        conn.close()
        return out

    # Pass 1: render every edition so all sibling directories exist on disk.
    last_out: Optional[Path] = None
    for offset, edition in enumerate(week_dates):
        last_out = _render_one(offset, edition)
        typer.echo(f"rendered edition {edition}")

    # Pass 2: re-render so each edition's timeline sees ALL siblings (the earlier
    # editions were rendered before the later ones existed on disk).
    if len(week_dates) > 1:
        for offset, edition in enumerate(week_dates):
            last_out = _render_one(offset, edition)

    if last_out is not None:
        typer.echo(f"file://{last_out}")
        typer.echo(f"file://{Path(settings.site_dir) / 'index.html'}")
    if serve:
        _serve(settings.site_dir, 8383)


@app.command("serve")
def serve_cmd(port: int = typer.Option(8383, help="Port to serve on.")) -> None:
    """Serve the generated site over HTTP."""
    settings, conn = _setup()
    conn.close()
    _serve(settings.site_dir, port)


def _serve(site_dir: Path, port: int) -> None:
    handler_cls = http.server.SimpleHTTPRequestHandler

    class Handler(handler_cls):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, directory=str(site_dir), **kwargs)  # type: ignore[arg-type]

    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        typer.echo(f"serving {site_dir} at http://127.0.0.1:{port}/ (Ctrl-C to stop)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            typer.echo("stopped")


@app.command("ask")
def ask_cmd(
    question: str = typer.Argument(..., help="What to research on GitHub, in plain English."),
) -> None:
    """Ask the FREE local agentic GitHub researcher (Ollama, no API key)."""
    settings, conn = _setup()
    try:
        result = agent.run_agent(question, settings, conn=conn)
    finally:
        conn.close()
    typer.echo(result.answer)
    if result.steps:
        typer.echo("")
        typer.echo("steps:")
        for step in result.steps:
            summary = (step.result_summary or "").replace("\n", " ")
            if len(summary) > 120:
                summary = summary[:120].rstrip() + " …"
            typer.echo(f"  - {step.tool}({step.args}) -> {summary}")


@app.command("panel")
def panel_cmd(
    port: Optional[int] = typer.Option(None, "--port", help="Port (default settings.panel_port / 8765)."),
    open_browser: bool = typer.Option(False, "--open", help="Open the panel in a browser."),
) -> None:
    """Serve the localhost glass control panel (pick backend, refresh, ask)."""
    settings = config.get_settings()
    panel.serve(settings, port=port, open_browser=open_browser)


if __name__ == "__main__":
    app()

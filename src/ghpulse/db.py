"""SQLite storage layer for ghpulse (WAL, Row factory, idempotent schema)."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS repo (
  id          INTEGER PRIMARY KEY,   -- GitHub numeric repo id
  full_name   TEXT NOT NULL, owner TEXT NOT NULL, language TEXT,
  created_at  TEXT NOT NULL, description TEXT, homepage TEXT, license TEXT,
  first_seen  TEXT NOT NULL, is_tracked INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS repo_topic (
  repo_id INTEGER REFERENCES repo(id), topic TEXT NOT NULL,
  PRIMARY KEY (repo_id, topic)
);

-- Append-only. One row per repo per run. THE core table.
CREATE TABLE IF NOT EXISTS snapshot (
  repo_id INTEGER NOT NULL REFERENCES repo(id),
  captured_at TEXT NOT NULL,
  stars INTEGER NOT NULL, forks INTEGER NOT NULL,
  subscribers INTEGER, open_issues INTEGER,
  commits_7d INTEGER, merged_prs_total INTEGER,
  releases_90d INTEGER, last_release_at TEXT, contributors_7d INTEGER,
  PRIMARY KEY (repo_id, captured_at)
);

CREATE TABLE IF NOT EXISTS usage_signal (   -- P3: external adoption, sparse
  repo_id INTEGER REFERENCES repo(id), captured_at TEXT NOT NULL,
  source TEXT NOT NULL,        -- 'npm' | 'crates' | 'pypi' | 'dependents'
  value INTEGER NOT NULL,
  PRIMARY KEY (repo_id, captured_at, source)
);

CREATE TABLE IF NOT EXISTS metric (         -- computed per edition, denormalized for fast render
  edition TEXT NOT NULL, repo_id INTEGER REFERENCES repo(id),
  name TEXT NOT NULL, value REAL NOT NULL, rank INTEGER,
  PRIMARY KEY (edition, repo_id, name)
);

CREATE TABLE IF NOT EXISTS http_cache (     -- conditional requests
  url TEXT PRIMARY KEY, etag TEXT, fetched_at TEXT NOT NULL, body BLOB
);

CREATE TABLE IF NOT EXISTS run_log (
  run_id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  requests_rest INTEGER, requests_search INTEGER, requests_graphql INTEGER,
  repos_snapshotted INTEGER, status TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS social_mention (  -- P2: cross-platform buzz, one row per post
  repo_id     INTEGER NOT NULL REFERENCES repo(id),
  platform    TEXT NOT NULL,          -- 'hn' | 'reddit' | 'bluesky' | 'lobsters' | 'mastodon'
  post_id     TEXT NOT NULL,          -- platform-native id (dedup key)
  url         TEXT,                   -- link to the post/thread
  title       TEXT,
  engagement  INTEGER NOT NULL,       -- normalized points+comments
  posted_at   TEXT NOT NULL,          -- ISO8601, for recency decay
  captured_at TEXT NOT NULL,
  PRIMARY KEY (platform, post_id)
);

CREATE TABLE IF NOT EXISTS summary (   -- P3: LLM "what happened" explainer, one row per edition
  edition      TEXT PRIMARY KEY,
  text         TEXT NOT NULL,
  model        TEXT NOT NULL,          -- e.g. 'claude-opus-4-8' | 'llama3.1:8b (local)' | 'demo (offline)'
  generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS repo_blurb (  -- P4: LLM "focused description" per repo, keyed by desc hash
  full_name    TEXT NOT NULL,
  desc_hash    TEXT NOT NULL,           -- sha256(normalized description), truncated: cache/version key
  blurb        TEXT NOT NULL,           -- one punchy "what it DOES" line
  model        TEXT NOT NULL,           -- e.g. 'claude-opus-4-8' | 'llama3.1:8b (local)' | 'demo (offline)'
  generated_at TEXT NOT NULL,
  PRIMARY KEY (full_name, desc_hash)
);

CREATE TABLE IF NOT EXISTS group_blurb (  -- News view: LLM-refined trend paragraph per topical group, per edition
  edition      TEXT NOT NULL,
  tag_id       TEXT NOT NULL,             -- topical category id (e.g. 'ai-agents')
  blurb        TEXT NOT NULL,             -- the trend-story paragraph
  model        TEXT NOT NULL,             -- e.g. 'claude-opus-4-8' | 'llama3.1:8b (local)'
  generated_at TEXT NOT NULL,
  PRIMARY KEY (edition, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_repo_time ON snapshot(repo_id, captured_at);
CREATE INDEX IF NOT EXISTS idx_metric_edition_name ON metric(edition, name, rank);
CREATE INDEX IF NOT EXISTS idx_social_repo ON social_mention(repo_id, posted_at);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database with WAL mode and Row factory."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables and indexes. Idempotent."""
    conn.executescript(SCHEMA)
    conn.commit()


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def upsert_repo(conn: sqlite3.Connection, repo: dict) -> None:
    """Insert or update a repo row (preserving first_seen), replace its topics,
    and mark it tracked.

    Expected keys: id, full_name, owner, language, created_at, description,
    homepage, license, topics (list[str]).
    """
    first_seen = now_iso()
    conn.execute(
        """
        INSERT INTO repo (id, full_name, owner, language, created_at,
                          description, homepage, license, first_seen, is_tracked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(id) DO UPDATE SET
          full_name   = excluded.full_name,
          owner       = excluded.owner,
          language    = excluded.language,
          created_at  = excluded.created_at,
          description = excluded.description,
          homepage    = excluded.homepage,
          license     = excluded.license,
          is_tracked  = 1
        """,
        (
            repo["id"],
            repo["full_name"],
            repo["owner"],
            repo.get("language"),
            repo["created_at"],
            repo.get("description"),
            repo.get("homepage"),
            repo.get("license"),
            first_seen,
        ),
    )
    conn.execute("DELETE FROM repo_topic WHERE repo_id = ?", (repo["id"],))
    topics: list[str] = repo.get("topics") or []
    conn.executemany(
        "INSERT OR IGNORE INTO repo_topic (repo_id, topic) VALUES (?, ?)",
        [(repo["id"], t) for t in topics],
    )


def insert_snapshot(
    conn: sqlite3.Connection,
    repo_id: int,
    captured_at: str,
    *,
    stars: int,
    forks: int,
    subscribers: int | None = None,
    open_issues: int | None = None,
    commits_7d: int | None = None,
    merged_prs_total: int | None = None,
    releases_90d: int | None = None,
    last_release_at: str | None = None,
    contributors_7d: int | None = None,
) -> None:
    """Insert (or replace) one snapshot row for (repo_id, captured_at)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO snapshot (
          repo_id, captured_at, stars, forks, subscribers, open_issues,
          commits_7d, merged_prs_total, releases_90d, last_release_at, contributors_7d
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo_id,
            captured_at,
            stars,
            forks,
            subscribers,
            open_issues,
            commits_7d,
            merged_prs_total,
            releases_90d,
            last_release_at,
            contributors_7d,
        ),
    )


def tracked_repo_ids(conn: sqlite3.Connection) -> list[int]:
    """All repo ids with is_tracked=1, ascending."""
    rows = conn.execute(
        "SELECT id FROM repo WHERE is_tracked = 1 ORDER BY id"
    ).fetchall()
    return [row["id"] for row in rows]


def repo_row(conn: sqlite3.Connection, repo_id: int) -> sqlite3.Row | None:
    """The repo row for repo_id, or None."""
    return conn.execute("SELECT * FROM repo WHERE id = ?", (repo_id,)).fetchone()


def topics_for_repo(conn: sqlite3.Connection, repo_id: int) -> list[str]:
    """All topics for repo_id, sorted ascending (deterministic)."""
    rows = conn.execute(
        "SELECT topic FROM repo_topic WHERE repo_id = ? ORDER BY topic",
        (repo_id,),
    ).fetchall()
    return [row["topic"] for row in rows]


def latest_snapshot(conn: sqlite3.Connection, repo_id: int) -> sqlite3.Row | None:
    """Most recent snapshot for repo_id, or None."""
    return conn.execute(
        "SELECT * FROM snapshot WHERE repo_id = ? ORDER BY captured_at DESC LIMIT 1",
        (repo_id,),
    ).fetchone()


def snapshot_on_or_before(
    conn: sqlite3.Connection, repo_id: int, iso_ts: str
) -> sqlite3.Row | None:
    """Most recent snapshot with captured_at <= iso_ts, or None."""
    return conn.execute(
        """
        SELECT * FROM snapshot
        WHERE repo_id = ? AND captured_at <= ?
        ORDER BY captured_at DESC LIMIT 1
        """,
        (repo_id, iso_ts),
    ).fetchone()


def earliest_snapshot(conn: sqlite3.Connection, repo_id: int) -> sqlite3.Row | None:
    """Oldest snapshot for repo_id, or None."""
    return conn.execute(
        "SELECT * FROM snapshot WHERE repo_id = ? ORDER BY captured_at ASC LIMIT 1",
        (repo_id,),
    ).fetchone()


def snapshot_series(
    conn: sqlite3.Connection, repo_id: int, limit: int = 30
) -> list[sqlite3.Row]:
    """The last `limit` snapshots for repo_id in chronological order (for sparklines)."""
    rows = conn.execute(
        """
        SELECT * FROM (
          SELECT * FROM snapshot
          WHERE repo_id = ?
          ORDER BY captured_at DESC LIMIT ?
        ) ORDER BY captured_at ASC
        """,
        (repo_id, limit),
    ).fetchall()
    return rows


def write_metrics(
    conn: sqlite3.Connection, edition: str, rows: list[tuple]
) -> None:
    """Replace metric rows for this edition. Each row: (repo_id, name, value, rank).

    Clears every metric name for the edition before inserting so that a metric
    whose row set becomes empty on recompute does not leave stale ranked rows.
    """
    conn.execute("DELETE FROM metric WHERE edition = ?", (edition,))
    conn.executemany(
        """
        INSERT OR REPLACE INTO metric (edition, repo_id, name, value, rank)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(edition, repo_id, name, value, rank) for repo_id, name, value, rank in rows],
    )


def add_metrics(conn: sqlite3.Connection, edition: str, rows: list[tuple]) -> None:
    """Insert (or replace) metric rows for an edition WITHOUT clearing existing ones.

    Unlike ``write_metrics`` (which wipes the whole edition first), this layers
    additional metric names onto an edition that has already been scored — used
    by the social hype pass, which runs after ``score.compute_metrics``.
    Each row: (repo_id, name, value, rank).
    """
    conn.executemany(
        """
        INSERT OR REPLACE INTO metric (edition, repo_id, name, value, rank)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(edition, repo_id, name, value, rank) for repo_id, name, value, rank in rows],
    )


def insert_mention(
    conn: sqlite3.Connection,
    repo_id: int,
    platform: str,
    post_id: str,
    url: str | None,
    title: str | None,
    engagement: int,
    posted_at: str,
    captured_at: str,
) -> None:
    """Insert (or replace) one social mention, deduped on (platform, post_id)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO social_mention (
          repo_id, platform, post_id, url, title, engagement, posted_at, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (repo_id, platform, post_id, url, title, int(engagement), posted_at, captured_at),
    )


def mentions_for_repo(
    conn: sqlite3.Connection, repo_id: int, since_iso: str
) -> list[sqlite3.Row]:
    """All social mentions for repo_id with posted_at >= since_iso, newest first."""
    return conn.execute(
        """
        SELECT * FROM social_mention
        WHERE repo_id = ? AND posted_at >= ?
        ORDER BY posted_at DESC
        """,
        (repo_id, since_iso),
    ).fetchall()


def upsert_summary(
    conn: sqlite3.Connection,
    edition: str,
    text: str,
    model: str,
    generated_at: str | None = None,
) -> None:
    """Insert (or replace) the LLM explainer summary for an edition."""
    conn.execute(
        """
        INSERT OR REPLACE INTO summary (edition, text, model, generated_at)
        VALUES (?, ?, ?, ?)
        """,
        (edition, text, model, generated_at or now_iso()),
    )


def get_summary(conn: sqlite3.Connection, edition: str) -> sqlite3.Row | None:
    """The stored summary row for an edition, or None."""
    return conn.execute(
        "SELECT * FROM summary WHERE edition = ?", (edition,)
    ).fetchone()


def desc_hash(description: str | None) -> str:
    """Stable short key for a repo description.

    Normalizes (collapse whitespace, casefold) then returns a truncated sha256
    hex digest. Pure and deterministic — the SAME helper is used by the refine
    pass (to key stored blurbs) and by the renderer (to look them up), so a
    description edit invalidates the cached blurb automatically.
    """
    normalized = " ".join((description or "").split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def upsert_blurb(
    conn: sqlite3.Connection,
    full_name: str,
    desc_hash: str,
    blurb: str,
    model: str,
    generated_at: str | None = None,
) -> None:
    """Insert (or replace) the focused blurb for (full_name, desc_hash)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO repo_blurb (full_name, desc_hash, blurb, model, generated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (full_name, desc_hash, blurb, model, generated_at or now_iso()),
    )


def get_blurb(
    conn: sqlite3.Connection, full_name: str, desc_hash: str
) -> str | None:
    """The stored blurb text for (full_name, desc_hash), or None."""
    row = conn.execute(
        "SELECT blurb FROM repo_blurb WHERE full_name = ? AND desc_hash = ?",
        (full_name, desc_hash),
    ).fetchone()
    return row["blurb"] if row is not None else None


def upsert_group_blurb(
    conn: sqlite3.Connection,
    edition: str,
    tag_id: str,
    blurb: str,
    model: str,
    generated_at: str | None = None,
) -> None:
    """Insert (or replace) the LLM trend-story paragraph for (edition, tag_id)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO group_blurb (edition, tag_id, blurb, model, generated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (edition, tag_id, blurb, model, generated_at or now_iso()),
    )


def get_group_blurb(
    conn: sqlite3.Connection, edition: str, tag_id: str
) -> str | None:
    """The stored trend-story paragraph for (edition, tag_id), or None."""
    row = conn.execute(
        "SELECT blurb FROM group_blurb WHERE edition = ? AND tag_id = ?",
        (edition, tag_id),
    ).fetchone()
    return row["blurb"] if row is not None else None


def get_group_blurbs(conn: sqlite3.Connection, edition: str) -> dict[str, str]:
    """All stored trend-story paragraphs for an edition, keyed by tag_id."""
    rows = conn.execute(
        "SELECT tag_id, blurb FROM group_blurb WHERE edition = ?", (edition,)
    ).fetchall()
    return {row["tag_id"]: row["blurb"] for row in rows}


def search_tracked(
    conn: sqlite3.Connection,
    terms: "str | list[str]",
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    """Search the tracked cohort for repos matching ANY term. Pure, offline.

    Matches a term against the repo's full_name / description / topic (substring)
    or language (exact). Ranks by a simple relevance score summed per term
    (name-match=3, topic=2, description=1) then stars desc. Each result carries
    the latest-snapshot stars/forks/commits_7d, the weekly/monthly star-growth %
    pulled from the persisted metric table for the latest edition, its topical
    tags, and ``tracked: True``. Returns the ``[offset:offset+limit]`` page.

    Never raises for a bad query — an empty/blank term list yields ``[]``.
    """
    from . import score, tags  # local imports avoid an import cycle (score->db)

    if isinstance(terms, str):
        terms = terms.split()
    norm_terms = [t.strip().lower() for t in (terms or []) if t and t.strip()]
    if not norm_terms:
        return []

    # Weekly / monthly star-growth % from the latest edition's metric rows.
    edition = score.latest_edition(conn)
    week_val: dict[int, float] = {}
    month_val: dict[int, float] = {}
    if edition:
        for r in conn.execute(
            "SELECT repo_id, value FROM metric WHERE edition = ? AND name = ?",
            (edition, "star_growth_pct"),
        ):
            week_val[r["repo_id"]] = r["value"]
        for r in conn.execute(
            "SELECT repo_id, value FROM metric WHERE edition = ? AND name = ?",
            (edition, "star_growth_pct_30d"),
        ):
            month_val[r["repo_id"]] = r["value"]

    # Latest snapshot per tracked repo (MAX(captured_at) join).
    rows = conn.execute(
        """
        SELECT r.id, r.full_name, r.description, r.language,
               s.stars, s.forks, s.commits_7d
        FROM repo r
        JOIN snapshot s ON s.repo_id = r.id
        JOIN (
          SELECT repo_id, MAX(captured_at) AS mx FROM snapshot GROUP BY repo_id
        ) m ON m.repo_id = s.repo_id AND m.mx = s.captured_at
        WHERE r.is_tracked = 1
        """
    ).fetchall()

    scored: list[tuple[int, int, str, dict]] = []
    for row in rows:
        rid = row["id"]
        name_l = (row["full_name"] or "").lower()
        desc_l = (row["description"] or "").lower()
        lang_l = (row["language"] or "").lower()
        topics = topics_for_repo(conn, rid)
        topics_l = [t.lower() for t in topics]

        relevance = 0
        matched = False
        for term in norm_terms:
            if term in name_l:
                relevance += 3
                matched = True
            if any(term in tp for tp in topics_l):
                relevance += 2
                matched = True
            if term in desc_l:
                relevance += 1
                matched = True
            if lang_l and lang_l == term:
                matched = True
        if not matched:
            continue

        tag_ids = tags.classify(
            {
                "full_name": row["full_name"],
                "description": row["description"],
                "language": row["language"],
                "topics": topics,
            }
        )
        week_pct = week_val.get(rid)
        month_pct = month_val.get(rid)
        result = {
            "full_name": row["full_name"],
            "url": f"https://github.com/{row['full_name']}",
            "description": row["description"],
            "language": row["language"],
            "stars": row["stars"],
            "forks": row["forks"],
            "commits_7d": row["commits_7d"],
            "week_pct": round(week_pct) if week_pct is not None else None,
            "month_pct": round(month_pct) if month_pct is not None else None,
            "tags": tag_ids,
            "tracked": True,
        }
        scored.append((relevance, int(row["stars"] or 0), row["full_name"], result))

    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
    try:
        offset = max(0, int(offset))
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        offset, limit = 0, 20
    page = scored[offset : offset + limit] if limit else scored[offset:]
    return [item for _rel, _stars, _name, item in page]


def commit(conn: sqlite3.Connection) -> None:
    conn.commit()

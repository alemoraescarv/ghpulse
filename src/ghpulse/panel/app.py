"""Stdlib control-panel server for ghpulse.

A tiny ``http.server``-based control panel bound to 127.0.0.1. No new
dependencies: only the Python standard library plus ghpulse's own modules
(imported lazily inside the background jobs so the server can start even if a
heavy import is momentarily unhappy).

Endpoints
---------
GET  /                 -> the glass-UI control panel (panel/templates/panel.html)
GET  /api/status       -> {backend, ollama_available, latest_edition, busy, error, ...}
POST /api/config       -> {backend: "anthropic"|"ollama"|"off"} -> persist -> status
POST /api/refresh      -> start a background pipeline+render, returns {started: bool}
POST /api/ask          -> {question} -> run the local agent in the background
GET  /api/ask/result   -> latest AgentResult (or {ready: false})
POST /api/ask_edition  -> {question} -> answer grounded on this week's repos
GET  /api/ask_edition/result -> {ready, busy, question, answer, cited, error}
GET  /news             -> serve the latest rendered site/<edition>/index.html

Everything is guarded: a failed background job records an error into the panel
status and never kills the server.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

# Map the panel's backend labels to the GHPULSE_LLM env value used elsewhere.
_BACKEND_TO_LLM = {"anthropic": "anthropic", "ollama": "ollama", "off": "off"}
_VALID_BACKENDS = tuple(_BACKEND_TO_LLM)

_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "panel.html"

DEFAULT_PANEL_PORT = 8765


# --------------------------------------------------------------------------- #
# Config env-file persistence (mirrors config.load_env_value's file format)    #
# --------------------------------------------------------------------------- #
def write_env_value(config_dir: Path, key: str, value: str) -> Path:
    """Upsert ``KEY=value`` into ``config_dir/env``, preserving other lines.

    Returns the path to the env file. Creates the directory if needed. Matches
    the plain and ``export KEY=`` line forms understood by config.load_env_value.
    """
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    env_file = config_dir / "env"
    lines: list[str] = []
    if env_file.is_file():
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
    new_line = f"{key}={value}"
    replaced = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        body = stripped[len("export "):].lstrip() if stripped.startswith("export ") else stripped
        if body.startswith(f"{key}=") and not stripped.startswith("#"):
            if not replaced:
                out.append(new_line)
                replaced = True
            # drop any later duplicates of the same key
            continue
        out.append(line)
    if not replaced:
        out.append(new_line)
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    # The env file can later hold secrets (ANTHROPIC_API_KEY / GITHUB_TOKEN);
    # keep it owner-only, matching deploy.sh's `chmod 600` behavior.
    try:
        import os

        os.chmod(env_file, 0o600)
    except OSError:  # pragma: no cover - non-POSIX / permission edge case
        pass
    return env_file


def read_backend(settings: Any) -> str:
    """Return the currently configured backend label from live settings."""
    llm = str(getattr(settings, "llm", "off") or "off").strip().lower()
    if llm in _VALID_BACKENDS:
        return llm
    # "auto" (or anything else) maps onto its closest panel label.
    if llm == "auto":
        return "anthropic"
    return "off"


def panel_port(settings: Any) -> int:
    """Panel port from settings.panel_port, else the default."""
    try:
        return int(getattr(settings, "panel_port", DEFAULT_PANEL_PORT) or DEFAULT_PANEL_PORT)
    except (TypeError, ValueError):
        return DEFAULT_PANEL_PORT


# --------------------------------------------------------------------------- #
# Cheap availability + edition probes                                          #
# --------------------------------------------------------------------------- #
def ollama_available(settings: Any, timeout: float = 0.25) -> bool:
    """Fast, offline-safe TCP probe of the Ollama host:port. Never raises."""
    url = str(getattr(settings, "ollama_url", "http://localhost:11434"))
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 11434
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def latest_edition(settings: Any) -> Optional[str]:
    """Newest rendered edition directory name, or None. Never raises."""
    try:
        from .. import render  # local import keeps server import cheap

        editions = render.list_editions(Path(settings.site_dir))
        return editions[0] if editions else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Shared panel state                                                           #
# --------------------------------------------------------------------------- #
class PanelState:
    """Thread-safe holder for panel settings and background-job state."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self.busy = False
        self.error: Optional[str] = None
        self.last_refresh: Optional[str] = None
        self.agent_busy = False
        self.agent_error: Optional[str] = None
        self.agent_result: Optional[dict[str, Any]] = None
        self.agent_question: Optional[str] = None
        # "Ask about this week" grounded Q&A over the current edition. Kept on a
        # SEPARATE busy flag/result so it never collides with the GitHub agent.
        self.ask_edition_busy = False
        self.ask_edition_error: Optional[str] = None
        self.ask_edition_result: Optional[dict[str, Any]] = None
        self.ask_edition_question: Optional[str] = None
        # Actual bound TCP port; set by build_server once the socket is bound
        # (may differ from settings.panel_port, e.g. --port or an ephemeral 0).
        self.bound_port: Optional[int] = None

    # -- status ---------------------------------------------------------- #
    def status(self) -> dict[str, Any]:
        with self._lock:
            busy = self.busy
            error = self.error
            last_refresh = self.last_refresh
            agent_busy = self.agent_busy
        return {
            "backend": read_backend(self.settings),
            "ollama_available": ollama_available(self.settings),
            "latest_edition": latest_edition(self.settings),
            "busy": busy,
            "error": error,
            "last_refresh": last_refresh,
            "agent_busy": agent_busy,
            "panel_port": self.bound_port or panel_port(self.settings),
        }

    # -- config ---------------------------------------------------------- #
    def set_backend(self, backend: str) -> dict[str, Any]:
        backend = str(backend or "").strip().lower()
        if backend not in _VALID_BACKENDS:
            raise ValueError(f"invalid backend {backend!r}; expected one of {_VALID_BACKENDS}")
        llm_value = _BACKEND_TO_LLM[backend]
        # Serialize the read-modify-write of the env file: ThreadingHTTPServer
        # can dispatch concurrent POST /api/config requests, and overlapping
        # writes could otherwise drop an update or corrupt the file.
        with self._lock:
            write_env_value(Path(self.settings.config_dir), "GHPULSE_LLM", llm_value)
            # Reflect immediately in the live settings so status() is consistent
            # even before the next process reload.
            try:
                self.settings.llm = llm_value
            except Exception:  # pragma: no cover - frozen settings edge case
                pass
        return self.status()

    # -- refresh --------------------------------------------------------- #
    def start_refresh(self) -> bool:
        with self._lock:
            if self.busy:
                return False
            self.busy = True
            self.error = None
        thread = threading.Thread(target=self._run_refresh, name="ghpulse-refresh", daemon=True)
        thread.start()
        return True

    def _run_refresh(self) -> None:
        try:
            self._refresh_pipeline()
            with self._lock:
                self.last_refresh = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:  # pragma: no cover - resilience path
            with self._lock:
                self.error = f"refresh failed: {exc}"
        finally:
            with self._lock:
                self.busy = False

    def _refresh_pipeline(self) -> None:
        """Best-effort weekly refresh + render; re-render latest without a token."""
        from datetime import datetime, timedelta, timezone

        from .. import db, render, score

        settings = self.settings
        conn = db.connect(settings.db_path)
        try:
            db.init_db(conn)
            token = getattr(settings, "token", None)
            if token:
                # Full pipeline mirrors `ghpulse weekly` at a high level; every
                # sub-step is best-effort so a partial failure still renders.
                from .. import collect, discover, hype, ratelimit
                from .. import http as gh_http
                from .. import social

                today = datetime.now(timezone.utc).date()
                monday = (today - timedelta(days=today.weekday())).isoformat()
                edition = today.isoformat()
                limiter = ratelimit.RateLimiter()
                client = gh_http.GitHubClient(token, limiter=limiter, conn=conn)
                try:
                    discover.discover(client, conn, monday, settings)
                except Exception:
                    pass
                try:
                    collect.snapshot_all(client, conn, db.now_iso())
                except Exception:
                    pass
                sections = score.compute_metrics(conn, edition)
                db.commit(conn)
                try:
                    social.fetch_social(conn, None, settings)
                    hype.compute_hype(conn, edition)
                    sections = hype.merge_hype_sections(conn, edition, sections)
                except Exception:
                    pass
                render.render_edition(conn, edition, settings, sections=sections)
            else:
                # No token: re-render the newest edition already in the DB so the
                # news page reflects any layout/template changes.
                edition = score.latest_edition(conn)
                if edition:
                    render.render_edition(conn, edition, settings)
                else:
                    raise RuntimeError(
                        "no GitHub token and no existing edition — run `ghpulse demo` first"
                    )
        finally:
            conn.close()

    # -- agent ----------------------------------------------------------- #
    def start_ask(self, question: str) -> bool:
        question = str(question or "").strip()
        if not question:
            return False
        with self._lock:
            if self.agent_busy:
                return False
            self.agent_busy = True
            self.agent_error = None
            self.agent_result = None
            self.agent_question = question
        thread = threading.Thread(
            target=self._run_ask, args=(question,), name="ghpulse-agent", daemon=True
        )
        thread.start()
        return True

    def _run_ask(self, question: str) -> None:
        try:
            from .. import agent as agent_mod
            from .. import db

            settings = self.settings
            conn = db.connect(settings.db_path)
            try:
                db.init_db(conn)
                result = agent_mod.run_agent(question, settings, conn=conn)
            finally:
                conn.close()
            with self._lock:
                self.agent_result = _serialize_agent_result(result, question)
        except Exception as exc:  # pragma: no cover - resilience path
            with self._lock:
                self.agent_error = f"agent failed: {exc}"
                self.agent_result = {
                    "question": question,
                    "answer": f"The agent could not run: {exc}",
                    "steps": [],
                    "error": str(exc),
                }
        finally:
            with self._lock:
                self.agent_busy = False

    def ask_result(self) -> dict[str, Any]:
        with self._lock:
            if self.agent_busy:
                return {"ready": False, "busy": True, "question": self.agent_question}
            if self.agent_result is None and self.agent_error is None:
                return {"ready": False, "busy": False}
            payload: dict[str, Any] = {"ready": True, "busy": False}
            if self.agent_result is not None:
                payload["result"] = self.agent_result
            if self.agent_error is not None:
                payload["error"] = self.agent_error
            return payload

    # -- ask about this week (grounded over the current edition) ---------- #
    def start_ask_edition(self, question: str) -> bool:
        question = str(question or "").strip()
        if not question:
            return False
        with self._lock:
            if self.ask_edition_busy:
                return False
            self.ask_edition_busy = True
            self.ask_edition_error = None
            self.ask_edition_result = None
            self.ask_edition_question = question
        thread = threading.Thread(
            target=self._run_ask_edition,
            args=(question,),
            name="ghpulse-ask-edition",
            daemon=True,
        )
        thread.start()
        return True

    def _run_ask_edition(self, question: str) -> None:
        try:
            from .. import db, llm, score

            settings = self.settings
            conn = db.connect(settings.db_path)
            try:
                db.init_db(conn)
                edition = score.latest_edition(conn)
                if not edition:
                    result = {
                        "answer": "No edition yet — run `ghpulse demo` or click "
                        "Refresh data first, then ask again.",
                        "cited": [],
                        "model": "",
                        "grounded": False,
                    }
                else:
                    result = llm.answer_over_edition(
                        conn, edition, question, settings
                    )
            finally:
                conn.close()
            with self._lock:
                self.ask_edition_result = {
                    "question": question,
                    "answer": result.get("answer", "(no answer)"),
                    "cited": result.get("cited") or [],
                    "grounded": bool(result.get("grounded")),
                    "model": result.get("model", ""),
                }
        except Exception as exc:  # pragma: no cover - resilience path
            with self._lock:
                self.ask_edition_error = f"ask failed: {exc}"
                self.ask_edition_result = {
                    "question": question,
                    "answer": f"Could not answer: {exc}",
                    "cited": [],
                    "grounded": False,
                    "error": str(exc),
                }
        finally:
            with self._lock:
                self.ask_edition_busy = False

    def ask_edition_result_payload(self) -> dict[str, Any]:
        with self._lock:
            busy = self.ask_edition_busy
            result = self.ask_edition_result
            error = self.ask_edition_error
            question = self.ask_edition_question
        if busy:
            return {"ready": False, "busy": True, "question": question}
        if result is None and error is None:
            return {"ready": False, "busy": False}
        payload: dict[str, Any] = {
            "ready": True,
            "busy": False,
            "question": question,
            "answer": (result or {}).get("answer") if result else None,
            "cited": (result or {}).get("cited") or [],
        }
        if error is not None:
            payload["error"] = error
        return payload


_INTERPRET_SYSTEM = (
    "Rewrite this developer question as a GitHub repository search query of 2-5 "
    "keywords (optionally language:/topic: qualifiers). Reply with the query only."
)


def _interpret_query(settings: Any, raw: str, timeout: float = 2.0) -> str:
    """Best-effort LLM rewrite of a natural question into GitHub search keywords.

    Returns the rewritten query, or the raw query on any failure / >2s / no
    backend. Never raises. Keyword search must fully work with interpret=0.
    """
    raw = str(raw or "").strip()
    if not raw:
        return raw
    try:
        from .. import llm

        backend = llm.select_backend(settings)
        if backend is None or not backend.available():
            return raw

        holder: dict[str, str] = {}

        def _run() -> None:
            try:
                holder["out"] = backend.summarize(_INTERPRET_SYSTEM, raw) or ""
            except Exception:  # noqa: BLE001 - degrade to raw
                holder["out"] = ""

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout)
        rewritten = (holder.get("out") or "").strip().splitlines()[0].strip() if holder.get("out") else ""
        # Guard against a chatty model: keep it short, else fall back to raw.
        if rewritten and len(rewritten) <= 200:
            return rewritten
        return raw
    except Exception:  # noqa: BLE001 - never break search over interpretation
        return raw


def _serialize_agent_result(result: Any, question: str) -> dict[str, Any]:
    """Turn an AgentResult (or anything close) into a JSON-safe dict."""
    answer = getattr(result, "answer", None)
    if answer is None and isinstance(result, dict):
        answer = result.get("answer")
    raw_steps = getattr(result, "steps", None)
    if raw_steps is None and isinstance(result, dict):
        raw_steps = result.get("steps")
    steps: list[dict[str, Any]] = []
    for step in raw_steps or []:
        if isinstance(step, dict):
            steps.append(
                {
                    "tool": step.get("tool"),
                    "args": step.get("args"),
                    "result_summary": step.get("result_summary"),
                }
            )
        else:
            steps.append(
                {
                    "tool": getattr(step, "tool", None),
                    "args": getattr(step, "args", None),
                    "result_summary": getattr(step, "result_summary", None),
                }
            )
    return {
        "question": question,
        "answer": answer if answer is not None else "(no answer)",
        "steps": steps,
    }


# --------------------------------------------------------------------------- #
# HTTP handler                                                                 #
# --------------------------------------------------------------------------- #
def _load_template() -> str:
    try:
        return _TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>ghpulse panel</title><p>panel.html missing</p>"


def make_handler(state: PanelState) -> type[BaseHTTPRequestHandler]:
    """Build a BaseHTTPRequestHandler subclass bound to ``state``."""

    class PanelHandler(BaseHTTPRequestHandler):
        server_version = "ghpulse-panel/1.0"

        # Quiet by default; the panel is a background convenience server.
        def log_message(self, *args: Any) -> None:  # noqa: D401
            return

        # -- helpers ----------------------------------------------------- #
        def _send_cors(self) -> None:
            """Permissive CORS for the static news page (localhost, single-user).

            The rendered news page is served from ``file://`` or a different
            localhost port than the panel, so the browser makes cross-origin
            calls to ``/api/*``. ``*`` is safe here: the panel binds 127.0.0.1
            and is single-user.
            """
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send_json(self, payload: Any, code: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._send_cors()
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, code: int = HTTPStatus.OK) -> None:
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json_body(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}

        # -- routing ----------------------------------------------------- #
        def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
            """CORS preflight: 204 No Content with permissive CORS headers."""
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    self._send_html(_load_template())
                elif path == "/api/status":
                    self._send_json(state.status())
                elif path == "/api/ask/result":
                    self._send_json(state.ask_result())
                elif path == "/api/ask_edition/result":
                    self._send_json(state.ask_edition_result_payload())
                elif path == "/api/search":
                    self._handle_search(urlparse(self.path).query)
                elif path == "/api/search_github":
                    self._handle_search_github(urlparse(self.path).query)
                elif path == "/news":
                    self._serve_news()
                else:
                    self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:  # pragma: no cover - never kill the server
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            path = urlparse(self.path).path
            try:
                if path == "/api/config":
                    body = self._read_json_body()
                    try:
                        status = state.set_backend(body.get("backend", ""))
                    except ValueError as exc:
                        self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                        return
                    self._send_json(status)
                elif path == "/api/refresh":
                    started = state.start_refresh()
                    self._send_json({"started": started, "busy": state.status()["busy"]})
                elif path == "/api/ask":
                    body = self._read_json_body()
                    started = state.start_ask(body.get("question", ""))
                    if not started and not str(body.get("question") or "").strip():
                        self._send_json(
                            {"started": False, "error": "empty question"},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                    self._send_json({"started": started})
                elif path == "/api/ask_edition":
                    body = self._read_json_body()
                    started = state.start_ask_edition(body.get("question", ""))
                    if not started and not str(body.get("question") or "").strip():
                        self._send_json(
                            {"started": False, "error": "empty question"},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                    self._send_json({"started": started})
                else:
                    self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:  # pragma: no cover - never kill the server
                self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

        # -- /api/search (tracked cohort, offline, synchronous) ---------- #
        def _handle_search(self, query: str) -> None:
            """Sub-second, synchronous search over the tracked cohort. No LLM/network."""
            params = parse_qs(query or "")
            q = (params.get("q") or [""])[0].strip()
            try:
                limit = int((params.get("limit") or ["20"])[0])
            except (TypeError, ValueError):
                limit = 20
            try:
                offset = int((params.get("offset") or ["0"])[0])
            except (TypeError, ValueError):
                offset = 0
            limit = max(1, min(limit, 50))
            offset = max(0, offset)

            if not q:
                self._send_json(
                    {"query": q, "source": "tracked", "total": 0, "offset": offset, "repos": []}
                )
                return
            try:
                from .. import db

                conn = db.connect(state.settings.db_path)
                try:
                    db.init_db(conn)
                    ranked = db.search_tracked(conn, q, limit=1_000_000, offset=0)
                finally:
                    conn.close()
                total = len(ranked)
                repos = ranked[offset : offset + limit]
            except Exception as exc:  # never crash the panel over a query
                self._send_json(
                    {
                        "query": q,
                        "source": "tracked",
                        "total": 0,
                        "offset": offset,
                        "repos": [],
                        "error": str(exc),
                    }
                )
                return
            self._send_json(
                {
                    "query": q,
                    "source": "tracked",
                    "total": total,
                    "offset": offset,
                    "repos": repos,
                }
            )

        # -- /api/search_github (one live page, optional LLM rewrite) ---- #
        def _handle_search_github(self, query: str) -> None:
            """One live GitHub search page, mapped to compact repo dicts. Guarded.

            With no token it returns a friendly ``{error}`` rather than crashing.
            Page 5 is a hard cap (``has_more`` False + a refine note). ``interpret=1``
            asks the LLM backend (if any) to rewrite the query into keywords, with a
            fast fallback to the raw query.
            """
            params = parse_qs(query or "")
            q = (params.get("q") or [""])[0].strip()
            try:
                page = int((params.get("page") or ["1"])[0])
            except (TypeError, ValueError):
                page = 1
            page = max(1, page)
            interpret = (params.get("interpret") or ["0"])[0] in ("1", "true", "yes")

            if not q:
                self._send_json(
                    {
                        "query": q,
                        "interpreted_query": q,
                        "source": "github",
                        "page": page,
                        "has_more": False,
                        "repos": [],
                    }
                )
                return

            token = getattr(state.settings, "token", None)
            if not token:
                self._send_json(
                    {
                        "query": q,
                        "interpreted_query": q,
                        "source": "github",
                        "page": page,
                        "has_more": False,
                        "repos": [],
                        "error": "add a GitHub token (GITHUB_TOKEN) to search GitHub live.",
                    }
                )
                return

            PER_PAGE = 20
            PAGE_CAP = 5
            try:
                interpreted = _interpret_query(state.settings, q) if interpret else q
                interpreted = interpreted or q

                # Page 6+ is past the hard cap: return nothing more with a note.
                if page > PAGE_CAP:
                    self._send_json(
                        {
                            "query": q,
                            "interpreted_query": interpreted,
                            "source": "github",
                            "page": page,
                            "has_more": False,
                            "repos": [],
                            "note": "Showing the first 5 pages — refine your query for more.",
                        }
                    )
                    return

                from .. import db
                from .. import http as gh_http
                from ..agent import tools as agent_tools

                conn = db.connect(state.settings.db_path)
                try:
                    db.init_db(conn)
                    tracked_names = {
                        r["full_name"]
                        for r in conn.execute("SELECT full_name FROM repo").fetchall()
                    }
                    client = gh_http.GitHubClient(token, conn=conn)
                    try:
                        items = client.search_page(interpreted, page=page, per_page=PER_PAGE)
                    finally:
                        client.close()
                finally:
                    conn.close()

                repos = []
                for it in items:
                    compact = agent_tools._compact_repo(it)
                    compact["commits_7d"] = None
                    compact["tracked"] = compact.get("full_name") in tracked_names
                    repos.append(compact)

                has_more = page < PAGE_CAP and len(items) >= PER_PAGE
                payload = {
                    "query": q,
                    "interpreted_query": interpreted,
                    "source": "github",
                    "page": page,
                    "has_more": has_more,
                    "repos": repos,
                }
                if page >= PAGE_CAP:
                    payload["note"] = "Showing the first 5 pages — refine your query for more."
                self._send_json(payload)
            except Exception as exc:  # never crash the panel over a live search
                self._send_json(
                    {
                        "query": q,
                        "interpreted_query": q,
                        "source": "github",
                        "page": page,
                        "has_more": False,
                        "repos": [],
                        "error": str(exc),
                    }
                )

        # -- /news ------------------------------------------------------- #
        def _serve_news(self) -> None:
            edition = latest_edition(state.settings)
            if not edition:
                self._send_html(
                    "<!doctype html><meta charset='utf-8'><title>ghpulse</title>"
                    "<p>No rendered edition yet. Run <code>ghpulse demo</code> or "
                    "click <b>Refresh data</b> in the control panel.</p>",
                    HTTPStatus.NOT_FOUND,
                )
                return
            page = Path(state.settings.site_dir) / edition / "index.html"
            try:
                html = page.read_text(encoding="utf-8")
            except OSError:
                self._send_html("<p>edition page unreadable</p>", HTTPStatus.NOT_FOUND)
                return
            self._send_html(html)

    return PanelHandler


# --------------------------------------------------------------------------- #
# Server entry points                                                          #
# --------------------------------------------------------------------------- #
def build_server(settings: Any, port: Optional[int] = None) -> tuple[ThreadingHTTPServer, PanelState]:
    """Construct (but do not start) the panel server. Returns (server, state).

    ``port=0`` binds an ephemeral port (useful for tests). The bound port is
    available as ``server.server_address[1]``.
    """
    state = PanelState(settings)
    if port is None:
        port = panel_port(settings)
    handler_cls = make_handler(state)
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), handler_cls)
    state.bound_port = server.server_address[1]
    return server, state


def serve(settings: Any, port: Optional[int] = None, open_browser: bool = False) -> None:
    """Run the control panel on 127.0.0.1 until interrupted."""
    server, _state = build_server(settings, port)
    bound_port = server.server_address[1]
    url = f"http://127.0.0.1:{bound_port}/"
    print(f"ghpulse control panel: {url} (Ctrl-C to stop)")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # pragma: no cover - headless / no browser
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()

#!/usr/bin/env bash
#
# deploy.sh — one command to set up and run ghpulse locally.
#
#   ./deploy.sh              full setup: venv -> install -> token -> first scan -> serve
#   ./deploy.sh --demo       offline demo (no GitHub token needed)
#   ./deploy.sh --schedule   also install the daily/weekly launchd schedulers (macOS)
#   ./deploy.sh --no-serve   set up + scan but don't start the web server
#   ./deploy.sh token-help   just print the "how to get a GitHub token" manual and exit
#
# Everything runs on your machine. Your token is stored only in
# ~/.config/ghpulse/env (chmod 600, never committed) and is sent only to
# api.github.com. Nothing is uploaded anywhere else.

set -euo pipefail

# ---- paths ------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
ENV_DIR="$HOME/.config/ghpulse"
ENV_FILE="$ENV_DIR/env"
export GHPULSE_HOME="${GHPULSE_HOME:-$HOME/.ghpulse}"
PORT="${GHPULSE_PORT:-8414}"

# ---- pretty printing --------------------------------------------------------
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }

# Run a long command with a live braille spinner, elapsed clock, and a peek at
# its latest output line — so long silent steps (the first scan) never look
# frozen. Non-TTY (piped/CI) falls back to plain streaming. Returns the cmd's rc.
SPIN_LOG=""
run_with_spinner() {
  local label="$1"; shift
  mkdir -p "$GHPULSE_HOME"
  SPIN_LOG="$GHPULSE_HOME/deploy-step.log"
  if [ ! -t 1 ]; then                       # not a terminal: just stream it
    "$@" 2>&1 | sed -E 's/ghp_[A-Za-z0-9]+/***/g'; return "${PIPESTATUS[0]}"
  fi
  "$@" >"$SPIN_LOG" 2>&1 &
  local pid=$! start=$SECONDS i=0
  local frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
  printf '\033[?25l'                          # hide cursor
  while kill -0 "$pid" 2>/dev/null; do
    local el=$((SECONDS - start))
    local last; last=$(tail -n1 "$SPIN_LOG" 2>/dev/null | sed -E 's/ghp_[A-Za-z0-9]+/***/g' | cut -c1-56)
    printf '\r  \033[36m%s\033[0m %s  \033[2m%dm%02ds\033[0m  \033[2m%s\033[0m\033[K' \
      "${frames[i++ % 10]}" "$label" $((el / 60)) $((el % 60)) "$last"
    sleep 0.1
  done
  wait "$pid"; local rc=$?
  printf '\r\033[K\033[?25h'                   # clear line, restore cursor
  return "$rc"
}

# ---- the token manual (printed on demand + when no token is set) ------------
print_token_help() {
  cat <<'EOF'

  ┌─────────────────────────────────────────────────────────────────────────┐
  │  HOW TO GET A GITHUB TOKEN (2 minutes, free)                             │
  └─────────────────────────────────────────────────────────────────────────┘

  ghpulse reads PUBLIC GitHub data. A token is needed only to raise your rate
  limit (60 → 5,000 requests/hour) and to use the GraphQL API that powers the
  star/fork snapshots. It needs NO write access to anything.

  1. Sign in to GitHub, then open:
         https://github.com/settings/tokens

  2. Click  "Generate new token"  →  "Generate new token (classic)".

  3. Fill it in:
         • Note:        ghpulse (local)
         • Expiration:  90 days  (or whatever you like)
         • Scopes:      LEAVE ALL UNCHECKED.
                        Public data needs no scope. (If your org requires one,
                        "public_repo" is the most you'd ever tick — never a
                        write scope, never "repo".)

  4. Click  "Generate token"  at the bottom and COPY the value.
     It looks like:  ghp_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
     GitHub shows it only once — copy it now.

  5. Paste it when this script asks. It gets saved to:
         ~/.config/ghpulse/env      (chmod 600, on your machine only)

  Prefer a fine-grained token? Use "Fine-grained tokens", set Repository
  access = "Public Repositories (read-only)", and add no extra permissions.

  Safety: the token never leaves your machine except in requests to
  api.github.com. You can revoke it any time at the URL in step 1 — ghpulse
  keeps working the moment you drop a new one into ~/.config/ghpulse/env.

  ─────────────────────────────────────────────────────────────────────────
  OPTIONAL — Claude Sonnet 5 for the "what's happening" explainer + blurbs
  ─────────────────────────────────────────────────────────────────────────
  By default the LLM features use the FREE local Ollama model. To use Claude
  Sonnet 5 instead (sharper summaries), add an Anthropic API key from
  https://console.anthropic.com/settings/keys to the same env file:

      ANTHROPIC_API_KEY=sk-ant-...
      GHPULSE_LLM=anthropic
      # optional: GHPULSE_CLAUDE_MODEL=claude-opus-4-8   (defaults to claude-sonnet-5)

EOF
}

# ---- subcommand: token-help -------------------------------------------------
if [ "${1:-}" = "token-help" ]; then
  print_token_help
  exit 0
fi

# ---- parse flags ------------------------------------------------------------
DEMO=0; SERVE=1; SCHEDULE=0; SKIP_OLLAMA=0
for arg in "$@"; do
  case "$arg" in
    --demo)         DEMO=1 ;;
    --no-serve)     SERVE=0 ;;
    --schedule)     SCHEDULE=1 ;;
    --skip-ollama)  SKIP_OLLAMA=1 ;;
    -h|--help)
      sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) warn "ignoring unknown flag: $arg" ;;
  esac
done

bold "ghpulse — local GitHub tech-news deploy"
echo "  repo:  $REPO_DIR"
echo "  home:  $GHPULSE_HOME"

# ---- 1. find a suitable Python (3.12+) --------------------------------------
step "Step 1/6 · Locating Python 3.12+"
PYBIN=""
for cand in python3.14 python3.13 python3.12 /opt/homebrew/bin/python3.14 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0.0)"
    major="${ver%%.*}"; minor="${ver#*.}"
    if [ "$major" = "3" ] && [ "$minor" -ge 12 ] 2>/dev/null; then
      PYBIN="$(command -v "$cand")"; ok "using $PYBIN (Python $ver)"; break
    fi
  fi
done
if [ -z "$PYBIN" ]; then
  warn "No Python 3.12+ found. Install one, e.g.:  brew install python@3.14"
  exit 1
fi

# ---- 2. venv + install ------------------------------------------------------
step "Step 2/6 · Installing dependencies (virtualenv + ghpulse)"
if [ ! -x "$VENV_DIR/bin/python" ]; then
  "$PYBIN" -m venv "$VENV_DIR"; ok "created $VENV_DIR"
else
  ok "reusing existing venv"
fi
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
# Regular install (NOT editable): on macOS + Python 3.14 the editable .pth file
# is hidden and `import ghpulse` fails. Re-run deploy.sh after code changes.
"$VENV_DIR/bin/pip" install --quiet "$REPO_DIR"
ok "ghpulse installed"
GHP="$VENV_DIR/bin/ghpulse"

# ---- LLM readiness: the Ask bar + search need a backend --------------------
# Sets GHPULSE_LLM (+ a real Ollama model) so the interactive features actually
# work after install, or tells the user exactly how to enable one.
detect_llm() {
  step "Confirming LLM backend for the Ask bar + search"
  if [ -n "${ANTHROPIC_API_KEY:-}" ] && [ "${GHPULSE_LLM:-}" = "anthropic" ]; then
    export GHPULSE_LLM=anthropic
    ok "using Claude (${GHPULSE_CLAUDE_MODEL:-claude-sonnet-5})"
    return
  fi
  if command -v ollama >/dev/null 2>&1 && curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    export GHPULSE_LLM="${GHPULSE_LLM:-ollama}"
    # Default model (llama3.1:8b) is usually NOT pulled; prefer qwen2.5 if present.
    if [ -z "${GHPULSE_OLLAMA_MODEL:-}" ] && ollama list 2>/dev/null | grep -qi 'qwen2.5'; then
      export GHPULSE_OLLAMA_MODEL=qwen2.5
    fi
    ok "using local Ollama (model: ${GHPULSE_OLLAMA_MODEL:-qwen2.5})"
    return
  fi
  if command -v ollama >/dev/null 2>&1; then
    warn "Ollama is installed but not running — start it so the Ask bar works:  open -a Ollama"
  else
    warn "No LLM backend yet — the Ask bar + search need one:"
    echo "     • FREE local (recommended):  bash \"$REPO_DIR/scripts/setup-ollama.sh\""
    echo "     • Or Claude Sonnet 5:  add ANTHROPIC_API_KEY + GHPULSE_LLM=anthropic to $ENV_FILE"
  fi
}

# ---- port helpers: never crash on "Address already in use" ----------------
port_busy()  { (: < "/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1; }   # 0 = something listening
panel_alive(){ curl -sf "http://127.0.0.1:8765/api/status" >/dev/null 2>&1; }
free_port()  { local p="$1"; while port_busy "$p"; do p=$((p + 1)); done; printf '%s' "$p"; }

# ---- serve the site AND the control panel together ------------------------
# The Ask bar/search call the panel (127.0.0.1:8765); starting only the static
# server leaves them dead. Run the panel in the background, serve in the
# foreground, and stop both on Ctrl-C. Resilient to ports already in use.
PANEL_PID=""
serve_all() {
  mkdir -p "$GHPULSE_HOME"
  # Control panel — the page's Ask bar is hard-wired to port 8765, so we don't
  # relocate it: reuse a running one, or warn if something else holds the port.
  if panel_alive; then
    ok "control panel already running on 8765 — reusing it."
  elif port_busy 8765; then
    warn "port 8765 is held by another process — the Ask bar may be inactive."
    warn "free it with:  lsof -nP -iTCP:8765 -sTCP:LISTEN   then  kill <PID>"
  else
    step "Starting control panel (Ask bar + search) → http://127.0.0.1:8765"
    "$GHP" panel >"$GHPULSE_HOME/panel.log" 2>&1 &
    PANEL_PID=$!
    sleep 1
    if kill -0 "$PANEL_PID" 2>/dev/null; then
      ok "control panel running (pid $PANEL_PID, logs: $GHPULSE_HOME/panel.log)"
    else
      PANEL_PID=""
      warn "control panel didn't start — the Ask bar will be inactive (see $GHPULSE_HOME/panel.log)"
    fi
  fi
  trap '[ -n "$PANEL_PID" ] && kill "$PANEL_PID" 2>/dev/null; true' EXIT INT TERM
  # Static site — auto-pick the next free port so a clash never crashes us.
  local serve_port; serve_port="$(free_port "$PORT")"
  [ "$serve_port" != "$PORT" ] && warn "port $PORT was busy — serving on $serve_port instead."
  step "Serving the page → http://localhost:$serve_port   (Ctrl-C stops both)"
  "$GHP" serve --port "$serve_port"
}

# ---- Ollama: the free local AI that powers the Ask bar + search -----------
# Runs the interactive, non-fatal setup-ollama.sh (installs Ollama, starts it,
# pulls qwen2.5) unless it's already good, --skip-ollama is set, or Claude is on.
run_ollama_setup() {
  step "Step 3/6 · Ollama — free local AI (powers the Ask bar + search)"
  if [ "$SKIP_OLLAMA" = "1" ]; then warn "skipped (--skip-ollama)"; return; fi
  if command -v ollama >/dev/null 2>&1 \
     && curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 \
     && ollama list 2>/dev/null | grep -qi 'qwen2.5'; then
    ok "Ollama installed, running, qwen2.5 present — nothing to do."
    return
  fi
  if [ -n "${ANTHROPIC_API_KEY:-}" ] && [ "${GHPULSE_LLM:-}" = "anthropic" ]; then
    ok "Claude backend configured — Ollama not required (skipping)."
    return
  fi
  if [ ! -f "$REPO_DIR/scripts/setup-ollama.sh" ]; then
    warn "setup-ollama.sh missing; install Ollama later from https://ollama.com"; return
  fi
  if [ -t 0 ]; then
    GHPULSE_AGENT_MODEL="${GHPULSE_OLLAMA_MODEL:-qwen2.5}" bash "$REPO_DIR/scripts/setup-ollama.sh" || true
  else
    warn "non-interactive shell — skipping Ollama setup."
    echo "     Run it yourself:  bash \"$REPO_DIR/scripts/setup-ollama.sh\""
  fi
}

# ---- demo path: no token, offline -------------------------------------------
if [ "$DEMO" = "1" ]; then
  run_ollama_setup
  step "Seeding offline demo (no token, no network)"
  GHPULSE_HOME="$GHPULSE_HOME" "$GHP" demo
  ok "demo editions rendered"
  if [ "$SERVE" = "1" ]; then
    detect_llm
    serve_all
  fi
  exit 0
fi

# ---- Step 3/6 · Ollama ------------------------------------------------------
run_ollama_setup

# ---- Step 4/6 · GitHub token ------------------------------------------------
step "Step 4/6 · GitHub token"
mkdir -p "$ENV_DIR"; chmod 700 "$ENV_DIR" 2>/dev/null || true
have_token=0
if [ -f "$ENV_FILE" ] && grep -q '^GITHUB_TOKEN=ghp_' "$ENV_FILE" 2>/dev/null; then
  have_token=1
  ok "found an existing token in $ENV_FILE (leaving it untouched)"
fi
if [ "$have_token" = "0" ]; then
  print_token_help
  if [ -t 0 ]; then
    printf '  Paste your GitHub token (input hidden), or press Enter to run WITHOUT one: '
    read -r -s TOKEN; echo
    if [ -n "$TOKEN" ]; then
      case "$TOKEN" in
        ghp_*|github_pat_*)
          umask 177
          printf 'GITHUB_TOKEN=%s\n' "$TOKEN" > "$ENV_FILE"
          chmod 600 "$ENV_FILE"
          have_token=1
          ok "saved to $ENV_FILE (chmod 600, local only)"
          ;;
        *) warn "that doesn't look like a GitHub token (expected ghp_… or github_pat_…); continuing without one" ;;
      esac
    else
      warn "no token — GitHub allows only 60 req/hr and no GraphQL snapshots; results will be thin"
    fi
  else
    warn "non-interactive shell; skipping token prompt. Add one later to $ENV_FILE"
  fi
fi

# Load the token for this run (only if present).
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi

# ---- Step 5/6 · first scan --------------------------------------------------
step "Step 5/6 · First scan (discover + snapshot + metrics + render)"
echo "  Walks sharded GitHub searches — a few minutes on the first run."
if run_with_spinner "scanning GitHub…" "$GHP" weekly --render; then
  ok "edition rendered into $GHPULSE_HOME/site"
else
  warn "scan reported errors — see ${SPIN_LOG:-$GHPULSE_HOME/deploy-step.log}"
fi

# ---- 5. optional schedulers -------------------------------------------------
if [ "$SCHEDULE" = "1" ]; then
  step "Installing launchd schedulers (daily 07:00 + weekly Mon 08:00)"
  if [ -f "$REPO_DIR/scripts/install-launchd.sh" ]; then
    GHPULSE_HOME="$GHPULSE_HOME" bash "$REPO_DIR/scripts/install-launchd.sh" || warn "scheduler install skipped"
  else
    warn "scripts/install-launchd.sh not found"
  fi
fi

# ---- Step 6/6 · serve (page + control panel) --------------------------------
if [ "$SERVE" = "1" ]; then
  detect_llm
  step "Step 6/6 · Serving"
  serve_all
else
  detect_llm
  bold "Done."
  echo "  Open the page any time with the panel:"
  echo "     GHPULSE_HOME=$GHPULSE_HOME $GHP panel &      # Ask bar + search"
  echo "     GHPULSE_HOME=$GHPULSE_HOME $GHP serve --port $PORT   # the page"
  [ "$SCHEDULE" = "1" ] || echo "  Tip: re-run with --schedule to snapshot daily and build % trends automatically."
fi

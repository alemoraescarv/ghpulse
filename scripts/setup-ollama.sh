#!/usr/bin/env bash
#
# setup-ollama.sh — get the FREE local AI engine ready for ghpulse.
# ================================================================
# This installs Ollama (if you don't have it), starts it, and downloads the
# small default model (qwen2.5) that ghpulse's local agent uses. It asks
# before doing anything that changes your system, and it is safe to re-run.
#
# It NEVER fails the whole setup: if you decline a step, or the network is
# down, it just prints how to finish later and exits cleanly.
#
# You can run it directly:
#     bash scripts/setup-ollama.sh
#
set -u

MODEL="${GHPULSE_AGENT_MODEL:-qwen2.5}"

if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    BOLD=""; DIM=""; GREEN=""; YELLOW=""; RESET=""
fi
ok()    { printf "  %s✓%s %s\n" "$GREEN" "$RESET" "$*"; }
warn()  { printf "  %s!%s %s\n" "$YELLOW" "$RESET" "$*"; }
info()  { printf "  %s%s%s\n" "$DIM" "$*" "$RESET"; }
later() { printf "  %sTo do this later:%s %s\n" "$YELLOW" "$RESET" "$*"; }

ask() {
    local prompt="$1" def="${2:-y}" reply hint
    if [ "$def" = "y" ]; then hint="[Y/n]"; else hint="[y/N]"; fi
    printf "  %s%s%s %s " "$BOLD" "$prompt" "$RESET" "$hint"
    if ! IFS= read -r reply; then printf "\n"; [ "$def" = "y" ] && return 0 || return 1; fi
    reply="$(printf "%s" "$reply" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    case "$reply" in
        "" ) [ "$def" = "y" ] && return 0 || return 1 ;;
        y|yes ) return 0 ;;
        * ) return 1 ;;
    esac
}

printf "\n%sOllama setup for ghpulse (free local AI)%s\n" "$BOLD" "$RESET"

# --------------------------------------------------------------------------
# 1) Is Ollama installed?
# --------------------------------------------------------------------------
if command -v ollama >/dev/null 2>&1; then
    ok "Ollama is already installed ($(command -v ollama))."
else
    warn "Ollama is not installed yet."
    info "Ollama is the free tool that runs AI models locally on your machine."
    info "Official installer (macOS/Linux):"
    info "    curl -fsSL https://ollama.com/install.sh | sh"
    info "(On a Mac you can also download the app from https://ollama.com/download)"
    if ask "Run the official Ollama installer now?" y; then
        if command -v curl >/dev/null 2>&1; then
            if curl -fsSL https://ollama.com/install.sh | sh; then
                ok "Ollama installed."
            else
                warn "The installer did not finish."
                later "run:  curl -fsSL https://ollama.com/install.sh | sh"
            fi
        else
            warn "'curl' is not available on this system."
            later "install Ollama from https://ollama.com/download"
        fi
    else
        info "Skipped installing Ollama."
        later "run:  curl -fsSL https://ollama.com/install.sh | sh   (or download from https://ollama.com/download)"
        # Nothing else to do without Ollama — exit cleanly, non-fatal.
        exit 0
    fi
fi

# Re-check after a possible install.
if ! command -v ollama >/dev/null 2>&1; then
    warn "Ollama still isn't on your PATH. You may need to open a new terminal."
    later "open a new terminal, then run:  ollama pull $MODEL"
    exit 0
fi

# --------------------------------------------------------------------------
# 2) Make sure the Ollama service is reachable.
# --------------------------------------------------------------------------
ollama_up() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1
    else
        ollama list >/dev/null 2>&1
    fi
}

if ollama_up; then
    ok "Ollama service is running."
else
    info "Starting the Ollama service in the background..."
    # `ollama serve` blocks, so detach it. On macOS the app usually auto-starts,
    # but this covers the CLI-only case.
    (ollama serve >/dev/null 2>&1 &) || true
    # Give it a moment to come up.
    tries=0
    while [ "$tries" -lt 10 ]; do
        if ollama_up; then break; fi
        tries=$((tries + 1))
        sleep 1
    done
    if ollama_up; then
        ok "Ollama service is running."
    else
        warn "Could not confirm the Ollama service is up."
        later "run 'ollama serve' in a separate terminal, then re-run this script"
    fi
fi

# --------------------------------------------------------------------------
# 3) Download the model.
# --------------------------------------------------------------------------
have_model() {
    ollama list 2>/dev/null | awk '{print $1}' | grep -q "^${MODEL}\(:latest\)\?$"
}

if have_model; then
    ok "Model '$MODEL' is already downloaded."
else
    info "The model '$MODEL' is a few gigabytes; this can take several minutes."
    if ask "Download the model '$MODEL' now?" y; then
        if ollama pull "$MODEL"; then
            ok "Downloaded '$MODEL'."
        else
            warn "Download did not complete."
            later "run:  ollama pull $MODEL"
        fi
    else
        info "Skipped the model download."
        later "run:  ollama pull $MODEL"
    fi
fi

# --------------------------------------------------------------------------
# 4) Verify and show what's installed.
# --------------------------------------------------------------------------
printf "\n%sInstalled models:%s\n" "$BOLD" "$RESET"
ollama list 2>/dev/null || warn "Could not list models (is the service running?)."

printf "\n"
info "Other model options (all free, all via Ollama):"
info "  • A capable alternative:   ollama pull llama3.1"
info "  • Any Hugging Face GGUF:   ollama run hf.co/<user>/<repo>"
info "ghpulse defaults to '$MODEL'; change it later with GHPULSE_AGENT_MODEL=<name>."
printf "\n%s%sOllama setup complete.%s\n\n" "$GREEN" "$BOLD" "$RESET"

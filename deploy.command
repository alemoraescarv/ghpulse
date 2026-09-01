#!/usr/bin/env bash
#
# ghpulse — double-click me!
# ==========================
# This is a tiny macOS launcher. When you double-click it in Finder, it opens
# a Terminal window, moves into this folder, and runs the friendly setup
# wizard (deploy.sh). You just answer a few yes/no questions.
#
# ---------------------------------------------------------------------------
# FIRST-TIME GATEKEEPER NOTE (macOS may block downloaded scripts)
# ---------------------------------------------------------------------------
# If double-clicking shows "cannot be opened because it is from an
# unidentified developer" (or nothing happens), do ONE of these once:
#
#   • Easiest: right-click (or Control-click) this file in Finder, choose
#     "Open", then click "Open" in the dialog. macOS remembers your choice.
#
#   • Or clear the download quarantine flag in Terminal, then double-click:
#         xattr -d com.apple.quarantine deploy.command
#
# ---------------------------------------------------------------------------
# IF DOUBLE-CLICK DOES NOTHING (files not marked executable yet)
# ---------------------------------------------------------------------------
# The launcher and the wizard need the "executable" permission. Run once in
# Terminal from this folder:
#         chmod +x deploy.command deploy.sh scripts/setup-ollama.sh
# After that, double-clicking works.
#
set -u

# Move into the folder this file lives in (Finder launches from $HOME otherwise).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
cd "$HERE" || {
    echo "Could not open the project folder." >&2
    exit 1
}

# Make sure the wizard is runnable even if the executable bit got lost.
chmod +x "$HERE/deploy.sh" 2>/dev/null || true
chmod +x "$HERE/scripts/setup-ollama.sh" 2>/dev/null || true

echo "Starting the ghpulse setup wizard..."
echo

# Run the actual interactive installer.
exec bash "$HERE/deploy.sh"

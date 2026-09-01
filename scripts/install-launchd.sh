#!/usr/bin/env bash
# Generate + install ghpulse launchd agents:
#   daily  — snapshot at 07:00
#   weekly — discover + snapshot + render, Monday 08:00
# Portable: paths are resolved from this script's location, so it works from any
# clone. Override the data home with GHPULSE_HOME (defaults to ~/.ghpulse).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_BIN="$PROJECT_DIR/.venv/bin/ghpulse"
GHPULSE_HOME="${GHPULSE_HOME:-$HOME/.ghpulse}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$PROJECT_DIR/logs"
mkdir -p "$AGENTS_DIR" "$LOG_DIR"

if [ ! -x "$VENV_BIN" ]; then
  echo "error: $VENV_BIN not found — run ./deploy.sh first." >&2
  exit 1
fi

gen() {  # $1=label  $2=ghpulse subcommand  $3=StartCalendarInterval dict xml
  local label="$1" cmd="$2" cal="$3" dst="$AGENTS_DIR/$1.plist"
  cat > "$dst" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>[ -f "\$HOME/.config/ghpulse/env" ] &amp;&amp; set -a &amp;&amp; . "\$HOME/.config/ghpulse/env" &amp;&amp; set +a; export GHPULSE_HOME="$GHPULSE_HOME"; exec "$VENV_BIN" $cmd</string>
  </array>
  <key>StartCalendarInterval</key>$cal
  <key>StandardOutPath</key><string>$LOG_DIR/$label.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$label.err.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><false/>
</dict></plist>
EOF
  launchctl unload "$dst" 2>/dev/null || true
  launchctl load "$dst"
  echo "installed and loaded $label"
}

gen com.ghpulse.daily  "daily" \
  "<dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>"
gen com.ghpulse.weekly "weekly --render" \
  "<dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>"

echo
echo "Done. Token is read at run time from ~/.config/ghpulse/env (GITHUB_TOKEN=...)."
echo "Home: $GHPULSE_HOME   Logs: $LOG_DIR"

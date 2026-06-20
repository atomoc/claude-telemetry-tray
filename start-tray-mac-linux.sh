#!/usr/bin/env bash
# Run the Claude Telemetry Tray on macOS / Linux.
DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/claude-telemetry-tray.py" "$@"

#!/usr/bin/env bash
# Project the config repo into every detected harness on this machine.
# Wired to Claude Code SessionStart (see hooks/hooks.json). The engine is
# deterministic, idempotent, and spends zero LLM tokens, so running it on every
# session start is cheap and safe. Never blocks a session: exits 0 regardless.
set -uo pipefail

REPO="${HARNESS_CONFIG_REPO:-$HOME/harness-config}"

# The engine ships inside this plugin. ${CLAUDE_PLUGIN_ROOT} is the install dir
# when loaded as a plugin; fall back to this script's parent for standalone use.
ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
SCRIPT="$ROOT/skills/harness-sync/scripts/harness_sync.py"

# A source of truth is required to project from.
if [ ! -f "$REPO/CLAUDE.md" ]; then
  echo "harness-sync: no config repo at $REPO (set HARNESS_CONFIG_REPO). Skipping." >&2
  exit 0
fi

# The engine needs stdlib tomllib (Python 3.11+); pick a modern interpreter.
PY=""
for c in "${HARNESS_SYNC_PYTHON:-}" python3.13 python3.12 python3.11 python3; do
  [ -n "$c" ] && command -v "$c" >/dev/null 2>&1 || continue
  if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "harness-sync: need Python 3.11+; skipping sync." >&2
  exit 0
fi

HARNESS_CONFIG_REPO="$REPO" "$PY" "$SCRIPT" "$@" \
  || echo "harness-sync: sync reported issues (see above); continuing." >&2
exit 0

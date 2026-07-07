#!/usr/bin/env bash
# Hermetic smoke test: detect -> apply -> verify idempotence against the example
# config, using a throwaway $HOME so it never touches your real harness homes.
# Fails if the first apply errors or the second run is not all-skip.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
ENGINE="$ROOT/skills/harness-sync/scripts/harness_sync.py"
FIXTURE="$ROOT/examples/minimal-config"

PY="${DEMO_PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "smoke_test: need Python 3.11+"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.codex" "$TMP/.claude"
: > "$TMP/.codex/config.toml"        # Codex detection marker

engine() {
  HOME="$TMP" CLAUDE_HOME="$TMP/.claude" HARNESS_CONFIG_REPO="$FIXTURE" \
    "$PY" "$ENGINE" "$@" 2>&1
}

echo "== detect =="
engine --list

echo "== apply (first run) =="
OUT1="$(engine --only codex,claude)"; echo "$OUT1"
if echo "$OUT1" | grep -qE 'error='; then
  echo "SMOKE FAIL: errors on first apply"; exit 1
fi

echo "== apply (second run — must be idempotent) =="
OUT2="$(engine --only codex,claude)"; echo "$OUT2"
CHANGES="$(echo "$OUT2" | grep -oE 'changes:.*' || true)"
if echo "$CHANGES" | grep -qE 'symlink=|retarget=|translate=|prune=|adopt=|error='; then
  echo "SMOKE FAIL: second run not idempotent -> $CHANGES"; exit 1
fi

echo "SMOKE PASS: idempotent (${CHANGES:-no changes line})"

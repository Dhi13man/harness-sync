#!/usr/bin/env bash
# Hermetic smoke test: detect -> apply -> verify idempotence against the example
# config, using a throwaway home so it never touches real harness homes.
# Fails if the first apply errors or the second run is not all-skip.
# Test: harnessSync_whenRunWithThrowawayHome_thenIsHermeticAndIdempotent
set -euo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
ENGINE="$ROOT/skills/harness-sync/scripts/harness_sync.py"
FIXTURE="$ROOT/examples/minimal-config"

# Arrange
PY="${DEMO_PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "smoke_test: need Python 3.11+"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/.codex" "$TMP/.claude"
: > "$TMP/.codex/config.toml"        # Codex detection marker

# Act + Assert
if ! HOME="$TMP" USERPROFILE="$TMP" HOMEDRIVE='' HOMEPATH='' \
  "$PY" -c \
    'from pathlib import Path; import sys; sys.exit(Path.home().resolve() != Path(sys.argv[1]).resolve())' \
    "$TMP"; then
  echo "SMOKE FAIL: Python home escaped the throwaway directory"
  exit 1
fi

engine() {
  HOME="$TMP" USERPROFILE="$TMP" HOMEDRIVE='' HOMEPATH='' \
    CLAUDE_HOME="$TMP/.claude" \
    CLAUDE_CONFIG_JSON="$TMP/.claude.json" \
    HARNESS_CONFIG_REPO="$FIXTURE" \
    HARNESS_MCP_MANIFEST="$FIXTURE/mcp-servers.json" \
    "$PY" "$ENGINE" "$@" 2>&1
}

# Act
echo "== detect =="
engine --list

echo "== apply (first run) =="
OUT1="$(engine --only codex,claude)"; echo "$OUT1"
# Assert
if echo "$OUT1" | grep -qE 'error='; then
  echo "SMOKE FAIL: errors on first apply"; exit 1
fi

# Act
echo "== apply (second run: must be idempotent) =="
OUT2="$(engine --only codex,claude)"; echo "$OUT2"
CHANGES="$(echo "$OUT2" | grep -oE 'changes:.*' || true)"
# Assert
if echo "$CHANGES" | grep -qE 'symlink=|retarget=|translate=|prune=|adopt=|error='; then
  echo "SMOKE FAIL: second run not idempotent -> $CHANGES"; exit 1
fi

echo "SMOKE PASS: idempotent (${CHANGES:-no changes line})"

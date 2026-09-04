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
OMP_AGENT="$TMP/.omp/profiles/work/agent"
PI_AGENT="$TMP/pi-agent"
CURSOR_HOME="$TMP/.cursor"
mkdir -p "$TMP/.codex" "$TMP/.claude" "$OMP_AGENT" "$PI_AGENT" "$CURSOR_HOME"
: > "$TMP/.codex/config.toml"        # Codex detection marker
: > "$OMP_AGENT/config.yml"           # Oh My Pi named-profile detection marker
: > "$PI_AGENT/settings.json"          # Pi detection marker
: > "$CURSOR_HOME/cli-config.json"     # Cursor Agent CLI detection marker

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
    PI_CONFIG_DIR=.omp \
    OMP_PROFILE=work \
    PI_CODING_AGENT_DIR="$PI_AGENT" \
    "$PY" "$ENGINE" "$@" 2>&1
}

# Act
echo "== detect =="
LIST_OUT="$(engine --list)"; echo "$LIST_OUT"
if ! echo "$LIST_OUT" | grep -qE 'ohmypi.*hybrid.*profiles[/\\]work[/\\]agent'; then
  echo "SMOKE FAIL: named Oh My Pi profile was not detected"; exit 1
fi
if ! echo "$LIST_OUT" | grep -qE 'pi.*symlink.*pi-agent'; then
  echo "SMOKE FAIL: Pi override was not detected"; exit 1
fi
if ! echo "$LIST_OUT" | grep -qE 'cursor.*symlink.*\.cursor'; then
  echo "SMOKE FAIL: Cursor Agent CLI was not detected"; exit 1
fi

echo "== apply (first run) =="
OUT1="$(engine --only codex,claude,cursor,pi,ohmypi)"; echo "$OUT1"
# Assert
if echo "$OUT1" | grep -qE 'error='; then
  echo "SMOKE FAIL: errors on first apply"; exit 1
fi
if ! echo "$OUT1" | grep -qF 'ohmypi [hybrid]'; then
  echo "SMOKE FAIL: Oh My Pi was not applied"; exit 1
fi
"$PY" -c \
  'from pathlib import Path; import sys; assert all(Path(value).is_symlink() for value in sys.argv[1:])' \
  "$OMP_AGENT/AGENTS.md" "$OMP_AGENT/agents/hello.md" \
  "$OMP_AGENT/commands/hello.md" "$OMP_AGENT/skills/hello" \
  || { echo "SMOKE FAIL: Oh My Pi native links were not projected"; exit 1; }
"$PY" -c \
  'from pathlib import Path; import sys; assert all(Path(value).is_symlink() for value in sys.argv[1:])' \
  "$PI_AGENT/AGENTS.md" "$PI_AGENT/prompts/hello.md" "$PI_AGENT/skills/hello" \
  "$CURSOR_HOME/agents/hello.md" "$CURSOR_HOME/skills/hello" \
  || { echo "SMOKE FAIL: Pi or Cursor Agent native links were not projected"; exit 1; }
if [[ ! -f "$CURSOR_HOME/skills/cmd-hello/SKILL.md" ]]; then
  echo "SMOKE FAIL: Cursor command skill was not projected"; exit 1
fi
"$PY" -c \
  'import json, sys; data=json.load(open(sys.argv[1], encoding="utf-8")); assert data == {"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}' \
  "$OMP_AGENT/mcp.json" \
  || { echo "SMOKE FAIL: Oh My Pi MCP config was not projected"; exit 1; }
if [[ -e "$PI_AGENT/mcp.json" ]]; then
  echo "SMOKE FAIL: Pi core received an unsupported MCP config"; exit 1
fi

# Act
echo "== apply (second run: must be idempotent) =="
OUT2="$(engine --only codex,claude,cursor,pi,ohmypi)"; echo "$OUT2"
CHANGES="$(echo "$OUT2" | grep -oE 'changes:.*' || true)"
CHANGES="${CHANGES//$'\r'/}"
# Assert
if [[ -z "$CHANGES" ]] || ! echo "$CHANGES" | grep -qE '(^|[[:space:]])skip=[0-9]+($|[[:space:]])'; then
  echo "SMOKE FAIL: second run had no verifiable skip summary -> $CHANGES"; exit 1
fi
NON_SKIP="$(echo "$CHANGES" | tr ' ' '\n' | grep -vE '^$|^changes:$|^skip=[0-9]+$' || true)"
if [[ -n "$NON_SKIP" ]]; then
  echo "SMOKE FAIL: second run not idempotent -> $CHANGES"; exit 1
fi

echo "SMOKE PASS: idempotent (${CHANGES:-no changes line})"

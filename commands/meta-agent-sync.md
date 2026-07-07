---
allowed-tools: Read, Bash, Edit
argument-hint: "[--dry-run] [--only claude,codex,cursor,gemini] [--list] [-v]"
description: Project one config repo (agents, commands, skills, hooks, MCP, guidance) into every AI coding harness detected on this machine — Claude Code, Codex, Cursor, Gemini, and future ones — via the deterministic harness_sync.py engine.
---

# Meta-Agent Sync

One command to keep every harness at feature parity with the source-of-truth config repo plus machine-local connector overlays. Wraps `harness_sync.py` with pre-flight loading, argument parsing, and a readable result report.

**Task**: $ARGUMENTS

## Setup

- **Engine**: `${CLAUDE_PLUGIN_ROOT}/skills/harness-sync/scripts/harness_sync.py` (bundled with this plugin).
- **Config repo** (source of truth): `$HARNESS_CONFIG_REPO`, default `~/harness-config`. It must contain `CLAUDE.md` + `skills/` + `hooks/`.
- **Invocation** used in every phase below:

  ```bash
  HARNESS_CONFIG_REPO="${HARNESS_CONFIG_REPO:-$HOME/harness-config}" \
    python3 "${CLAUDE_PLUGIN_ROOT}/skills/harness-sync/scripts/harness_sync.py" <flags>
  ```

## Pre-Flight: Load the harness-sync skill

Before touching the engine, load the **harness-sync** skill and route to its `references/` for per-harness sync knowledge (detection markers, strategy selection, per-harness quirks). Every phase below references those files.

## Flags

Parse from $ARGUMENTS. All flags pass directly to `harness_sync.py`.

| Flag | Purpose | Default |
| ---- | ------- | ------- |
| `--list` | Print detected harnesses and exit | `false` |
| `--dry-run` | Report planned changes without writing | `false` |
| `--only NAMES` | Comma-separated harness names to sync (e.g., `codex`, `codex,cursor,gemini`) | All detected |
| `-v`, `--verbose` | Trace every action (symlink/retarget/translate/prune/skip/error) | `false` |

## PHASE 1: DETECT

**Objective**: Enumerate harnesses present on this machine.

1. Run the engine with `--list`.
2. Report detected harnesses (marked `✓`) vs absent (marked `·`).
3. If `config_repo` is not detected → STOP. The source of truth is missing; no sync possible.

Reference: [detection.md](../skills/harness-sync/references/detection.md)

## PHASE 2: PLAN (dry-run)

**Objective**: Show what would change before touching anything.

1. Run the engine with `--dry-run`.
2. Parse the summary line (`changes: translate=N prune=N symlink=N skip=N`).
3. If `--dry-run` was in the user's flags: STOP HERE, report, exit.
4. If any `error` count > 0: STOP. Show errors. Do not proceed to apply.
5. If counts are all `skip`: report "already in sync" and exit 0 without applying.

## PHASE 3: APPLY

**Objective**: Execute the sync.

1. Run the engine with the user's flags (minus `--dry-run` and `--list`).
2. Capture exit code. 0 = clean, 1 = partial failure, 2 = fatal.
3. Report the summary. Do not hide errors — surface them verbatim.

## PHASE 4: VERIFY

**Objective**: Prove the sync was idempotent (a re-run reports zero changes).

1. Run the engine a second time.
2. Assert the summary shows `changes: skip=N` only (no translate, symlink, retarget, or prune).
3. If non-skip actions appear on the second run, the engine has an oscillation bug. Report and halt.

Idempotence is the defining property of the sync engine. A sync that is not idempotent is broken.

## Error Handling & Recovery

| Failure Mode | Detection | Recovery | Reference |
| ------------ | --------- | -------- | --------- |
| Source of truth missing | engine exits 2 | Restore/point `HARNESS_CONFIG_REPO` at your config repo | [claude.md](../skills/harness-sync/references/claude.md) |
| Target has a real file where a symlink belongs | strategy returns `Change(ok=False, action="error")` | Decide: keep your edits or let sync overwrite | [sync-strategies.md](../skills/harness-sync/references/sync-strategies.md) |
| Detection false negative (harness installed, marker missing) | `--list` shows `·` for a harness you know is there | Run the harness once to let it create its marker file | [detection.md](../skills/harness-sync/references/detection.md) |
| Oscillation (non-idempotent second run) | Phase 4 catches this | Bug in a strategy's `_write_if_changed` or sort order; fix before next run | [sync-strategies.md](../skills/harness-sync/references/sync-strategies.md) |
| Gemini TOML parse errors downstream | Surfaces on next Gemini session | Check for unescaped triple quotes in Claude markdown body | [gemini.md](../skills/harness-sync/references/gemini.md) |
| MCP source contains inline secrets | engine emits a `warn` | Rotate the secret; configure it per-harness instead of in the shared manifest | [mcp-manifest.md](../skills/harness-sync/references/mcp-manifest.md) |

## Graceful Degradation

| Available Harnesses | Behavior | Exit |
| ------------------- | -------- | ---- |
| claude + codex + cursor + gemini | Full sync across all | 0 |
| any subset | Sync those present, skip absent (noted in report) | 0 |
| claude only | Nothing to project to; report "no projection targets" | 0 |
| no config_repo | Fatal; no source of truth | 2 |

## Output Format

```markdown
# Meta-Agent Sync: [Detect | Plan | Apply | Verify]

## Detected Harnesses
- config_repo [source]        $HARNESS_CONFIG_REPO
- claude   [symlink]          ~/.claude
- codex    [symlink]          ~/.codex
- cursor   [symlink]          ~/.cursor
- gemini   [translate]        ~/.gemini

## Changes (Applied | Planned)
| Action    | Count | Notes                              |
| --------- | ----- | ---------------------------------- |
| symlink   | N     | Codex/Cursor per-child skill links |
| translate | N     | Command wrappers + Gemini TOMLs    |
| prune     | N     | Removed stale artifacts            |
| skip      | N     | Already current                    |
| error     | N     | Surfaced above                     |

## Idempotence Check
Second run reported: skip=N (0 other actions) → PASS / FAIL
```

## Completion Criteria

**MUST** (blocking):

- [ ] PHASE 1 ran — detected harness list printed
- [ ] PHASE 2 ran — dry-run plan summarized
- [ ] PHASE 3 ran (unless `--dry-run` set) — actual sync applied
- [ ] PHASE 4 ran (unless `--dry-run` set) — idempotence check passed
- [ ] No `error` count on the applied run, or errors surfaced verbatim with cause

## Usage

```bash
/meta-agent-sync                 # sync everything
/meta-agent-sync --dry-run       # preview changes before committing
/meta-agent-sync --only codex    # only sync Codex
/meta-agent-sync -v              # trace every action
/meta-agent-sync --list          # just show what's detected
```

## Integration

Runs automatically on every Claude Code session start when this plugin is installed (see `hooks/hooks.json`). Manual invocation is for forcing a refresh after bulk changes, dry-run verification before commits, debugging with `-v`, or onboarding a new machine (the first run does the heavy backfill).

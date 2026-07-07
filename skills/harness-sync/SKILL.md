---
name: harness-sync
description: Use when adding, debugging, or reasoning about syncing agent config (skills, agents, commands, hooks, MCP servers, guidance) across AI coding harnesses — Claude Code, Codex, Cursor, Gemini, and future ones — via harness_sync.py or the /meta-agent-sync command. Covers detection markers, symlink-vs-translate strategy selection, per-harness quirks, the shared MCP manifest, secret detection, and the idempotence contract.
---

# Harness Sync

Keeps every detected AI coding harness on a machine at feature parity with one source-of-truth config repo. The engine is `scripts/harness_sync.py`; the user-facing entry point is the `/meta-agent-sync` command. The default config repo path is `~/harness-config`; set `HARNESS_CONFIG_REPO` for a non-standard checkout.

## Core Principle

**The config repo is the source of truth. Every harness home is a projection or a machine-local overlay.** Edits flow outward from the cloned repo. A harness that cannot fit this rule does not belong in the sync engine.

## How the engine works

- Every harness is one declarative spec dict in `_harness_specs()`: a `name`, a `home` path, a `role`, a `detect(home)` predicate, and a list of `artifacts`.
- Each artifact names a `source` (a path in the config repo), a `target_rel` (relative path in the harness home), and a **strategy** — `symlink`, per-child `symlink_children`, or a `translate_*` that rewrites the source into the harness's native format (TOML for Gemini, `config.toml`/`hooks.json` for Codex, etc.).
- The driver detects harnesses, then runs each artifact's strategy, collecting a `Report` of `Change(action, target, ok)` rows. Actions: `symlink`, `retarget`, `adopt`, `translate`, `prune`, `skip`, `error`.
- **Idempotence is the contract**: a second run must report `skip` only. Strategies skip writes when content is byte-identical (`_write_if_changed`) so re-runs don't churn mtimes or trigger file-watchers.

Adding a harness is a dict entry plus (if it needs a new format) a strategy — never a one-off script. See CONTRIBUTING.md.

## Reference Routing

| When you are about to... | Read FIRST | Key content |
| ------------------------ | ---------- | ----------- |
| Add a new harness to the engine | [references/detection.md](references/detection.md) + [references/sync-strategies.md](references/sync-strategies.md) | Detection marker conventions, strategy selection matrix, harness-spec dict shape |
| Debug why a harness isn't detected | [references/detection.md](references/detection.md) | Detection order, false-positive traps, per-harness marker files |
| Choose between symlink and translate | [references/sync-strategies.md](references/sync-strategies.md) | Decision tree, format-compatibility matrix, idempotence requirements |
| Sync to Codex specifically | [references/codex.md](references/codex.md) | `.system/` preservation, per-child symlinks, command projection, config trust |
| Sync to Cursor specifically | [references/cursor.md](references/cursor.md) | `skills-cursor/` preservation, personal skill target, command wrappers |
| Sync to Gemini specifically | [references/gemini.md](references/gemini.md) | TOML escaping, skill→command expansion, `index.json` contract |
| Understand config repo vs. harness home | [references/claude.md](references/claude.md) | Source-of-truth invariants, Claude projection, machine-local overlays |
| Handle MCP servers / secrets across machines | [references/mcp-manifest.md](references/mcp-manifest.md) | Shared manifest rules, secret-shape detection, per-harness sidecars |

## Running it

```bash
# via the command (auto-loads this skill):
/meta-agent-sync --dry-run

# or the engine directly:
HARNESS_CONFIG_REPO=~/harness-config python3 scripts/harness_sync.py --list
```

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Write a one-off shell script per harness | N dangling scripts to maintain; drift is silent | Add a spec dict + strategy to `harness_sync.py` |
| Treat a harness as bidirectional | Conflict resolution is a cost nobody pays for | Always project outward; if a harness diverges, it's wrong |
| Hard-code per-harness paths for skills/agents/commands | Couples the engine to today's names | Express as strategy + relative target, reuse across harnesses |
| Wipe and rewrite target dirs every sync | Breaks file-watchers, churns mtimes, loses tool-managed files (Codex `.system/`) | Diff and write only if content changed; preserve tool-managed entries |
| Sync without an idempotence test | First run looks fine; the second reveals oscillation | Require a second run to report zero changes before declaring success |

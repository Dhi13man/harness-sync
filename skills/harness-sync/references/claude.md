# Config Repo and Claude Projection

The cloned config repo is the authoritative home for shared agent capability. `~/.claude` is the Claude Code harness home: it receives symlinks to shared files and keeps Claude-owned runtime state locally.

## Source Resolution

`harness_sync.py` resolves the config repo in this order:

1. `HARNESS_CONFIG_REPO`
2. the repo the engine sits inside, but only if that repo looks like a config repo (`CLAUDE.md` + `skills/` + `hooks/`) - so installing this tool as a standalone plugin never mistakes the plugin's own `skills/`/`hooks/` for the source of truth
3. `~/harness-config`

The default checkout is `~/harness-config`. If you use a non-standard path, set `HARNESS_CONFIG_REPO` to that checkout and the projection model does not change.

## Invariants

| # | Invariant | Enforcement |
| - | --------- | ----------- |
| 1 | Shared content is edited in the config repo | Harness targets use symlinks or generated files; edits flow outward |
| 2 | New skills, commands, agents, hooks, and guidance are added under the repo first | The sync engine discovers repo artifacts and projects them |
| 3 | Harness homes are not reverse-synced into the repo | No strategy writes into `CONFIG_HOME` |
| 4 | A missing or incomplete config repo is fatal | `harness_sync.py` exits 2 before projection |
| 5 | Machine-local overlays stay local unless explicitly opted in | MCP and runtime state can contain machine-specific endpoints or secrets |

## Shared Artifact Inventory

| Artifact | Repo path | Claude target | Other projections |
| -------- | --------- | ------------- | ----------------- |
| Agents | `agents/` | `~/.claude/agents` symlink | Codex/Cursor/Oh My Pi native agent links; Pi core has no subagent surface |
| Commands | `commands/` | `~/.claude/commands` symlink | Codex/Cursor skill wrappers, Gemini TOML commands, Pi prompts, Oh My Pi commands |
| Skills | `skills/` | `~/.claude/skills` symlink | Codex/Cursor/Pi/Oh My Pi per-child symlinks, Gemini skill commands and index |
| Hooks | `hooks/*.sh` | `~/.claude/hooks` symlink | Codex/Cursor per-child symlinks plus hook JSON translation |
| Guidance | `CLAUDE.md` | `~/.claude/CLAUDE.md` symlink | Codex/Pi/Oh My Pi `AGENTS.md` symlink |
| Settings | `settings.json` | `~/.claude/settings.json` symlink | Portable hook wiring translated into Codex/Cursor |
| Statusline | `statusline-command.sh` | `~/.claude/statusline-command.sh` symlink | Not projected elsewhere |
| Memory | Claude runtime memory dir | Local Claude runtime state | Codex `memories/` symlink |
| Shared MCP definitions | repo `mcp-servers.json` or explicit override | manifest-owned entries in `~/.claude.json` | Codex/Cursor/Gemini/Oh My Pi managed config blocks; no Pi core projection |

## Claude-Local Artifacts

These live under `~/.claude` but do not belong in the shared repo projection.

| Artifact | Path | Why local |
| -------- | ---- | --------- |
| Runtime state | `sessions/`, `projects/`, `todos/`, `telemetry/` | Per-harness state, not portable capability |
| Local settings | `settings.local.json` | Machine-specific overrides |
| Claude plugin state | `plugins/` | Claude Code registry/runtime data |
| Legacy/local MCP file | `mcp-servers.json` when it is a real file | Not an implicit shared source; use an explicit manifest override if it should be projected |
| Claude connector registry | `~/.claude.json` `mcpServers` | Sync manages only manifest-owned names; unowned connectors and Claude OAuth state remain local |

## MCP Source Precedence

Shared MCP definitions have one canonical manifest. The sync engine resolves it using this precedence:

1. `HARNESS_MCP_MANIFEST`
2. `CLAUDE_MCP_MANIFEST`
3. repo `mcp-servers.json`

The manifest is projected into the Claude connector registry resolved from `CLAUDE_CONFIG_JSON`, then `CLAUDE_CONFIG_PATH`, then `~/.claude.json`. A sidecar scoped beside that exact target lets the engine update and prune only manifest-owned names. Unowned Claude connectors and native OAuth state always stay in Claude; the repo manifest is the only cross-harness source. See [mcp-manifest.md](mcp-manifest.md).

## Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | **Add shared content to the config repo first, then sync** | Any other order creates divergent state the engine cannot heal |
| 2 | **Do not hand-copy shared content between harness homes** | Manual copies drift silently; projection strategies are the source of truth |
| 3 | **Keep symlinks for format-compatible harnesses** | Symlinks are still the lowest-drift projection mechanism when the target reads the same format |
| 4 | **Use translation only when the harness format requires it** | Generated files are deterministic but lossy; repo markdown remains canonical |
| 5 | **Project one shared MCP manifest and preserve unowned local connectors** | Shared definitions do not drift while client-owned credentials and OAuth state remain outside the portable config |

## Moving or renaming your config repo

If you move your config repo to a new path:

1. Move or re-clone the repo to the new location.
2. Point `HARNESS_CONFIG_REPO` at the new checkout in your harness launch environment.
3. Re-run the sync and require an all-`skip` second pass.

Do not rename the harness homes (`~/.claude`, `~/.codex`, and so on). They are native application state; only the config repo path changes.

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Treat `~/.claude` as canonical because it is the first harness | Breaks repair when Claude symlinks drift and makes repo rename harder | Treat the repo as canonical and Claude as a target |
| Symlink Gemini's generated command dir back into the repo | Creates a cycle; generated TOML is lossy and harness-specific | Regenerate Gemini files from repo markdown |
| Treat a real `~/.claude/mcp-servers.json` as an implicit override | Creates a second shared source that can drift | Use the repo manifest or an explicit override variable |
| Copy credential-bearing Claude connectors into shared config | Distributes secrets or client-owned OAuth material | Commit exact `${VAR}` references and leave native OAuth stores local |
| Edit generated Codex/Cursor command wrappers by hand | Next sync overwrites them | Edit the source command under `commands/` |

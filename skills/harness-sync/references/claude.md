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
| Agents | `agents/` | `~/.claude/agents` symlink | Codex `~/.codex/agents` symlink |
| Commands | `commands/` | `~/.claude/commands` symlink | Codex/Cursor skill wrappers, Gemini TOML commands |
| Skills | `skills/` | `~/.claude/skills` symlink | Codex/Cursor per-child symlinks, Gemini skill commands and index |
| Hooks | `hooks/*.sh` | `~/.claude/hooks` symlink | Codex/Cursor per-child symlinks plus hook JSON translation |
| Guidance | `CLAUDE.md` | `~/.claude/CLAUDE.md` symlink | Codex `AGENTS.md` symlink |
| Settings | `settings.json` | `~/.claude/settings.json` symlink | Portable hook wiring translated into Codex/Cursor |
| Statusline | `statusline-command.sh` | `~/.claude/statusline-command.sh` symlink | Not projected elsewhere |
| Memory | Claude runtime memory dir | Local Claude runtime state | Codex `memories/` symlink |
| MCP source set | local override or repo `mcp-servers.json` plus `~/.claude.json` connector registry | Claude local files remain local | Codex/Cursor/Gemini managed config blocks |

## Claude-Local Artifacts

These live under `~/.claude` but do not belong in the shared repo projection.

| Artifact | Path | Why local |
| -------- | ---- | --------- |
| Runtime state | `sessions/`, `projects/`, `todos/`, `telemetry/` | Per-harness state, not portable capability |
| Local settings | `settings.local.json` | Machine-specific overrides |
| Claude plugin state | `plugins/` | Claude Code registry/runtime data |
| Local MCP overlay | `mcp-servers.json` when it is a real file | May contain machine-specific connector endpoints; use `HARNESS_MCP_MANIFEST` to override |
| Claude connector registry | `~/.claude.json` `mcpServers` | Claude Code app connectors are machine-local; sync reads them as an overlay and does not write them |

## MCP Source Precedence

MCP is deliberately not a pure repo-only source because connector endpoints can be machine-local. The sync engine first picks a manifest using this precedence:

1. `HARNESS_MCP_MANIFEST`
2. `CLAUDE_MCP_MANIFEST`
3. real, non-symlink `~/.claude/mcp-servers.json`
4. repo `mcp-servers.json`
5. `~/.claude/mcp-servers.json` as a final missing-file placeholder

Then it appends missing entries from the Claude connector registry, resolved from `CLAUDE_CONFIG_JSON`, then `CLAUDE_CONFIG_PATH`, then `~/.claude.json`. Manifest entries win on canonical-name conflicts. This preserves local connector setup while keeping a repo manifest available for portable MCP servers.

## Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | **Add shared content to the config repo first, then sync** | Any other order creates divergent state the engine cannot heal |
| 2 | **Do not hand-copy shared content between harness homes** | Manual copies drift silently; projection strategies are the source of truth |
| 3 | **Keep symlinks for format-compatible harnesses** | Symlinks are still the lowest-drift projection mechanism when the target reads the same format |
| 4 | **Use translation only when the harness format requires it** | Generated files are deterministic but lossy; repo markdown remains canonical |
| 5 | **Preserve real Claude MCP files and read `~/.claude.json` as a local overlay** | Avoid accidentally committing or distributing machine-local connector config while still projecting installed app connectors to other harnesses |

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
| Copy `mcp-servers.json` with local connector endpoints into the repo without review | May distribute machine-specific or sensitive endpoints | Keep it as a local overlay or set an explicit manifest path |
| Ignore `~/.claude.json` when syncing MCPs | Claude app connectors disappear from Codex/Cursor/Gemini even though Claude has them | Merge Claude's connector registry as a local overlay during MCP projection |
| Edit generated Codex/Cursor command wrappers by hand | Next sync overwrites them | Edit the source command under `commands/` |

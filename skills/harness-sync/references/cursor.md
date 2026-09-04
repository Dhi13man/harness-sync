# Cursor Desktop and Agent CLI

Cursor desktop and the `cursor-agent` CLI share the same user agents, skills, hooks, rules, and MCP configuration under `~/.cursor`. The config repo is the source of truth; both clients consume one projection rather than parallel copies.

## Layout Mapping

| Config repo source | Cursor target | Strategy | Notes |
| ------------- | ------------- | -------- | ----- |
| `agents/*` | `~/.cursor/agents/*` | per-child symlink | User subagents are available in the editor and CLI. |
| `skills/*` | `~/.cursor/skills/*` | per-child symlink | Cursor reads `SKILL.md` folders natively. |
| `commands/*` | `~/.cursor/skills/cmd-<name>/SKILL.md` | `strategy_command_to_cursor_skill` | Generates a Cursor Agent Skill wrapper per command. |
| `hooks/*` | `~/.cursor/hooks/*` | per-child symlink | Hook scripts stay shared; Cursor owns its hook wiring. |
| `settings.json` bootstrap hook | `~/.cursor/hooks.json` `sessionStart` | `strategy_translate_bootstrap_hook_to_cursor_json` | Installs only the portable startup bootstrap hook. |
| MCP manifest | `~/.cursor/mcp.json` `.mcpServers.*` | `strategy_mcp_to_cursor` | Projects only the canonical repo manifest and preserves Cursor-only servers not managed by sync. |

## The `skills-cursor/` Rule

Cursor owns `~/.cursor/skills-cursor/` for built-in skills. Never write generated or symlinked artifacts there. Use it only as a detection marker.

Projecting into `~/.cursor/skills/` keeps the user skill surface separate from Cursor's managed inventory and avoids overwrites on Cursor updates.

## Detection Signature

Desktop detection uses `~/.cursor/argv.json` or the built-in `~/.cursor/skills-cursor/` directory. Agent CLI detection uses its resolved `cli-config.json`: `CURSOR_CONFIG_DIR`, then `$XDG_CONFIG_HOME/cursor` on Linux/BSD, then `~/.cursor`.

All markers are Cursor-owned. Do not detect from generated `~/.cursor/agents/` or `~/.cursor/skills/`, because the sync engine creates them. A CLI-only marker can live outside `~/.cursor`; the projection still targets `~/.cursor` because Cursor documents those user-level capabilities as shared with the editor.

## Command Projection

Cursor Agent Skills are the portable surface for shared slash commands. Each command in the repo `commands/` directory becomes `~/.cursor/skills/cmd-<name>/SKILL.md`; the wrapper includes the original command description plus trigger keywords (`<name>`, `/<name>`, `run <name>`, `do <name>`), and rewrites `$ARGUMENTS` to a plain placeholder.

Commands that rely on Claude-only orchestration tools degrade the same way they do in Codex: the workflow prose remains available, but the agent may need to execute tool-specific steps inline or narrate unavailable orchestration.

## Hook Projection

Cursor desktop and Agent CLI support user hooks in `~/.cursor/hooks.json` and scripts under `~/.cursor/hooks/`. The sync engine projects only the portable startup bootstrap hook from Claude's `SessionStart` config into Cursor's `sessionStart` event. It does not translate arbitrary Claude hooks because event names, payloads, and matcher semantics differ.

The generated Cursor hook runs `hooks/bootstrap-agent-config.sh`, which is a symlink to the repo-owned script. That script clones or updates the config repo, restores Claude symlinks, and reruns the harness sync engine.

## MCP Projection

Cursor's global user MCP config lives at `~/.cursor/mcp.json` and is read by desktop and Agent CLI. The sync engine writes the merged MCP source set into its `mcpServers` object and tracks managed names in `~/.cursor/.harness-sync-managed-mcp.json`, so it can prune removed shared servers without deleting Cursor-only entries like `readscripts-cloud-mcp`.

`cli-config.json` is Cursor CLI-owned behavior such as model selection, permissions, and update policy. Sync uses it only as an installation marker and never changes it.

## Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | Always project shared skills into `~/.cursor/skills`, never `~/.cursor/skills-cursor` | Cursor manages built-in skills and can rewrite that directory. |
| 2 | Prefer per-child symlinks over copying skills | Edits in Claude should become visible to Cursor without another manual copy. |
| 3 | Project Claude commands as skill wrappers | Cursor has a skill surface that can carry reusable command workflows. |
| 4 | Project only portable startup hook wiring | Cursor's hook event schema is not Claude's hook schema. |
| 5 | Project shared MCP servers through `~/.cursor/mcp.json`, not Cursor project runtime catalogs | `projects/*/mcps` is generated runtime state; the global config is the user-editable source. |
| 6 | Do not sync Cursor runtime state | `projects/`, transcripts, extension state, blocklists, and tracking databases are Cursor-owned. |
| 7 | Keep desktop and Agent CLI on one user projection | Both clients consume the documented `~/.cursor` user surfaces; a second CLI copy would drift. |

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Write to `~/.cursor/skills-cursor/` | Cursor owns it; updates can overwrite generated files | Use `~/.cursor/skills/` |
| Detect Cursor from generated agents or skills | The sync engine creates them, so this would be circular evidence | Detect desktop from `argv.json`/`skills-cursor/`, or CLI from resolved `cli-config.json` |
| Copy Claude skills manually | Copies drift silently | Let `strategy_symlink_children` manage per-skill symlinks |
| Translate every Claude hook into Cursor | Payloads and matcher semantics differ | Translate only the bootstrap hook unless a hook is explicitly made Cursor-compatible |
| Write MCPs into `~/.cursor/projects/*/mcps` | Those are generated server catalogs and auth status, not user config | Write `~/.cursor/mcp.json` and let Cursor refresh its runtime catalogs |
| Sync Cursor transcripts or project state | They are runtime state, not shared agent capability | Leave `~/.cursor/projects/` untouched |
| Copy the projection into the CLI config directory | `CURSOR_CONFIG_DIR` selects CLI settings, not a second user capability home | Keep agents, skills, hooks, and MCP under `~/.cursor` for both clients |

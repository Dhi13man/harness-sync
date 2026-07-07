# Cursor - Skill Projection

Cursor (`~/.cursor/`) supports Agent Skills with `SKILL.md` folders. The config repo is the source of truth; Cursor receives a projection under the personal skill directory.

## Layout Mapping

| Config repo source | Cursor target | Strategy | Notes |
| ------------- | ------------- | -------- | ----- |
| `skills/*` | `~/.cursor/skills/*` | per-child symlink | Cursor reads `SKILL.md` folders natively. |
| `commands/*` | `~/.cursor/skills/cmd-<name>/SKILL.md` | `strategy_command_to_cursor_skill` | Generates a Cursor Agent Skill wrapper per command. |
| `hooks/*` | `~/.cursor/hooks/*` | per-child symlink | Hook scripts stay shared; Cursor owns its hook wiring. |
| `settings.json` bootstrap hook | `~/.cursor/hooks.json` `sessionStart` | `strategy_translate_bootstrap_hook_to_cursor_json` | Installs only the portable startup bootstrap hook. |
| MCP source set | `~/.cursor/mcp.json` `.mcpServers.*` | `strategy_mcp_to_cursor` | Uses selected MCP manifest plus Claude connector registry overlay; preserves Cursor-only servers not managed by sync. |

## The `skills-cursor/` Rule

Cursor owns `~/.cursor/skills-cursor/` for built-in skills. Never write generated or symlinked artifacts there. Use it only as a detection marker.

Projecting into `~/.cursor/skills/` keeps the user skill surface separate from Cursor's managed inventory and avoids overwrites on Cursor updates.

## Detection Signature

```python
detect = lambda h: (h / "argv.json").exists() or (h / "skills-cursor").is_dir()
```

`argv.json` and `skills-cursor/` are Cursor-owned. Do not detect from `~/.cursor/skills/`, because the sync engine creates that directory.

## Command Projection

Cursor Agent Skills are the portable surface for shared slash commands. Each command in the repo `commands/` directory becomes `~/.cursor/skills/cmd-<name>/SKILL.md`; the wrapper includes the original command description plus trigger keywords (`<name>`, `/<name>`, `run <name>`, `do <name>`), and rewrites `$ARGUMENTS` to a plain placeholder.

Commands that rely on Claude-only orchestration tools degrade the same way they do in Codex: the workflow prose remains available, but the agent may need to execute tool-specific steps inline or narrate unavailable orchestration.

## Hook Projection

Cursor supports user hooks in `~/.cursor/hooks.json` and scripts under `~/.cursor/hooks/`. The sync engine projects only the portable startup bootstrap hook from Claude's `SessionStart` config into Cursor's `sessionStart` event. It does not translate arbitrary Claude hooks because event names, payloads, and matcher semantics differ.

The generated Cursor hook runs `hooks/bootstrap-agent-config.sh`, which is a symlink to the repo-owned script. That script clones or updates the config repo, restores Claude symlinks, and reruns the harness sync engine.

## MCP Projection

Cursor's global user MCP config lives at `~/.cursor/mcp.json`. The sync engine writes the merged MCP source set into its `mcpServers` object and tracks managed names in `~/.cursor/.harness-sync-managed-mcp.json`, so it can prune removed shared servers without deleting Cursor-only entries like `readscripts-cloud-mcp`.

## Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | Always project shared skills into `~/.cursor/skills`, never `~/.cursor/skills-cursor` | Cursor manages built-in skills and can rewrite that directory. |
| 2 | Prefer per-child symlinks over copying skills | Edits in Claude should become visible to Cursor without another manual copy. |
| 3 | Project Claude commands as skill wrappers | Cursor has a skill surface that can carry reusable command workflows. |
| 4 | Project only portable startup hook wiring | Cursor's hook event schema is not Claude's hook schema. |
| 5 | Project shared MCP servers through `~/.cursor/mcp.json`, not Cursor project runtime catalogs | `projects/*/mcps` is generated runtime state; the global config is the user-editable source. |
| 6 | Do not sync Cursor runtime state | `projects/`, transcripts, extension state, blocklists, and tracking databases are Cursor-owned. |

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Write to `~/.cursor/skills-cursor/` | Cursor owns it; updates can overwrite generated files | Use `~/.cursor/skills/` |
| Detect Cursor from `~/.cursor/skills/` | The sync engine creates it, so this would be circular evidence | Detect from `argv.json` or `skills-cursor/` |
| Copy Claude skills manually | Copies drift silently | Let `strategy_symlink_children` manage per-skill symlinks |
| Translate every Claude hook into Cursor | Payloads and matcher semantics differ | Translate only the bootstrap hook unless a hook is explicitly made Cursor-compatible |
| Write MCPs into `~/.cursor/projects/*/mcps` | Those are generated server catalogs and auth status, not user config | Write `~/.cursor/mcp.json` and let Cursor refresh its runtime catalogs |
| Sync Cursor transcripts or project state | They are runtime state, not shared agent capability | Leave `~/.cursor/projects/` untouched |

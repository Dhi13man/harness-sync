# Grok Build CLI

Grok Build (`grok`) stores user config under `$GROK_HOME` (default `~/.grok`). Skills, commands, and agent markdown are native. MCP lives in `config.toml`. Auth and OAuth refresh state stay Grok-owned.

## Layout Mapping

| Config repo source | Grok target | Strategy | Notes |
| --- | --- | --- | --- |
| `agents/**/*.md` | `~/.grok/agents/<name>.md` | `strategy_symlink_flattened_files` | Grok discovers top-level agent markdown; nested repo roles flatten by filename. |
| `skills/*` | `~/.grok/skills/*` | per-child symlink | Same Agent Skills layout. Real local skills stay in place. |
| `commands/*.md` | `~/.grok/commands/*.md` | per-child symlink | Grok treats flat command markdown as slash commands, including `$ARGUMENTS`. |
| `AGENTS.md` | `~/.grok/rules/AGENTS.md` | file symlink | Home-level rules are scanned from `$GROK_HOME/rules/`. |
| MCP manifest | `~/.grok/config.toml` `[mcp_servers.*]` inside managed-block markers | `strategy_mcp_to_grok` | Projects only the canonical repo manifest. Unowned Grok servers stay outside the block. |

## Detection Signature

Grok-owned markers: `config.toml`, `auth.json`, or `version.json`. `GROK_HOME` relocates the home. Do not detect from projected `skills/`, `commands/`, `agents/`, or `rules/`.

MCP projection requires a real `config.toml`. A Grok install that has only `auth.json` or `version.json` still receives skills, commands, agents, and rules; MCP reports `target config.toml missing` until Grok writes the file.

## MCP Projection

Grok expands `${VAR}` in `[mcp_servers.*]` `url`, `command`, `args`, `env`, and `headers` at load time. The engine writes those references unchanged. It does not translate them into Codex `env_vars` / `bearer_token_env_var`.

OAuth tokens stay in `~/.grok/mcp_credentials.json`. Login stays in `~/.grok/auth.json`. Sync never reads, copies, or links those files.

## What Is Not Projected

| Surface | Why |
| --- | --- |
| Hook wiring | Grok already loads `~/.claude/settings.json` when Claude hook compat is on. A second JSON copy would double-run handlers whose command strings differ after timeout conversion (Grok timeouts are seconds; Claude's are milliseconds). |
| Memory, `bundled/`, sessions, logs | Grok-owned runtime and platform files. |

## Stances

1. Project commands as `~/.grok/commands/*.md`, never as `cmd-*` skill wrappers.
2. Flatten nested agents into `~/.grok/agents/<filename>`.
3. Write MCP only inside managed-block markers.
4. Keep `${VAR}` in Grok MCP tables.
5. Never touch `auth.json` or `mcp_credentials.json`.

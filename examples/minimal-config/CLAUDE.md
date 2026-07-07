# Example config repo

A minimal source-of-truth config for `harness-sync`. Everything here is projected
into every detected harness. Replace it with your own agents, skills, commands,
hooks, and guidance — the layout is what matters.

- `agents/` — subagent definitions
- `commands/` — slash commands
- `skills/` — skills (`SKILL.md` + optional `references/`, `scripts/`)
- `hooks/` — harness-agnostic hook scripts
- `settings.json` — hook wiring / permissions
- `mcp-servers.json` — MCP server manifest (keep secrets machine-local)

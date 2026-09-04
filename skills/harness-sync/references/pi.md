# Pi - Minimal Native Projection

Pi and Oh My Pi are separate harnesses. Pi keeps a deliberately small core under `~/.pi/agent`; Oh My Pi is a fork with its own `.omp` profile system, native MCP configuration, agents, hooks, and a different auth store. Do not reuse the Oh My Pi projection for Pi.

The paths and capability boundary below follow Pi source at [`47236c8`](https://github.com/badlogic/pi-mono/tree/47236c84450656043dd8fb21c8513d1421505ae3/packages/coding-agent). Pi's own README explicitly documents [global guidance, prompts, and skills](https://github.com/badlogic/pi-mono/blob/47236c84450656043dd8fb21c8513d1421505ae3/packages/coding-agent/README.md#context-files) and states that [MCP and subagents are not built into core](https://github.com/badlogic/pi-mono/blob/47236c84450656043dd8fb21c8513d1421505ae3/packages/coding-agent/README.md#philosophy).

## Native Mapping

| Config repo source | Pi target | Strategy | Notes |
| ------------------ | --------- | -------- | ----- |
| `AGENTS.md` | active agent dir `AGENTS.md` | symlink | Pi loads this as global context. |
| `commands/*.md` | active agent dir `prompts/*.md` | per-child symlink | Pi exposes prompt templates as `/<filename>`. Its parser consumes `description` and `argument-hint` frontmatter. |
| `skills/*` | active agent dir `skills/*` | per-child symlink | Pi implements the Agent Skills standard. |
| `agents/*` | none | not synced | Pi core deliberately has no native subagent definition surface. |
| `hooks/*` | none | not synced | Pi extensions can implement lifecycle behavior, but there is no compatible declarative hook surface. |
| `mcp-servers.json` | none | explicit `skip` | Pi core deliberately has no native MCP config. |

The active directory is `~/.pi/agent` unless `PI_CODING_AGENT_DIR` relocates it. Detection uses Pi-owned `settings.json`, `auth.json`, or `sessions/`; projected guidance, prompts, and skills are never used as signatures.

## Authentication Boundary

Pi owns `auth.json`, provider login flows, custom providers, extensions, package state, trust decisions, and sessions. Harness sync never reads, copies, links, or edits them. A Pi MCP extension may define its own config and credential contract; install and manage that extension explicitly rather than guessing a universal file shape.

## Stances

1. Project only native Pi core capabilities.
2. Map shared commands to `prompts/`, not `commands/`.
3. Never create an inert `mcp.json` or copy Oh My Pi's `agent.db` model into Pi.
4. Never install or configure an MCP/subagent extension implicitly; Pi extensions execute code and require a separate trust decision.

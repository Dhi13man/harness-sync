# Oh My Pi

Oh My Pi (OMP) keeps user-level configuration in its active agent directory. The default is `~/.omp/agent`; named profiles use `~/${PI_CONFIG_DIR:-.omp}/profiles/<name>/agent`, while `PI_CODING_AGENT_DIR` may relocate only the default profile. `OMP_PROFILE` takes precedence over legacy `PI_PROFILE`, including when an empty value selects the default profile.

OMP is a feature-rich fork of Pi, not Pi's configuration directory under another name. Pi uses `~/.pi/agent`, `prompts/`, and no core MCP or subagent system; OMP uses `~/.omp/agent`, native `commands/` and `agents/`, named profiles, and native MCP. Sync detects and projects them independently, so installing both is supported.

## Capability Map

| Config repo source | OMP target | Strategy | Notes |
| ------------------ | ---------- | -------- | ----- |
| `AGENTS.md` | `<agent-dir>/AGENTS.md` | file symlink | Native user context |
| `agents/*` | `<agent-dir>/agents/*` | per-child symlink | Native task-agent Markdown |
| `commands/*` | `<agent-dir>/commands/*` | per-child symlink | Native slash-command Markdown |
| `skills/*` | `<agent-dir>/skills/*` | per-child symlink | Native one-level Agent Skills layout |
| MCP source set | `<agent-dir>/mcp.json` | owned JSON merge | Preserves unowned servers, enable/disable lists, and same-endpoint local `auth`/`oauth` metadata |

OMP can import several foreign harness formats, but native projection does not depend on optional foreign-user discovery and gives the shared source native priority. These surfaces and precedence are grounded in OMP's [context](https://github.com/can1357/oh-my-pi/blob/f241301c83726afe75a847e919b89977a54dafbe/docs/context-files.md), [skill](https://github.com/can1357/oh-my-pi/blob/f241301c83726afe75a847e919b89977a54dafbe/docs/skills.md), [command](https://github.com/can1357/oh-my-pi/blob/f241301c83726afe75a847e919b89977a54dafbe/docs/slash-command-internals.md), and [task-agent](https://github.com/can1357/oh-my-pi/blob/f241301c83726afe75a847e919b89977a54dafbe/docs/task-agent-discovery.md) documentation.

## MCP and Authentication Boundary

OMP's preferred user MCP file is `<agent-dir>/mcp.json`. It supports the shared `mcpServers` shape and expands `${VAR}` recursively. The cross-harness manifest permits only exact `${VAR}` references because Cursor and Codex cannot preserve OMP's default-value form. See OMP's [MCP configuration reference](https://github.com/can1357/oh-my-pi/blob/f241301c83726afe75a847e919b89977a54dafbe/docs/mcp-config.md).

Do not symlink `mcp.json`: OMP's [config writer](https://github.com/can1357/oh-my-pi/blob/f241301c83726afe75a847e919b89977a54dafbe/packages/coding-agent/src/mcp/config-writer.ts#L56-L77) publishes by temporary-file rename and can replace the link. The sync engine instead owns individual server names through a sidecar, joins OMP's platform-native file lock before its read-modify-write, publishes by same-directory atomic replace, and creates a missing `mcp.json` with mode `0600`.

OMP stores managed MCP OAuth refresh material in the active profile's auth storage (`agent.db` locally, or its documented auth broker). A definition at the same URL resolves that profile-specific credential automatically. Sync projects the definition and secret references, preserves OMP-local `auth`/`oauth` metadata when the transport identity is unchanged, and never copies, links, parses, or publishes `agent.db`. If a shared definition changes an authenticated endpoint, sync stops instead of carrying the binding to a different endpoint. This follows OMP's [profile-scoped OAuth contract](https://github.com/can1357/oh-my-pi/blob/f241301c83726afe75a847e919b89977a54dafbe/docs/mcp-config.md#auth-fields).

## Verification

```bash
python3 skills/harness-sync/scripts/harness_sync.py --only ohmypi --dry-run -v
python3 skills/harness-sync/scripts/harness_sync.py --only ohmypi
python3 skills/harness-sync/scripts/harness_sync.py --only ohmypi
```

The final run must report only skips. Inspect connection state inside OMP with `/mcp`; authenticate an OAuth server in that profile with `/mcp reauth <name>`.

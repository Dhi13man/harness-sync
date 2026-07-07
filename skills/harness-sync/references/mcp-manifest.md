# MCP Sources

Source model for MCP servers shared across harnesses. `harness_sync.py` reads the first available manifest from `HARNESS_MCP_MANIFEST`, `CLAUDE_MCP_MANIFEST`, a real local `~/.claude/mcp-servers.json`, then the repo `mcp-servers.json`. It then merges Claude Code app connectors from `~/.claude.json` `mcpServers` as a machine-local overlay and projects the merged set into each harness's native config.

The selected manifest remains authoritative for portable/shared servers. Claude's connector registry fills missing server names only; if the manifest and `~/.claude.json` contain the same canonical name, the manifest entry wins.

## Schema

```json
{
 "mcpServers": {
 "<name>": {
 "command": "npx",
 "args": ["-y", "@scope/package"],
 "type": "stdio",
 "env": { "KEY": "value" },
 "url": "https://..."
 }
 }
}
```

| Field | Required | Notes |
| ----- | -------- | ----- |
| `command` | Yes for stdio | Executable (e.g. `npx`, `uvx`, `python`, absolute path) |
| `args` | Optional | Array of CLI arguments |
| `type` | Optional | `stdio` (default) or `http` |
| `url` | Required for http | Full URL to MCP endpoint |
| `env` | Optional | Per-server environment variables (object of strings) |

Extra top-level keys are allowed and ignored by the sync engine (e.g. `$schema`, `_comment`). The selected manifest and Claude connector registry both use this `mcpServers` shape for entries the sync engine reads.

## Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | **Always** edit the selected manifest for portable/shared servers, or configure the connector in Claude for machine-local app connectors | Target edits are overwritten next sync - the merged source set is authoritative for anything listed in it |
| 2 | **Never** add secrets to the manifest if the harnesses sync via Syncthing/git | One copy = N machines. Security research cites inline secrets as the #1 MCP vuln |
| 3 | **Prefer** kebab-case server names (`sequential-thinking`) matching the community MCP convention | Canonical form; drift gets auto-resolved on sync anyway, but picking the convention keeps spelling stable |
| 4 | **Never** list a server that needs per-machine paths (e.g. `/Users/alice/..` only valid on alice's box) in the shared manifest | Breaks sync across machines; configure per-harness instead |
| 5 | **Always** keep harness-specific servers OUT of the shared repo manifest (e.g. Codex-only `oorep_mcp`, Gemini-only `context7`) | The sync engine preserves non-manifest entries; putting them in the manifest forces them everywhere |
| 6 | **Default to** stdio transport (`npx`, `uvx`) over `http` in shared manifests | Portable; http URLs often carry auth expectations that differ per machine |

## Drift Tolerance

The engine compares names by canonical form (`name.replace("_", "-").lower()`). This lets the manifest unify pre-existing spelling drift:

| Existing in harness | Manifest entry | Result |
| ------------------- | -------------- | ------ |
| `sequential_thinking` (Codex snake) | `sequential-thinking` (kebab) | Existing stripped, manifest spelling wins |
| `Sequential-Thinking` (pascal-kebab) | `sequential-thinking` | Existing stripped, manifest spelling wins |
| `sequentialthinking` (run-together) | `sequential-thinking` | Canonical differs; existing preserved alongside new |

Canonicalization merges `_` and `-`, and lowercases. It does NOT strip other punctuation. If you had two servers that are semantically different but canonically equal (rare), the sync merges them - fix by renaming before sync.

## Secret Detection

On every sync the engine scans merged MCP source values for common secret shapes:

| Pattern | Matches |
| ------- | ------- |
| `ctx7sk-[a-f0-9-]+` | Context7 API keys |
| `sk-[A-Za-z0-9]{20,}` | OpenAI/Anthropic keys |
| `gh[oprs]_[A-Za-z0-9]{36,}` | GitHub PATs/tokens |
| `xox[bpoars]-…` | Slack tokens |
| `AKIA[A-Z0-9]{16}` | AWS access key IDs |
| JWT-shaped `eyJ…\.…\.…` | JWTs |

A match produces a `warn` entry in the sync report. **The sync still propagates** - the engine surfaces the risk and lets you decide, rather than silently blocking a sync you may have intended.

## What Propagates Where

| Harness | Target | Strategy | Preserves |
| ------- | ------ | -------- | --------- |
| Codex | `~/.codex/config.toml` inside `# >>> harness-sync: mcp-servers >>>` / `<<<` marker block | `strategy_mcp_to_codex` | All `[mcp_servers.*]` tables with names NOT in the merged source set, outside the marker block |
| Cursor | `~/.cursor/mcp.json` `.mcpServers.*` | `strategy_mcp_to_cursor` | All entries whose name AND canonical form aren't in the merged source set and not in sidecar-tracked managed-names list |
| Gemini | `~/.gemini/settings.json` `.mcpServers.*` | `strategy_mcp_to_gemini` | All entries whose name AND canonical form aren't in the merged source set and not in sidecar-tracked managed-names list |
| Claude | `~/.claude.json` `.mcpServers.*` | input overlay only | Claude Code owns its app connector registry; sync reads it but does not write it |

## Source Precedence

Use this precedence to keep machine-local connector endpoints from being overwritten by the repo during bootstrap:

1. `HARNESS_MCP_MANIFEST`
2. `CLAUDE_MCP_MANIFEST`
3. real, non-symlink `~/.claude/mcp-servers.json`
4. repo `mcp-servers.json`
5. `~/.claude/mcp-servers.json` as a missing-file placeholder

After selecting the manifest, the engine reads the Claude connector registry from `CLAUDE_CONFIG_JSON`, then `CLAUDE_CONFIG_PATH`, then `~/.claude.json`. Registry entries are appended only when their canonical name is absent from the selected manifest.

## Sidecar State (Cursor and Gemini)

Cursor and Gemini need a sidecar at `~/.cursor/.harness-sync-managed-mcp.json` or `~/.gemini/.harness-sync-managed-mcp.json` - a JSON list of names we've ever written. Purpose: when a server leaves the merged source set, we can prune it only if it was ours originally. Codex doesn't need a sidecar - the marker block itself delimits our scope.

## When to Override

If you really need a server with a secret synced across machines:

- Option A (recommended): leave it per-harness, don't add to manifest. Each machine configures it once.
- Option B: add to manifest, accept the warn, and use Syncthing's scoped encryption or an env-var placeholder (not yet supported; tracked as a future extension).

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Hand-edit a synced TOML/JSON entry for a managed server | Next sync overwrites your change | Edit the selected MCP manifest or Claude connector config, then `/meta-agent-sync` |
| Put absolute paths like `~/my/script.py` in the manifest | `~` may expand differently per user; paths can be machine-specific | Use `npx`/`uvx` packages or commit script to a shared location all machines mount |
| Sync an HTTP MCP server with OAuth credentials in the URL query string | Credentials in shared file; URL-based auth often one-use | Configure per-harness with server-side auth or OAuth flow |
| Rename a server in the manifest and expect old target entries to migrate | The engine adds the new name; old name may linger as orphan if canonical form differs | Drop both old and new into the manifest temporarily, sync, then remove old - or manually delete from target |

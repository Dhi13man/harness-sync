# MCP Sources

`mcp-servers.json` is the only cross-harness source for portable MCP server definitions and references to machine-scoped secrets. `harness_sync.py` projects its entries into each MCP-capable harness while preserving that target's unowned local entries. It never promotes a connector from one harness into another. Pi core has no native MCP surface, so it is intentionally excluded.

The manifest wins on canonical-name conflicts. Existing target entries remain local when the ownership sidecar does not identify them as projected entries.

## Schema

```json
{
  "mcpServers": {
    "stdio-example": {
      "command": "npx",
      "args": ["-y", "@scope/package"],
      "type": "stdio",
      "env": { "MCP_API_KEY": "${MCP_API_KEY}" }
    },
    "http-example": {
      "type": "http",
      "url": "https://example.test/mcp",
      "headers": { "Authorization": "Bearer ${MCP_TOKEN}" }
    }
  }
}
```

| Field | Required | Notes |
| ----- | -------- | ----- |
| `command` | Yes for stdio | Executable such as `npx`, `uvx`, or `python` |
| `args` | Optional | Array of CLI arguments; credentials are not portable here |
| `type` | Optional | `stdio` (default) or `http` |
| `url` | Required for HTTP | Full MCP endpoint without embedded credentials |
| `env` | Optional | Per-server environment variables; every value uses the same-name `${VAR}` reference |
| `headers` | Optional | HTTP headers; values use `${VAR}`, or `Bearer ${VAR}` for `Authorization` |

Extra top-level keys such as `$schema` and `_comment` are ignored.

## Portable Authentication References

Put each API key in the machine's environment or secret manager once and commit only an exact `${VAR}` reference. Claude, Gemini, and Oh My Pi consume that form directly. The sync engine translates it to Cursor's `${env:VAR}` syntax and Codex's native `env_vars`, `env_http_headers`, or `bearer_token_env_var` fields.

Only exact references are portable. For `env`, the target key must equal the referenced variable because Codex supports same-name inheritance, not arbitrary environment aliases. `${VAR:-default}`, partial references, references in `args` or URLs, and harness-specific fields such as `auth`/`oauth` are rejected rather than translated lossily. Portable server entries use only the schema above; client-specific options remain local.

OAuth refresh state remains owned by each client. Sync shares the stable server name and URL so each harness can reuse its own native login, but it never copies credential files, OAuth caches, or Oh My Pi's `agent.db`. One cross-client OAuth login requires an external credential broker supported by every client; file projection cannot safely provide it.

## Source Precedence

The repo manifest is authoritative by default. An explicit environment override is useful for hermetic tests or a deliberately separate canonical file:

1. `HARNESS_MCP_MANIFEST`
2. `CLAUDE_MCP_MANIFEST`
3. repo `mcp-servers.json`

The engine projects manifest entries into the Claude connector registry resolved from `CLAUDE_CONFIG_JSON`, then `CLAUDE_CONFIG_PATH`, then `~/.claude.json`. Claude-local connectors stay in Claude; add a portable definition to the repo manifest when it should reach other harnesses.

## Drift Tolerance

Names are compared using `name.replace("_", "-").lower()`. This lets one manifest spelling replace common pre-existing drift:

| Existing target | Manifest entry | Result |
| --------------- | -------------- | ------ |
| `sequential_thinking` | `sequential-thinking` | Manifest spelling wins |
| `Sequential-Thinking` | `sequential-thinking` | Manifest spelling wins |
| `sequentialthinking` | `sequential-thinking` | Both remain; canonical forms differ |

Canonicalization does not remove punctuation other than converting `_` to `-`.

## Secret Gate

The engine blocks shared-manifest projection when either of two checks finds a credential:

- a structural check rejects every literal `env`/`headers` value, `auth`/`oauth` objects, URL userinfo or auth query parameters, and credential flags or header fragments in `args`;
- a value-shape scan catches common Context7, OpenAI/Anthropic, GitHub, GitLab, Hugging Face, Slack, AWS, bearer/basic, and JWT credentials under unusual field names.

All Claude-local connectors remain local, whether or not they appear credential-free. Codex, Cursor, Gemini, and Oh My Pi receive only repo-manifest definitions plus their own preserved unowned entries. Pi receives no MCP projection.

## Projection and Ownership

| Harness | Target | Native secret reference | Preserves |
| ------- | ------ | ----------------------- | --------- |
| Claude | `~/.claude.json` `.mcpServers.*` | `${VAR}` | Unowned Claude connectors |
| Codex | managed block in `~/.codex/config.toml` | `env_vars`, `env_http_headers`, `bearer_token_env_var` | MCP tables outside the block with other canonical names |
| Cursor | `~/.cursor/mcp.json` `.mcpServers.*` | `${env:VAR}` | Unowned server entries and top-level fields |
| Gemini | `~/.gemini/settings.json` `.mcpServers.*` | `${VAR}` | Unowned server entries and top-level fields |
| Pi | not projected | not supported by core | Existing extension-managed state is untouched |
| Oh My Pi | active agent directory `mcp.json` | `${VAR}` | Unowned entries, enable/disable lists, per-server `auth`/`oauth`, and native auth storage |

Claude keeps a target-scoped sidecar beside the resolved registry (for example `~/.claude.json.harness-sync-managed-mcp.json`); Cursor, Gemini, and Oh My Pi keep `.harness-sync-managed-mcp.json` beside their harness-owned configuration. Each sidecar contains only the names the engine wrote for that exact target. Removing a manifest entry prunes it only when the sidecar proves ownership. A secret-free transaction journal repairs an interrupted config-plus-sidecar publication on the next non-dry run. Codex uses its managed marker block instead.

Do not run sync while Claude, Cursor, or Gemini is actively changing its MCP file. The engine publishes atomically and checks the exact input digest immediately before publication, but those clients expose no documented lock or filesystem compare-and-swap contract, so exclusion against a native writer is not guaranteed. Oh My Pi is the exception: sync joins OMP's native configuration lock.

## Supplying Credentials

For API keys, export the referenced variable through the machine's existing secret-loading mechanism, then run the sync. For OAuth, sync the definition and authenticate with each harness's native flow.

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Hand-edit a managed target entry | The next sync replaces it | Edit the canonical manifest |
| Treat a real `~/.claude/mcp-servers.json` as an implicit override | It creates a second source of truth | Use the repo manifest or an explicit override variable |
| Commit a credential value | Git history and every projected target receive it | Commit `${VAR}` and store the value outside the repo |
| Pass credentials in `args` | Process listings and logs may expose them | Reference an environment variable in `env` or `headers` |
| Put credentials in an HTTP URL | URLs leak through history and telemetry | Use native OAuth or an environment-backed header |
| Copy or symlink an OAuth cache or credential database | Formats, encryption, client IDs, and refresh ownership differ | Sync the definition; let each harness own login state |
| Sync while a native client is editing its MCP file | Most clients expose no cross-process config lock, so the last atomic writer can win | Close or idle the client during sync; OMP is coordinated through its native lock |

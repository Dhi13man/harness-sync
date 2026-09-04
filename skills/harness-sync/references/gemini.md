# Gemini - Translation Projection

Gemini CLI (`~/.gemini/`) uses TOML files with `description` and `prompt` fields, not markdown with frontmatter. Every shared repo artifact must be parsed and regenerated. There are no symlinks in the Gemini target - everything is derived.

## Layout Mapping

| Config repo source | Gemini target | Strategy | Notes |
| ------------- | ------------- | -------- | ----- |
| `commands/<name>.md` | `~/.gemini/commands/<name>.toml` | `translate_commands_to_toml` | Preserves frontmatter `description`; body becomes `prompt` with `$ARGUMENTS` to `{{args}}` |
| `skills/<name>/SKILL.md` | `~/.gemini/commands/skill-<name>.toml` | `translate_skills_to_toml` | Gemini has no native skill system; each skill becomes an invocable slash-command |
| `skills/` walk | `~/.gemini/skills/index.json` | `generate_skill_index` | Manifest Gemini reads to enumerate skills |
| `agents/*` | - | not synced | Gemini has no agent delegation primitive comparable to Claude's Task tool |
| `CLAUDE.md` | `~/.gemini/GEMINI.md` | **user-managed** | User currently symlinks GEMINI.md to `~/Desktop/obsidian-docs/AI/AGENTS.md` - sync engine leaves this alone |
| Claude runtime memory | - | not synced | Gemini uses the `@modelcontextprotocol/server-memory` MCP server, not a directory |
| MCP manifest | `~/.gemini/settings.json` `.mcpServers.*` | `strategy_mcp_to_gemini` | Projects only the canonical repo manifest. Gemini-only servers (e.g. `context7`) are preserved. See [mcp-manifest.md](mcp-manifest.md). |

## Format Conversion Rules

### Markdown frontmatter → TOML description

```markdown
---
description: "Review code for quality issues"
argument-hint: "[files]"
allowed-tools: ["Read", "Edit"]
---
```

becomes:

```toml
description = "Review code for quality issues"
```

Only `description` survives. Other frontmatter keys (`argument-hint`, `allowed-tools`) are Claude-specific.

### Markdown body → TOML prompt

The entire markdown body after frontmatter becomes the TOML `prompt` field as a multiline string (`"""..."""`). Special substitutions:

| Source token | Rewritten to | Reason |
| ------------ | ------------ | ------ |
| `$ARGUMENTS` | `{{args}}` | Gemini's argument placeholder syntax |

### TOML escaping

| Character | Handling |
| --------- | -------- |
| `"` (double quote in description) | Replaced with `'` - avoids escape hell in single-line TOML strings |
| `\` (backslash) | Escaped to `\\` |
| `"""` (triple quote in body) | Escaped to `\"\"\"` inside multiline strings |

The escaping is lossy (double quotes become single quotes in descriptions) but Gemini doesn't need exact round-tripping - it just needs parseable TOML.

## Skill → Command Expansion

Gemini has no `/load-skill` primitive. To invoke a shared skill from Gemini, we generate a slash-command per skill:

```text
skills/accessibility/SKILL.md
 -> ~/.gemini/commands/skill-accessibility.toml

description = <accessibility SKILL.md frontmatter description>
prompt = """
# Skill: accessibility

<full SKILL.md body>

## Available References
Read these via `read_file` for depth:
- ~/.gemini/skills/accessibility/references/foo.md
- ~/.gemini/skills/accessibility/references/bar.md

<!-- The 'accessibility' skill has been loaded into context. -->
"""
```

The user invokes `/skill-accessibility` in Gemini; the body of the SKILL.md becomes the prompt, and the agent knows to `read_file` the references on demand.

## The `~/.gemini/skills/` Mount

Reference paths in generated TOMLs point to `~/.gemini/skills/<skill-name>/references/...`. This path must resolve - typically via a symlink the user maintains separately: `~/.gemini/skills -> $HARNESS_CONFIG_REPO/skills`. The sync engine does NOT create this symlink; it's a user-owned piece of setup.

If the user moves the mount, update the `skill_mount` option in the gemini harness spec:

```python
{"strategy": strategy_translate_skills_to_toml,
 "source": CONFIG_HOME / "skills",
 "target_rel": "commands",
 "opts": {"prefix": "skill-", "skill_mount": "~/new-mount-point"}}
```

## Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | **Always** regenerate TOMLs from repo markdown; never hand-edit a `~/.gemini/commands/*.toml` | Edits lost on next sync - translation is deterministic and lossy |
| 2 | **Always** prune stale TOMLs when source commands/skills are renamed or deleted | Otherwise Gemini sees zombie commands referencing nonexistent references |
| 3 | **Never** include Claude-only frontmatter keys in the TOML | `argument-hint`, `allowed-tools` aren't Gemini-meaningful; including them is noise |
| 4 | **Prefer** `_write_if_changed` over bulk wipe-and-rewrite | Bulk rewrite was the old `import_commands.py` behavior; it churns mtimes and breaks Syncthing efficiency |
| 5 | **Always** use the `skill-` prefix for translated skills, never collide with command names | Distinguishes skill-commands from regular commands; lets `translate_commands` safely prune without touching skill outputs |
| 6 | **Never** sync the agents directory to Gemini | Agents require a Task-tool-like delegation primitive Gemini lacks |
| 7 | **Default to** leaving GEMINI.md alone | User has it symlinked elsewhere (Obsidian); forcing a target here breaks their workflow |

## Known Gemini Quirks

| Quirk | Impact | Mitigation |
| ----- | ------ | ---------- |
| 49-file command dir predating sync engine came from an older `import_commands.py` | 9 skills and 5 commands went missing over time | First run of `harness_sync.py` backfills all of them |
| `sde-commit.toml` lived past the Claude `sde-commit` → `sde-git` rename | Zombie command invokable in Gemini | Prune logic catches this |
| No native memory concept | Memory propagation is N/A | Gemini uses MCP memory server separately |
| Double quotes in descriptions get downgraded to single quotes | Minor semantic change | Acceptable trade-off for escape-free TOML |

## Deprecation of `~/.gemini/import_commands.py`

The old 200-line `import_commands.py` wiped and rewrote `~/.gemini/commands/` every run, had no dry-run, no idempotence, no detection, and lived at a harness-specific path. It is superseded by `harness_sync.py` which:

- Does not wipe (diffs and writes only deltas)
- Supports `--dry-run`
- Is idempotent (second run reports zero changes)
- Discovers harnesses instead of hardcoding them

Keep the old file only as a redirect shim (see [scripts/harness_sync.py](../../scripts/harness_sync.py) header) or delete it outright once confident.

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Edit a generated `~/.gemini/commands/*.toml` | Overwritten next sync | Edit the repo source `.md`; run sync |
| Skip pruning on rename | Stale commands linger in Gemini | Prune matches naming convention (`skill-` prefix managed separately from plain commands) |
| Serialize skill reference files into the TOML body directly | TOML bloat; 10x the prompt size; hits context limits | List paths only; let Gemini's `read_file` pull on demand |
| Generate TOMLs for hidden skill dirs (`.system/` style) | Creates commands for non-user-facing internals | Skip `startswith(".")` in the iterator |

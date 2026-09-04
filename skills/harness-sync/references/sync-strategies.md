# Sync Strategies

The pluggable strategies `harness_sync.py` uses to project config repo artifacts into harness homes. Each strategy is a function `(source, target, report, harness, dry_run, **opts) -> None`. Adding a new strategy is additive - never fork an existing one.

## Strategy Selection Matrix

| Situation | Strategy | Why |
| --------- | -------- | --- |
| Target harness reads the repo's native markdown directly | `symlink` | Zero translation, zero drift, editing through the link edits the repo file |
| Target dir must coexist with harness-managed entries (e.g., Codex `.system/`) | `symlink_children` with `preserve=[...]` | Can't blanket-symlink the whole dir; must link each child and leave preserved names alone |
| Target harness uses a different file format (TOML, JSON, YAML) | `translate_*` | Must parse and regenerate; losing is acceptable, conflicts are not |
| Target harness needs a generated index/manifest from the source | `generate_*` | Derived artifact; regenerate deterministically every sync |
| Target JSON file mixes projected and harness-owned entries | owned JSON merge + sidecar | Update only recorded names while preserving native top-level state and unowned entries |

## Core Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | **Always** make strategies idempotent - a second run must report zero changes | Non-idempotent sync causes oscillation; two cooperating hooks fight each other |
| 2 | **Always** skip writes when content is byte-identical (`_write_if_changed`) | Churning mtimes triggers Syncthing, breaks incremental builds, wastes I/O |
| 3 | **Never** clobber a real file with a symlink | User may have edited it; refuse and report error, let them resolve |
| 4 | **Prefer** `symlink` over `translate` when format matches | Symlinks auto-propagate future edits; translations go stale on every Claude change |
| 5 | **Always** prune stale derived artifacts (old translations whose source vanished) | Otherwise renames leave zombies; `sde-commit.md` → `sde-git.md` must delete `sde-commit.toml` |
| 6 | **Never** prune `preserve`-list entries, non-symlinks, or links outside the matching source child | User-owned entries must be sacred; only our own dangling links are safe to remove |
| 7 | **Always** return a `Change` record per action so the report is actionable | "Done" without a log is unfalsifiable; every action must be inspectable |

## Strategy Contract

Every strategy MUST:

| Requirement | Implementation | Failure mode without it |
| ----------- | -------------- | ----------------------- |
| Accept `dry_run` and honor it | Guard every write with `if not dry_run` | Dry-run writes files → breaks user trust |
| Emit a `Change` with `ok=False` on error | Catch, report, continue | One bad artifact aborts the whole sync |
| Create target parent dirs before writing | `parent.mkdir(parents=True, exist_ok=True)` | First-run failure on fresh harness |
| Check source existence | Short-circuit with error Change if missing | Cryptic IOError with no context |
| Be pure (no side effects beyond target writes) | No global state, no env mutation | Strategy reuse becomes unsafe |

## Built-in Strategies

### `symlink(source, target)`

Single file or dir symlink. Use when source and target formats match 1:1.

| Case | Behavior |
| ---- | -------- |
| target is correct symlink | skip |
| target is wrong symlink | retarget (unlink + relink) |
| target is real file/dir | error - refuse to clobber |
| target missing | create |

### `symlink_children(source, target, preserve=[...])`

Per-child symlinks into a target dir. Use when the target dir needs to retain non-Claude entries (Codex's `.system/`).

- Skips hidden children (names starting with `.`)
- Requires a real target directory; refuses to follow a root link
- Preserves names in `preserve` list (never touched, never pruned)
- Prunes a vanished source child only when the existing link still points to that matching source path

### `translate_commands_to_toml(source, target)`

Parse repo `.md` commands, write Gemini `.toml`. Preserves `skill-*` prefix entries (owned by the skill translator).

### `translate_skills_to_toml(source, target, prefix="skill-")`

Parse each repo skill's `SKILL.md`, write `skill-<name>.toml` that includes both the hub prompt and a list of reference file paths for the Gemini agent to `read_file` on demand.

### `generate_skill_index(source, target)`

Walk Claude's skills dir, write a JSON manifest at target with `{name, description, path}` entries.

### MCP JSON merge

Claude, Cursor, Gemini, and Oh My Pi keep real JSON config files. The MCP merger records the server names it owns in `.harness-sync-managed-mcp.json`, replaces canonical-name matches from the shared source, prunes only formerly owned names, and preserves all other entries and top-level fields. Each file uses same-directory atomic replace; a digest-only transaction journal completes or rolls back an interrupted config-plus-sidecar publication before the next sync. Oh My Pi additionally joins the client's native MCP file lock, creates `mcp.json` with mode `0600`, and never reads or writes its `agent.db` authentication store.

## When to Add a New Strategy

Add a strategy when **none** of the above match. Do NOT add one if you can express the need as new `opts` on an existing strategy.

| Need | Existing strategy + opts? | New strategy? |
| ---- | ------------------------- | ------------- |
| Symlink only top-level dirs, skip files | `symlink_children(..., include_files=False)` | No - use opts |
| Copy instead of symlink (for non-symlink-friendly filesystem) | None | Yes - `copy(source, target)` |
| Translate MD to YAML for a new harness | None | Yes - `translate_md_to_yaml` |
| Generate a different manifest format | None | Yes - `generate_manifest_yaml` |

## Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| Write target files unconditionally | Breaks `_write_if_changed` invariant; churns disk | Diff before write; return "skip" on match |
| Catch all exceptions and continue silently | Hides real problems; sync looks green but isn't | Emit `Change(ok=False)`; let caller decide |
| Mix translation and symlinking in one strategy | Unclear contract; hard to test | One strategy per mechanism; compose in the spec |
| Assume target dir exists | First-run on fresh harness fails | `parent.mkdir(parents=True, exist_ok=True)` is cheap and safe |
| Prune aggressively (delete any stranger in target) | Deletes user's legitimate files | Only prune artifacts you know you created - by filename pattern or link-ness |

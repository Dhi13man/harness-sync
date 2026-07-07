# Harness Detection

How `harness_sync.py` decides whether the config repo and each harness are usable on this machine. Each spec's `detect(home)` lambda returns a bool; the engine skips target harnesses that fail detection so a clean `~/.foo` dir doesn't pull in stale artifacts.

## Detection Stances

| # | Stance | Rationale |
| - | ------ | --------- |
| 1 | **Always** require a source signature for `config_repo` | The source must contain shared artifacts (`CLAUDE.md`, `skills/`, `hooks/`); an arbitrary directory is not a config repo |
| 2 | **Require** a harness-specific signature file for third-party harnesses, never just the home directory | An empty `~/.codex/` can exist after uninstall; only files the harness itself creates prove it's live |
| 3 | **Allow** Claude's home directory as its projection marker | The bootstrap runs from Claude and is responsible for restoring Claude symlinks; requiring the symlinks first would make repair circular |
| 4 | **Prefer** native config files over sync-engine-generated symlinks as signatures | A signature that we created isn't evidence - it's circular |
| 5 | **Never** detect third-party harnesses on the presence of `AGENTS.md`/`SKILL.md` alone | These can be our own symlinks; detection must predate our sync |
| 6 | **Always** require `home.exists()` AND `detect(home)` - both, not either | A dir without the signature is either new or corrupt; refuse to write into it |
| 7 | **Default to** OR-ing multiple markers if any one is authoritative | Harnesses evolve; `config.toml` may move, but the existence of either legacy or current marker still proves installation |

## Signature File Catalog

| Harness | Home | Primary signature | Fallback signature | Rationale |
| ------- | ---- | ----------------- | ------------------ | --------- |
| config_repo | `$HARNESS_CONFIG_REPO` or `~/harness-config` | `CLAUDE.md` + `skills/` + `hooks/` | Script location | Source of truth; all must exist |
| claude | `~/.claude` | directory exists | - | Projection target repaired by bootstrap; runtime state lives here |
| codex | `~/.codex` | `config.toml` | `AGENTS.md` (pre-sync only) | `config.toml` is Codex-owned; AGENTS.md is a fallback because older Codex installs predate config.toml |
| cursor | `~/.cursor` | `argv.json` | `skills-cursor/` | `argv.json` and built-in `skills-cursor/` are Cursor-owned; generated `skills/` is not a signature |
| gemini | `~/.gemini` | `settings.json` | - | Gemini always writes settings.json on first run |

When adding a new third-party harness, find a file that the harness itself writes on first launch. Never use a file you control.

## Detection Anti-Patterns

| Don't | Why | Instead |
| ----- | --- | ------- |
| `detect = lambda h: h.exists()` | Empty dirs pass; pulls in uninstalled harnesses | Require a specific file that the harness owns |
| `detect = lambda h: (h / "AGENTS.md").exists()` post-sync | AGENTS.md may now be our symlink; circular evidence | Keep the pre-sync fallback, but prefer the native config file |
| Trust one-and-done detection per session | Harness can be installed mid-session | Re-detect on every sync call (cheap - just `Path.exists()`) |
| Detect by process/PID | Harness doesn't need to be running to be installed | File-based detection; no runtime coupling |
| Treat `~/.claude` as the canonical source | Prevents repair when Claude's symlinks drift or the repo is later renamed | Validate the config repo separately, then project into Claude like any other harness |

## Detection Order

Declared by list order in `_harness_specs()`. `config_repo` is first so a missing source fails fast (exit 2) before any projection runs. Other harnesses are independent - ordering only matters for report readability.

## What Detection Is Not

Detection answers "is this harness installed?" It does NOT answer:

- **Is it configured correctly?** - that's each strategy's job (e.g., symlink refuses to clobber a real file)
- **Is it up to date?** - that's the change count in the report
- **Should we sync it today?** - that's the `--only` flag's job

Keep detection cheap and single-purpose.

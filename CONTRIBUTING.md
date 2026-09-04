# Contributing to harness-sync

Thanks for helping keep AI coding harnesses in parity. The most valuable contributions are **new harnesses**, **strategy fixes**, and **detection improvements**. One rule overrides everything: **the sync must stay idempotent** — a second run against an unchanged repo reports `skip` only.

## Project layout

```text
skills/harness-sync/
  scripts/harness_sync.py     # the engine (stdlib-only, Python 3.11+)
  references/*.md             # per-harness sync knowledge — read before changing behavior
  SKILL.md                   # design overview + reference routing
commands/meta-agent-sync.md  # the /meta-agent-sync workflow
hooks/                       # SessionStart wiring
examples/minimal-config/     # a runnable source-of-truth fixture (also the test target)
tests/test_harness_sync.py   # focused stdlib unit and boundary tests
tests/smoke_test.sh          # hermetic detect + apply + idempotence check
```

## Setup

```bash
git clone https://github.com/Dhi13man/harness-sync && cd harness-sync
python3 --version            # need 3.11+
python3 -m py_compile skills/harness-sync/scripts/harness_sync.py
python3 -m unittest discover -s tests -p 'test_*.py'
./tests/smoke_test.sh        # should pass green
```

No dependencies to install — the engine is stdlib-only. `ruff` is optional for linting.

## Adding a harness

Everything a harness needs is one dict in `_harness_specs()`. Do **not** write a per-harness script.

1. **Read first**: [`references/detection.md`](skills/harness-sync/references/detection.md) and [`references/sync-strategies.md`](skills/harness-sync/references/sync-strategies.md). Then the closest existing harness (`codex.md` for symlink+translate, `gemini.md` for pure translate, `pi.md` for a deliberately minimal core, `oh-my-pi.md` for profile-aware native projection).
2. **Detection**: pick a signature the *harness itself* creates (a native config file), never an artifact this engine generates — that's circular evidence.
3. **Add the spec dict**: `name`, `home`, `role`, `detect`, and `artifacts` (each = `strategy` + `source` + `target_rel` + optional `opts`).
4. **Reuse strategies** where you can (`strategy_symlink`, `strategy_symlink_children`). Only add a new `strategy_translate_*` if the harness needs a format the engine can't already emit — and make it skip byte-identical writes via `_write_if_changed`.
5. **Document it**: add `references/<harness>.md` (its quirks, preservation rules, gotchas) and a row in `SKILL.md`'s routing table and the README's harness table.
6. **Test idempotence** (below).

## The idempotence rule

Every strategy must be a no-op when the target already matches the source. Concretely:

- Return/record `skip` when the symlink already points correctly or content is byte-identical.
- Never `unlink`+recreate a link that's already correct.
- Sort any generated collections deterministically so output doesn't oscillate between runs.

`tests/smoke_test.sh` runs the engine twice against a temp harness home and **fails if the second run reports any non-`skip` action.** If your change can't pass it, it isn't done.

## Testing your change

```bash
./tests/smoke_test.sh                       # detect + apply + verify idempotence
python3 -m unittest discover -s tests -p 'test_*.py'
# manual, against your own repo, safely:
HARNESS_CONFIG_REPO=~/harness-config \
  python3 skills/harness-sync/scripts/harness_sync.py --dry-run -v
```

CI (`.github/workflows/ci.yml`) runs `py_compile`, the unit suite, and the smoke test on Python 3.11–3.13, plus the same checks on Windows. Keep them green.

## Code style

- Match the existing file: type hints, small pure helpers, `Change`/`Report` for reporting, comments that explain *why*.
- Stdlib only. Adding a third-party dependency needs a strong justification in the PR.
- Never overwrite a real (non-symlink) target — report an `error` and let the user decide.

## Pull requests

- One logical change per PR. Include what you tested and the idempotence result.
- New harness PRs: include the reference doc and a note on how you verified detection + a clean second run.
- Security-relevant changes (secret detection, path handling) get extra review — call them out.

By contributing you agree your work is licensed under the [MIT License](LICENSE).

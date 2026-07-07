#!/usr/bin/env python3
# ruff: noqa: T201
"""
Unified harness sync for the central config repo plus ~/.claude, ~/.codex,
~/.cursor, ~/.gemini, and future harnesses.

Treats the cloned config repo as the source of truth and projects agents /
commands / skills / guidance / hooks into every detected harness using the
strategy that harness needs (symlink, per-child symlink, or translation).

Declarative. Idempotent. Extensible — adding a new harness is a dict entry.

Usage:
    harness_sync.py              # sync all detected harnesses
    harness_sync.py -v           # verbose (trace every action)
    harness_sync.py --dry-run    # report planned changes, write nothing
    harness_sync.py --only codex # only sync specific harnesses (comma-sep)
    harness_sync.py --list       # show detected harnesses and exit

Exit codes:
    0  success (all detected harnesses synced)
    1  partial failure (some artifacts failed; others ok)
    2  fatal (config repo missing / unrecoverable)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


def _reexec_with_modern_python() -> None:
    """Rerun under Python 3.11+ so stdlib tomllib is available."""
    override = os.environ.get("HARNESS_SYNC_PYTHON")
    candidates = [override] if override else []
    candidates.extend(["python3.13", "python3.12", "python3.11"])

    for candidate in candidates:
        if not candidate or not shutil.which(candidate):
            continue
        version = subprocess.run(
            [candidate, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            check=False,
            capture_output=True,
            text=True,
        )
        if version.returncode == 0 and tuple(map(int, version.stdout.strip().split("."))) >= (3, 11):
            os.execvp(candidate, [candidate, __file__, *sys.argv[1:]])

    pyenv = shutil.which("pyenv")
    if pyenv:
        versions = subprocess.run(
            [pyenv, "versions", "--bare"],
            check=False,
            capture_output=True,
            text=True,
        )
        if versions.returncode == 0:
            for version in sorted(versions.stdout.splitlines(), reverse=True):
                parts = version.split(".")
                if len(parts) < 2 or not all(part.isdigit() for part in parts[:2]):
                    continue
                if (int(parts[0]), int(parts[1])) < (3, 11):
                    continue
                env = os.environ.copy()
                env["PYENV_VERSION"] = version
                os.execvpe(pyenv, [pyenv, "exec", "python", __file__, *sys.argv[1:]], env)


try:
    import tomllib
except ModuleNotFoundError:
    _reexec_with_modern_python()
    raise SystemExit(
        "harness_sync.py requires Python 3.11+ or HARNESS_SYNC_PYTHON pointing to it"
    )


# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

def _expand_path_env(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def _script_config_repo() -> Path:
    # scripts/harness_sync.py -> harness-sync -> skills -> repo root
    return Path(__file__).resolve().parents[3]


def _looks_like_config_repo(p: Path) -> bool:
    """A real source-of-truth checkout, not this tool's own plugin dir (which also
    has skills/ + hooks/). Require the guidance file the engine projects, matching
    the `config_repo` detection spec and sync()'s precondition."""
    return (
        (p / "CLAUDE.md").exists()
        and (p / "skills").is_dir()
        and (p / "hooks").is_dir()
    )


def _config_home() -> Path:
    configured = _expand_path_env("HARNESS_CONFIG_REPO")
    if configured is not None:
        return configured

    # Auto-detect only when the script sits inside a real config repo. When this
    # tool is installed standalone (as a plugin), fall through to the default so
    # we never mistake the plugin's own skills/+hooks/ for the source of truth.
    script_repo = _script_config_repo()
    if _looks_like_config_repo(script_repo):
        return script_repo

    # Default source-of-truth location. Point HARNESS_CONFIG_REPO elsewhere to
    # override; if it is absent, sync() emits a clear FATAL naming this path.
    return Path.home() / "harness-config"


def _mcp_manifest_path() -> Path:
    for env_name in ("HARNESS_MCP_MANIFEST", "CLAUDE_MCP_MANIFEST"):
        configured = _expand_path_env(env_name)
        if configured is not None:
            return configured

    # MCP often contains machine-local connector endpoints. Preserve an
    # existing local override unless the user explicitly opts into repo source.
    local_manifest = CLAUDE_HOME / "mcp-servers.json"
    if local_manifest.exists() and not local_manifest.is_symlink():
        return local_manifest

    repo_manifest = CONFIG_HOME / "mcp-servers.json"
    return repo_manifest if repo_manifest.exists() else local_manifest


def _claude_config_json_path() -> Path:
    configured = _expand_path_env("CLAUDE_CONFIG_JSON")
    if configured is not None:
        return configured
    return Path(os.environ.get("CLAUDE_CONFIG_PATH", str(Path.home() / ".claude.json"))).expanduser()


CONFIG_HOME = _config_home()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude"))).expanduser()
MCP_MANIFEST_PATH = _mcp_manifest_path()
CLAUDE_CONFIG_JSON_PATH = _claude_config_json_path()

CLAUDE_LINK_ENTRIES = (
    "AGENTS.md",
    "CLAUDE.md",
    "agents",
    "commands",
    "hooks",
    "skills",
    "settings.json",
    "statusline-command.sh",
)

# Managed-block markers for surgical edits inside Codex's config.toml.
# Anything between BEGIN and END is authored by the sync engine. Anything outside
# is user-owned and preserved verbatim.
_CODEX_MCP_BEGIN = "# >>> harness-sync: mcp-servers (managed, do not edit manually) >>>"
_CODEX_MCP_END = "# <<< harness-sync: mcp-servers <<<"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

@dataclass
class Change:
    harness: str
    action: str     # "symlink", "retarget", "translate", "prune", "skip", "error"
    target: str
    detail: str = ""
    ok: bool = True


@dataclass
class Report:
    changes: list[Change] = field(default_factory=list)
    harnesses_detected: list[str] = field(default_factory=list)
    harnesses_skipped: list[str] = field(default_factory=list)

    def add(self, c: Change) -> None:
        self.changes.append(c)

    def errors(self) -> list[Change]:
        return [c for c in self.changes if not c.ok]

    def by_action(self) -> dict[str, int]:
        d: dict[str, int] = {}
        for c in self.changes:
            d[c.action] = d.get(c.action, 0) + 1
        return d


# ---------------------------------------------------------------------------
# Strategy primitives
# ---------------------------------------------------------------------------

def _same_real_path(source: Path, target: Path) -> bool:
    try:
        return source.resolve() == target.resolve()
    except FileNotFoundError:
        return False


def _paths_match(source: Path, target: Path) -> bool:
    src_link, tgt_link = source.is_symlink(), target.is_symlink()
    if src_link and tgt_link:
        return os.readlink(source) == os.readlink(target)
    if src_link or tgt_link:
        # Exactly one side is a symlink (e.g. the repo's CLAUDE.md -> AGENTS.md
        # against a real ~/.claude/CLAUDE.md copy from the old layout). Match on
        # resolved file bytes so adopt_matching can upgrade an identical copy to a
        # symlink instead of erroring with "refusing to overwrite real file/dir".
        return (
            source.is_file()
            and target.is_file()
            and source.read_bytes() == target.read_bytes()
        )
    if source.is_file() and target.is_file():
        return source.read_bytes() == target.read_bytes()
    if not (source.is_dir() and target.is_dir()):
        return False

    source_entries = {p.relative_to(source) for p in source.rglob("*")}
    target_entries = {p.relative_to(target) for p in target.rglob("*")}
    if source_entries != target_entries:
        return False
    return all(_paths_match(source / rel, target / rel) for rel in source_entries)


# --- Windows / foreign-reparse-point safety --------------------------------
# A symlink created from WSL is stored on NTFS as an LX_SYMLINK reparse point.
# Native-Windows Python does NOT see it as a symlink (`is_symlink()` is False)
# and cannot follow it (`exists()`/`stat()` raise OSError -> WinError 1920).
# These helpers let the engine treat such a path as a stale, replaceable link
# artifact instead of crashing. On POSIX `st_file_attributes` is absent, so
# `_is_foreign_link` is always False and behavior is unchanged.

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_foreign_link(path: Path) -> bool:
    """True for a reparse point Python won't treat as a native symlink
    (e.g. a WSL LX_SYMLINK seen from Windows). Such a path is link-like:
    removing it deletes only the link, never real data."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not (getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT):
        return False
    return not path.is_symlink()


def _safe_exists(path: Path) -> bool:
    """`Path.exists()` that returns False instead of raising on an
    unfollowable reparse point."""
    try:
        return path.exists()
    except OSError:
        return False


def _neutralize_foreign_link(path: Path, dry_run: bool) -> bool:
    """Remove a foreign reparse point so a real file/dir or native symlink can
    take its place. Returns True if a link was (or would be) removed."""
    if not _is_foreign_link(path):
        return False
    if not dry_run:
        path.unlink()
    return True


def _strip_ext_prefix(p: str) -> str:
    """Drop the Windows extended-length prefix that `os.readlink` prepends
    (`\\\\?\\C:\\...` or `\\\\?\\UNC\\server\\share`). No-op on POSIX paths."""
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


def _norm_link(p: str) -> str:
    """Normalize a link target for comparison: strip the extended-length
    prefix, collapse separators, and case-fold on case-insensitive platforms."""
    return os.path.normcase(os.path.normpath(_strip_ext_prefix(p)))


def _link_points_to(current: str, source: Path) -> bool:
    """Whether a symlink's stored target refers to `source`. Tolerant of the
    `\\\\?\\` prefix and separator/case differences `os.readlink` introduces on
    Windows; the exact-string fast path preserves original POSIX behavior."""
    target = str(source)
    return current == target or _norm_link(current) == _norm_link(target)


def _replace_with_symlink(source: Path, link: Path) -> None:
    if link.is_symlink() or _is_foreign_link(link):
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    else:
        link.unlink()
    link.symlink_to(source)


def _safe_symlink(
    source: Path,
    link: Path,
    dry_run: bool,
    adopt_matching: bool = False,
) -> tuple[str, str]:
    """Create or repair link -> source. Refuses to clobber a real file/dir.
    Returns (action, detail)."""
    if link.is_symlink():
        current = os.readlink(link)
        if _link_points_to(current, source):
            return ("skip", "already correct")
        if not dry_run:
            link.unlink()
            link.symlink_to(source)
        return ("retarget", f"{current} -> {source}")
    if _is_foreign_link(link):
        # WSL LX_SYMLINK (or other foreign reparse point) seen from Windows.
        # It can never equal the symlink we want and shadows real data only as
        # a link, so replace it rather than crash on the exists() check below.
        if not dry_run:
            link.unlink()
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(source)
        return ("retarget", f"foreign reparse point -> {source}")
    if _safe_exists(link):
        if _same_real_path(source, link):
            return ("skip", "source and target are identical")
        if adopt_matching and _paths_match(source, link):
            if not dry_run:
                _replace_with_symlink(source, link)
            return ("adopt", f"replaced matching real target with symlink -> {source}")
        return ("error", f"refusing to overwrite real file/dir: {link}")
    if not dry_run:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(source)
    return ("symlink", str(source))


def strategy_symlink(
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    adopt_matching: bool = False,
    **_: object,
) -> None:
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return
    action, detail = _safe_symlink(source, target, dry_run, adopt_matching=adopt_matching)
    report.add(Change(harness, action, str(target), detail, ok=(action != "error")))


def strategy_symlink_preserve_real(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Create/repair a symlink, but leave an existing real target alone."""
    if not source.exists():
        report.add(Change(harness, "skip", str(target), f"source missing: {source}"))
        return
    if _safe_exists(target) and not target.is_symlink() and not _is_foreign_link(target):
        report.add(Change(harness, "skip", str(target), "preserving real file/dir"))
        return
    action, detail = _safe_symlink(source, target, dry_run)
    report.add(Change(harness, action, str(target), detail, ok=(action != "error")))


def strategy_symlink_children(
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    preserve: Iterable[str] = (),
    skip_hidden: bool = True,
    include_files: bool = True,
    **_: object,
) -> None:
    """Per-child symlinks. Used when the target dir must retain some native entries
    (e.g. Codex's skills/.system). `preserve` = names to leave untouched."""
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return

    preserve_set = set(preserve)
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)

    # Link every eligible child.
    source_names: set[str] = set()
    for child in sorted(source.iterdir()):
        if skip_hidden and child.name.startswith("."):
            continue
        if not include_files and child.is_file():
            continue
        source_names.add(child.name)
        link = target / child.name
        action, detail = _safe_symlink(child, link, dry_run)
        report.add(Change(harness, action, str(link), detail, ok=(action != "error")))

    # Prune stale symlinks (source removed) without touching preserved or real entries.
    if not target.exists():
        return
    for existing in target.iterdir():
        if existing.name in preserve_set:
            continue
        if not existing.is_symlink():
            continue
        if existing.name in source_names:
            continue
        if not dry_run:
            existing.unlink()
        report.add(Change(harness, "prune", str(existing), "source removed"))


# ---- markdown -> TOML translation (Gemini) --------------------------------

_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


def _parse_md(path: Path) -> tuple[dict[str, str], str]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return {}, ""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("'\"")
    return meta, m.group(2)


def _toml_str(v: str) -> str:
    if not v:
        return '""'
    cleaned = v.replace('"', "'").replace("\\", "\\\\")
    return f'"{cleaned}"'


def _toml_multiline(v: str) -> str:
    if not v:
        return '""'
    escaped = v.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{escaped}\n"""'


def _write_if_changed(path: Path, content: str, dry_run: bool) -> bool:
    """Return True if content differs from what's on disk (wrote or would write)."""
    if _safe_exists(path) and not _is_foreign_link(path):
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass
    if not dry_run:
        # A broken foreign reparse point where the file (or its parent dir)
        # belongs must be removed before we can write through it.
        _neutralize_foreign_link(path, dry_run)
        _neutralize_foreign_link(path.parent, dry_run)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return True


def strategy_translate_commands_to_toml(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Convert config commands/*.md into target/*.toml. Removes .toml whose .md vanished."""
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return

    source_names: set[str] = set()
    for md in sorted(source.glob("*.md")):
        name = md.stem
        source_names.add(f"{name}.toml")
        meta, body = _parse_md(md)
        description = meta.get("description", f"Invoke the {name} command.")
        # Gemini uses {{args}}; Claude uses $ARGUMENTS. Translate.
        prompt = body.replace("$ARGUMENTS", "{{args}}")
        toml = f"description = {_toml_str(description)}\n\nprompt = {_toml_multiline(prompt)}\n"
        out = target / f"{name}.toml"
        changed = _write_if_changed(out, toml, dry_run)
        report.add(Change(
            harness, "translate" if changed else "skip",
            str(out), "gemini command toml" if changed else "already current",
        ))

    _prune_stale_tomls(target, source_names, prefix_keep="skill-", report=report,
                      harness=harness, dry_run=dry_run)


def strategy_translate_skills_to_toml(
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    prefix: str = "skill-",
    skill_mount: str = "~/.gemini/skills",
    **_: object,
) -> None:
    """Convert each config skills/<name>/SKILL.md into target/<prefix><name>.toml.

    Gemini has no native Skill tool, so each shared skill becomes a slash-command.
    """
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return

    mount = os.path.expanduser(skill_mount)
    produced: set[str] = set()
    for skill_dir in sorted(source.iterdir()):
        if not skill_dir.is_dir():
            continue
        if skill_dir.name.startswith("."):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        meta, body = _parse_md(skill_md)
        description = meta.get("description", f"Load the {skill_dir.name} skill.")

        # Enumerate reference files so the Gemini-side agent can read them.
        ref_lines: list[str] = []
        refs_dir = skill_dir / "references"
        if refs_dir.is_dir():
            for ref in sorted(refs_dir.rglob("*")):
                if ref.is_file() and ref.suffix in {".md", ".txt"}:
                    rel = ref.relative_to(skill_dir)
                    ref_lines.append(f"- {mount}/{skill_dir.name}/{rel}")

        prompt = f"# Skill: {skill_dir.name}\n\n{body}\n\n"
        if ref_lines:
            prompt += "## Available References\n"
            prompt += "Read these via `read_file` for depth:\n"
            prompt += "\n".join(ref_lines) + "\n\n"
        prompt += f"<!-- The '{skill_dir.name}' skill has been loaded into context. -->"

        out_name = f"{prefix}{skill_dir.name}.toml"
        produced.add(out_name)
        toml = f"description = {_toml_str(description)}\n\nprompt = {_toml_multiline(prompt)}\n"
        out = target / out_name
        changed = _write_if_changed(out, toml, dry_run)
        report.add(Change(
            harness, "translate" if changed else "skip",
            str(out), "gemini skill toml" if changed else "already current",
        ))

    # Prune skill-*.toml whose source skill disappeared.
    if target.exists():
        for existing in target.glob(f"{prefix}*.toml"):
            if existing.name in produced:
                continue
            if not dry_run:
                existing.unlink()
            report.add(Change(harness, "prune", str(existing), "skill source removed"))


# ---- Command -> skill wrappers -------------------------------------------


def _build_codex_skill_wrapper(name: str, meta: dict, body: str) -> str:
    """Render a Codex skill SKILL.md that wraps a shared command.

    The frontmatter description must encode trigger phrases — Codex matches
    user intent against this string to auto-load the skill. We append explicit
    keywords including the slash-command name so users typing `/sde-git` or
    asking to "run sde-git" both trigger the same wrapper.
    """
    description = meta.get("description", f"Wrapper for the {name} command.")
    arg_hint = meta.get("argument-hint", "")

    skill_desc = (
        f"{description} Trigger keywords: {name}, /{name}, run {name}, do {name}."
    )
    if len(skill_desc) > 1000:  # 1024 cap; leave headroom
        skill_desc = skill_desc[:997] + "..."

    # Codex doesn't do $ARGUMENTS interpolation. Replace with a documented
    # placeholder so the model knows where user-supplied args belong.
    body_norm = body.replace("$ARGUMENTS", "{user-supplied arguments}").rstrip() + "\n"

    return (
        "---\n"
        f"name: cmd-{name}\n"
        f'description: "{skill_desc}"\n'
        "---\n"
        "\n"
        f"# {name} (Codex skill wrapper for a shared command)\n"
        "\n"
        f"Source command: `{CONFIG_HOME / 'commands' / (name + '.md')}`. This wrapper exists because Codex 0.117+ removed `~/.codex/prompts/` discovery (see openai/codex#15941); skills are the working surface for projecting shared commands into current Codex.\n"
        "\n"
        f"**Argument hint**: `{arg_hint}`\n"
        "\n"
        f"When the user asks to run `{name}`, types `/{name}`, or otherwise invokes the workflow, execute the steps below.\n"
        "\n"
        "## Workflow\n"
        "\n"
        f"{body_norm}"
    )


def _strategy_command_to_skill(
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    builder: Callable[[str, dict, str], str],
    detail: str,
) -> None:
    """For each command file under source, generate target/cmd-<name>/SKILL.md."""
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return

    produced: set[str] = set()

    for md in sorted(source.glob("*.md")):
        name = md.stem
        produced.add(f"cmd-{name}")
        meta, body = _parse_md(md)
        skill_dir = target / f"cmd-{name}"
        skill_md_path = skill_dir / "SKILL.md"

        content = builder(name, meta, body)

        if not dry_run:
            skill_dir.mkdir(parents=True, exist_ok=True)
        changed = _write_if_changed(skill_md_path, content, dry_run)
        report.add(Change(
            harness, "translate" if changed else "skip",
            str(skill_md_path),
            detail if changed else "already current",
        ))

    # Prune stale cmd-* skill dirs (source command removed or no longer portable).
    if target.exists():
        for child in sorted(target.iterdir()):
            if not child.is_dir() or not child.name.startswith("cmd-"):
                continue
            if child.name in produced:
                continue
            if not dry_run:
                wrapper = child / "SKILL.md"
                if wrapper.exists():
                    wrapper.unlink()
                # Refuse to remove the dir if it has any other contents — would
                # indicate user/Codex placed something there we shouldn't touch.
                try:
                    child.rmdir()
                except OSError:
                    report.add(Change(
                        harness, "error", str(child),
                        "stale wrapper dir not empty; left in place", ok=False,
                    ))
                    continue
            report.add(Change(
                harness, "prune", str(child),
                "source command removed",
            ))


def strategy_command_to_codex_skill(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool,
    **_: object,
) -> None:
    _strategy_command_to_skill(
        source, target, report, harness, dry_run,
        _build_codex_skill_wrapper, "codex command-skill wrapper",
    )


def _build_cursor_skill_wrapper(name: str, meta: dict, body: str) -> str:
    """Render a Cursor Agent Skill that wraps a shared command."""
    description = meta.get("description", f"Wrapper for the {name} command.")
    arg_hint = meta.get("argument-hint", "")

    skill_desc = (
        f"{description} Trigger keywords: {name}, /{name}, run {name}, do {name}."
    )
    if len(skill_desc) > 1000:  # Cursor documents a 1024 char cap.
        skill_desc = skill_desc[:997] + "..."

    body_norm = body.replace("$ARGUMENTS", "{user-supplied arguments}").rstrip() + "\n"

    return (
        "---\n"
        f"name: cmd-{name}\n"
        f'description: "{skill_desc}"\n'
        "---\n"
        "\n"
        f"# {name} (Cursor skill wrapper for a shared command)\n"
        "\n"
        f"Source command: `{CONFIG_HOME / 'commands' / (name + '.md')}`. Cursor Agent Skills are the portable surface for projecting shared command workflows into Cursor.\n"
        "\n"
        f"**Argument hint**: `{arg_hint}`\n"
        "\n"
        f"When the user asks to run `{name}`, types `/{name}`, or otherwise invokes the workflow, execute the steps below.\n"
        "\n"
        "## Workflow\n"
        "\n"
        f"{body_norm}"
    )


def strategy_command_to_cursor_skill(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool,
    **_: object,
) -> None:
    _strategy_command_to_skill(
        source, target, report, harness, dry_run,
        _build_cursor_skill_wrapper, "cursor command-skill wrapper",
    )


def _prune_stale_tomls(
    target: Path,
    keep: set[str],
    prefix_keep: str,
    report: Report,
    harness: str,
    dry_run: bool,
) -> None:
    """Remove .toml files in target that aren't in `keep`, except those starting with prefix_keep
    (those are managed by translate_skills_to_toml)."""
    if not target.exists():
        return
    for existing in target.glob("*.toml"):
        if existing.name in keep:
            continue
        if existing.name.startswith(prefix_keep):
            continue
        if not dry_run:
            existing.unlink()
        report.add(Change(harness, "prune", str(existing), "command source removed"))


# ---- MCP source propagation ----------------------------------------------

# Secret shape heuristics. We warn (never block) when the merged MCP source set
# contains anything matching these, so users see the risk before Syncthing or
# harness projection carries the secret further. See ../references/mcp-manifest.md.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ctx7sk-[a-f0-9-]{20,}"), "Context7 API key"),
    (re.compile(r"sk-[A-Za-z0-9]{10,}"), "OpenAI/Anthropic-style key"),
    (re.compile(r"gh[oprs]_[A-Za-z0-9]{36,}"), "GitHub token"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "GitLab PAT"),
    (re.compile(r"hf_[A-Za-z0-9]{30,}"), "HuggingFace token"),
    (re.compile(r"xox[bpoars]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "AWS access key ID"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"), "JWT"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_.\-+/=]{10,}"), "Bearer token"),
    (re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{16,}"), "Basic auth credential"),
]


def _scan_secrets(obj: object, hits: list[str]) -> None:
    """Recursively record the label of any secret-shaped string found under `obj`."""
    if isinstance(obj, str):
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(obj):
                hits.append(label)
                return
    elif isinstance(obj, dict):
        for v in obj.values():
            _scan_secrets(v, hits)
    elif isinstance(obj, list):
        for v in obj:
            _scan_secrets(v, hits)


# Minimal TOML emitter for the mcp_servers subset (strings, bools, numbers,
# arrays of those, one level of nested tables for `env`). Avoids adding a
# runtime dependency on tomlkit or tomli_w — the surface we need is small.
_TOML_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(k: str) -> str:
    return k if _TOML_BARE_KEY.match(k) else json.dumps(k)


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ValueError(f"unsupported TOML value type: {type(v).__name__}")


def _emit_codex_mcp_block(servers: dict[str, dict]) -> str:
    """Serialize a {name: config} map into `[mcp_servers.NAME]` TOML tables."""
    lines: list[str] = []
    for name in sorted(servers):
        cfg = dict(servers[name])  # copy so we can pop `env`
        env = cfg.pop("env", None)
        lines.append(f"[mcp_servers.{_toml_key(name)}]")
        for k, v in cfg.items():
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        if env:
            lines.append("")
            lines.append(f"[mcp_servers.{_toml_key(name)}.env]")
            for k, v in sorted(env.items()):
                lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _canonical_mcp_name(name: str) -> str:
    """Normalize server names for drift-tolerant comparison. `sequential_thinking`
    and `sequential-thinking` are treated as the same server so the manifest can
    unify spellings that pre-existing harness configs had drifted on."""
    return name.replace("_", "-").lower()


def _parse_toml_key_head(key_expr: str) -> str:
    """Return the first dotted component of a TOML table-header key expression.
    Handles bare keys (`foo.bar` -> `foo`) and quoted keys (`"a.b".env` -> `a.b`)."""
    if key_expr.startswith('"'):
        end = 1
        while end < len(key_expr) and key_expr[end] != '"':
            if key_expr[end] == "\\":
                end += 2
            else:
                end += 1
        try:
            return json.loads(key_expr[: end + 1])
        except json.JSONDecodeError:
            return key_expr[1:end]
    dot = key_expr.find(".")
    return key_expr if dot == -1 else key_expr[:dot]


def _is_toml_body_line(line: str) -> bool:
    """True if `line` plausibly belongs to the body of the previous table
    (blank line, or `key = value` at column 0, or indented continuation of an
    array/inline-table value). False if it could start a new section — that
    includes column-0 comments and new `[table]` headers."""
    if not line.strip():
        return True
    if line.startswith((" ", "\t")):
        return True  # indented continuation
    if line.lstrip().startswith("["):
        return False  # new table header
    if line.lstrip().startswith("#"):
        return False  # column-0 comment — end of section for our purposes
    return True  # assume `key = value`


def _strip_mcp_blocks_by_canonical(text: str, canonical_names: set[str]) -> str:
    """Remove [mcp_servers.NAME] tables (and any NAME.sub tables like NAME.env)
    where canonical(NAME) is in the set. Leaves all other [mcp_servers.*] tables
    and all non-table content untouched. Used only on first-time migration;
    subsequent runs update the managed-marker region in place."""
    if not canonical_names:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    header_re = re.compile(r"^\[mcp_servers\.([^\]]+)\]")
    for line in lines:
        stripped = line.lstrip()
        m = header_re.match(stripped)
        if m:
            base = _parse_toml_key_head(m.group(1))
            if _canonical_mcp_name(base) in canonical_names:
                skipping = True
                # trim trailing blank lines we already emitted to avoid leaving orphans
                while out and out[-1].strip() == "":
                    out.pop()
                continue
            skipping = False
        elif skipping and not _is_toml_body_line(line):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def _splice_codex_managed_block(text: str, new_block: str) -> str:
    """Insert/replace the managed block within Codex config text. Assumes any
    merged-source tables have already been stripped from `text`."""
    if new_block.strip():
        managed = f"{_CODEX_MCP_BEGIN}\n\n{new_block.rstrip()}\n\n{_CODEX_MCP_END}\n"
    else:
        managed = f"{_CODEX_MCP_BEGIN}\n{_CODEX_MCP_END}\n"

    if _CODEX_MCP_BEGIN in text and _CODEX_MCP_END in text:
        pattern = re.compile(
            re.escape(_CODEX_MCP_BEGIN) + r".*?" + re.escape(_CODEX_MCP_END) + r"\n?",
            re.DOTALL,
        )
        return pattern.sub(lambda _: managed, text, count=1)

    # First-time: insert before [projects.*] (the file's visual boundary) or at EOF
    marker = re.search(r"^(?:# [^\n]*\n)*\[projects\.", text, re.MULTILINE)
    if marker:
        insert_at = marker.start()
        head = text[:insert_at].rstrip() + "\n\n"
        tail = text[insert_at:]
        return head + managed + "\n" + tail
    return text.rstrip() + "\n\n" + managed


@dataclass(frozen=True)
class McpServerSet:
    servers: dict[str, dict]
    source_detail: str


def _read_json_object(
    path: Path,
    report: Report,
    harness: str,
    target: Path,
    source_kind: str,
    *,
    missing_ok: bool,
) -> dict | None:
    if not path.exists():
        if not missing_ok:
            report.add(Change(harness, "skip", str(target), f"no {source_kind} at {path}"))
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.add(Change(harness, "error", str(target), f"invalid {source_kind}: {e}", ok=False))
        return None
    if not isinstance(data, dict):
        report.add(Change(harness, "error", str(target), f"{source_kind} is not a JSON object", ok=False))
        return None
    return data


def _extract_mcp_servers(
    data: dict,
    report: Report,
    harness: str,
    target: Path,
    source_kind: str,
) -> dict[str, dict] | None:
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        report.add(Change(harness, "error", str(target), f"{source_kind} 'mcpServers' is not an object", ok=False))
        return None
    invalid = [name for name, cfg in servers.items() if not isinstance(name, str) or not isinstance(cfg, dict)]
    if invalid:
        report.add(Change(
            harness,
            "error",
            str(target),
            f"{source_kind} has non-object MCP entries: {', '.join(map(str, invalid))}",
            ok=False,
        ))
        return None
    return servers


def _load_mcp_servers(source: Path, report: Report, harness: str, target: Path) -> McpServerSet | None:
    """Return the merged MCP server set for projection.

    The selected manifest remains authoritative for portable/shared servers.
    Claude Code app connectors live in ~/.claude.json, so we merge them as a
    machine-local overlay and only add names not already supplied by the manifest.
    """
    manifest_data = _read_json_object(source, report, harness, target, "mcp manifest", missing_ok=True)
    if manifest_data is None and source.exists():
        return None
    manifest_servers = (
        _extract_mcp_servers(manifest_data, report, harness, target, "mcp manifest")
        if manifest_data is not None
        else {}
    )
    if manifest_servers is None:
        return None

    claude_data = _read_json_object(
        CLAUDE_CONFIG_JSON_PATH,
        report,
        harness,
        target,
        "Claude connector registry",
        missing_ok=True,
    )
    if claude_data is None and CLAUDE_CONFIG_JSON_PATH.exists():
        return None
    claude_servers = (
        _extract_mcp_servers(claude_data, report, harness, target, "Claude connector registry")
        if claude_data is not None
        else {}
    )
    if claude_servers is None:
        return None

    servers: dict[str, dict] = dict(manifest_servers)
    seen_canonicals = {_canonical_mcp_name(name) for name in manifest_servers}
    claude_added = 0
    for name, cfg in claude_servers.items():
        canonical = _canonical_mcp_name(name)
        if canonical in seen_canonicals:
            continue
        servers[name] = cfg
        seen_canonicals.add(canonical)
        claude_added += 1

    if not servers:
        report.add(Change(
            harness,
            "skip",
            str(target),
            f"no mcp servers in {source} or {CLAUDE_CONFIG_JSON_PATH}",
        ))
        return None

    if not source.exists():
        source_parts = ["manifest=missing"]
    else:
        source_parts = [f"manifest={len(manifest_servers)}"]
    if claude_servers:
        source_parts.append(f"claude_registry={claude_added}/{len(claude_servers)}")
    source_detail = ", ".join(source_parts)

    hits: list[str] = []
    _scan_secrets(servers, hits)
    if hits:
        kinds = ", ".join(sorted(set(hits)))
        report.add(Change(harness, "warn", str(target),
                          f"mcp sources contain inline secrets ({kinds}); propagating anyway"))
    return McpServerSet(servers=servers, source_detail=source_detail)


def strategy_mcp_to_codex(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Upsert merged MCP servers into ~/.codex/config.toml inside a managed block.

    Preserves any pre-existing [mcp_servers.*] tables whose canonical name is
    NOT in the merged source set — those are user-managed and left outside the block.
    Canonical matching unifies drifted spellings (e.g., `sequential_thinking`
    in Codex gets replaced by the source set's `sequential-thinking`).
    """
    server_set = _load_mcp_servers(source, report, harness, target)
    if server_set is None:
        return
    servers = server_set.servers
    if not target.exists():
        report.add(Change(harness, "error", str(target), "target config.toml missing", ok=False))
        return

    raw = target.read_text(encoding="utf-8")
    # Normalize CRLF to LF so idempotence comparisons hold on cross-platform
    # Syncthing'd files. We emit LF-only, so a CRLF config would be rewritten
    # every run otherwise.
    current = raw.replace("\r\n", "\n")
    try:
        parsed = tomllib.loads(current)
    except tomllib.TOMLDecodeError as e:
        report.add(Change(harness, "error", str(target), f"config.toml unparseable: {e}", ok=False))
        return
    existing_names = set(parsed.get("mcp_servers", {}).keys())
    source_canonicals = {_canonical_mcp_name(n) for n in servers}
    preserved = {n for n in existing_names if _canonical_mcp_name(n) not in source_canonicals}

    new_block = _emit_codex_mcp_block(servers) if servers else ""
    # Self-heal: unbalanced markers (e.g., a prior buggy run left an orphan
    # BEGIN/END) would make in-place replace target the wrong region. Strip
    # every marker line and treat as first-time.
    begin_count = current.count(_CODEX_MCP_BEGIN)
    end_count = current.count(_CODEX_MCP_END)
    if begin_count != end_count or begin_count > 1:
        current = "".join(
            line for line in current.splitlines(keepends=True)
            if _CODEX_MCP_BEGIN not in line and _CODEX_MCP_END not in line
        )

    # Always strip source-canonical tables. Inside the managed region they'd
    # be regenerated anyway; outside, they catch drift (e.g., user wrote
    # `sequential_thinking` outside the block while the merged source ships
    # `sequential-thinking` — canonical match, stripped, source spelling wins).
    cleaned = _strip_mcp_blocks_by_canonical(current, source_canonicals)
    updated = _splice_codex_managed_block(cleaned, new_block)

    if updated == current:
        report.add(Change(harness, "skip", str(target), "mcp block already current"))
        return

    try:
        tomllib.loads(updated)
    except tomllib.TOMLDecodeError as e:
        report.add(Change(harness, "error", str(target), f"generated TOML invalid: {e}", ok=False))
        return

    if not dry_run:
        backup_dir = target.parent / ".parity-backups"
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / "config.toml.pre-mcp-sync"
        if not backup.exists():
            backup.write_text(current, encoding="utf-8")
        target.write_text(updated, encoding="utf-8")

    detail = f"{len(servers)} managed ({server_set.source_detail})"
    if preserved:
        detail += f", {len(preserved)} preserved ({', '.join(sorted(preserved))})"
    report.add(Change(harness, "sync_mcp", str(target), detail))


def _sync_mcp_to_json_config(
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    *,
    create_if_missing: bool,
    sidecar_name: str = ".harness-sync-managed-mcp.json",
) -> None:
    server_set = _load_mcp_servers(source, report, harness, target)
    if server_set is None:
        return
    servers = server_set.servers
    if not target.exists():
        if create_if_missing:
            settings: dict[str, object] = {}
        else:
            report.add(Change(harness, "error", str(target), f"target {target.name} missing", ok=False))
            return
    else:
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            report.add(Change(harness, "error", str(target), f"invalid {target.name}: {e}", ok=False))
            return
        if not isinstance(loaded, dict):
            report.add(Change(harness, "error", str(target), f"{target.name} is not a JSON object", ok=False))
            return
        settings = loaded

    sidecar = target.parent / sidecar_name
    managed_names: set[str] = set()
    if sidecar.exists():
        try:
            loaded = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                managed_names = {str(x) for x in loaded}
        except json.JSONDecodeError:
            pass  # treat as empty; next write fixes it

    existing = settings.get("mcpServers") if isinstance(settings.get("mcpServers"), dict) else {}
    source_canonicals = {_canonical_mcp_name(n) for n in servers}
    managed_canonicals = {_canonical_mcp_name(n) for n in managed_names}
    new_servers: dict[str, dict] = {}
    for name, cfg in existing.items():
        canon = _canonical_mcp_name(name)
        if canon in source_canonicals:
            continue  # drift-tolerant: will be overwritten with source spelling
        if canon in managed_canonicals:
            continue  # was ours, now gone from manifest → prune
        new_servers[name] = cfg  # preserve user-managed entry
    for name, cfg in servers.items():
        new_servers[name] = cfg

    new_settings = dict(settings)
    new_settings["mcpServers"] = new_servers
    new_json = json.dumps(new_settings, indent=2) + "\n"
    changed = _write_if_changed(target, new_json, dry_run)

    sidecar_content = json.dumps(sorted(servers.keys()), indent=2) + "\n"
    sidecar_changed = _write_if_changed(sidecar, sidecar_content, dry_run)

    if changed or sidecar_changed:
        report.add(Change(harness, "sync_mcp", str(target),
                          f"{len(servers)} server{'s' if len(servers) != 1 else ''} ({server_set.source_detail})"))
    else:
        report.add(Change(harness, "skip", str(target), "mcp servers already current"))


def strategy_mcp_to_gemini(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Upsert merged MCP servers into ~/.gemini/settings.json's `mcpServers` key.

    Tracks managed server names in a sidecar so we can prune what used to be
    in the merged source but was removed. Servers never managed by us (e.g.
    Gemini-specific `context7`) are preserved untouched.
    """
    _sync_mcp_to_json_config(
        source,
        target,
        report,
        harness,
        dry_run,
        create_if_missing=False,
    )


def strategy_mcp_to_cursor(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Upsert merged MCP servers into Cursor's global ~/.cursor/mcp.json."""
    _sync_mcp_to_json_config(
        source,
        target,
        report,
        harness,
        dry_run,
        create_if_missing=True,
    )


def strategy_translate_hooks_to_codex_json(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Lift the `hooks` block from Claude's settings.json into Codex's hooks.json.

    Rewrites command-string path prefixes from `$HOME/.claude/hooks/` (and
    `~/.claude/hooks/`) to `$HOME/.codex/hooks/` so the wiring stays
    self-contained on the Codex side. The hook scripts themselves are
    per-child symlinks to `~/.claude/hooks/`, so both prefixes ultimately
    resolve to the same script — translating keeps the JSON readable and
    matches the rest of the harness-isolation pattern.
    """
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.add(Change(harness, "error", str(target), f"invalid settings.json: {e}", ok=False))
        return

    hooks_block = settings.get("hooks", {})
    if not isinstance(hooks_block, dict):
        report.add(Change(harness, "error", str(target),
                          "settings.json 'hooks' is not an object", ok=False))
        return

    def _rewrite(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: _rewrite(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_rewrite(v) for v in obj]
        if isinstance(obj, str):
            return obj.replace("$HOME/.claude/hooks/", "$HOME/.codex/hooks/") \
                      .replace("~/.claude/hooks/", "~/.codex/hooks/")
        return obj

    rewritten = _rewrite(hooks_block)
    content = json.dumps({"hooks": rewritten}, indent=2) + "\n"
    changed = _write_if_changed(target, content, dry_run)
    report.add(Change(
        harness, "translate" if changed else "skip",
        str(target),
        "codex hooks.json from claude settings.json" if changed else "already current",
    ))


def strategy_translate_bootstrap_hook_to_cursor_json(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Project Claude's bootstrap SessionStart hook into Cursor's user hooks.json."""
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.add(Change(harness, "error", str(target), f"invalid settings.json: {e}", ok=False))
        return

    session_start = settings.get("hooks", {}).get("SessionStart", [])
    managed_command = "hooks/bootstrap-agent-config.sh"
    managed_hooks: list[dict[str, object]] = []
    for group in session_start:
        if not isinstance(group, dict):
            continue
        for hook in group.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            command = hook.get("command", "")
            if not isinstance(command, str) or "bootstrap-agent-config.sh" not in command:
                continue
            cursor_hook: dict[str, object] = {"command": managed_command}
            timeout = hook.get("timeout")
            if isinstance(timeout, int):
                cursor_hook["timeout"] = max(1, (timeout + 999) // 1000)
            managed_hooks.append(cursor_hook)

    try:
        existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except json.JSONDecodeError as e:
        report.add(Change(harness, "error", str(target), f"invalid hooks.json: {e}", ok=False))
        return
    if not isinstance(existing, dict):
        report.add(Change(harness, "error", str(target), "hooks.json is not an object", ok=False))
        return

    hooks = existing.get("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
    current_session = hooks.get("sessionStart", [])
    if not isinstance(current_session, list):
        current_session = []
    preserved = [
        hook for hook in current_session
        if not (isinstance(hook, dict) and hook.get("command") == managed_command)
    ]

    new_hooks = dict(hooks)
    if managed_hooks or preserved:
        new_hooks["sessionStart"] = preserved + managed_hooks
    else:
        new_hooks.pop("sessionStart", None)

    updated = dict(existing)
    updated["version"] = updated.get("version", 1)
    updated["hooks"] = new_hooks
    content = json.dumps(updated, indent=2) + "\n"
    changed = _write_if_changed(target, content, dry_run)
    report.add(Change(
        harness, "translate" if changed else "skip",
        str(target),
        "cursor bootstrap hooks.json" if changed else "already current",
    ))


def strategy_generate_skill_index(
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    skill_mount: str = "~/.gemini/skills",
    **_: object,
) -> None:
    """Generate Gemini's skills/index.json from Claude's skill directory."""
    if not source.exists():
        report.add(Change(harness, "error", str(target), f"source missing: {source}", ok=False))
        return
    mount = os.path.expanduser(skill_mount)
    entries: list[dict[str, str]] = []
    for skill_dir in sorted(source.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue
        meta, _ = _parse_md(skill_md)
        entries.append({
            "name": skill_dir.name,
            "description": meta.get("description", f"Load the {skill_dir.name} skill."),
            "path": f"{mount}/{skill_dir.name}",
        })
    content = json.dumps(entries, indent=2) + "\n"
    changed = _write_if_changed(target, content, dry_run)
    report.add(Change(
        harness, "translate" if changed else "skip",
        str(target), f"{len(entries)} entries" if changed else "already current",
    ))


# ---------------------------------------------------------------------------
# Harness specs — declarative. Add a dict here to teach the engine a new harness.
# ---------------------------------------------------------------------------

def _claude_memory() -> Path:
    # Claude encodes the session CWD into the project dir by replacing both
    # '/' and '.' with '-'. Sessions run from $HOME, so /Users/foo.bar
    # becomes projects/-Users-foo-bar/memory. The dir gets auto-cleaned when
    # empty, so ensure it exists for downstream symlinks to resolve.
    # Match how Claude Code encodes the project dir: replace path separators and
    # '.' with '-'. On Windows that includes '\' and the drive ':' (C:\Users\x
    # -> C--Users-x); on POSIX only '/' and '.' ('\\'/':' can be literal
    # filename chars there, so leaving them preserves original behavior).
    seps = r"[/.\\:]" if os.name == "nt" else r"[/.]"
    encoded = re.sub(seps, "-", str(Path.home()))
    path = CLAUDE_HOME / "projects" / encoded / "memory"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _harness_specs() -> list[dict]:
    return [
        {
            "name": "config_repo",
            "home": CONFIG_HOME,
            "role": "source",
            "detect": lambda h: (h / "CLAUDE.md").exists()
            and (h / "skills").is_dir()
            and (h / "hooks").is_dir(),
            "artifacts": [],  # Source; nothing flows into it.
        },
        {
            "name": "claude",
            "home": CLAUDE_HOME,
            "role": "symlink",
            "detect": lambda h: h.exists(),
            "artifacts": [
                *[
                    {
                        "strategy": strategy_symlink,
                        "source": CONFIG_HOME / entry,
                        "target_rel": entry,
                        "opts": {"adopt_matching": True},
                    }
                    for entry in CLAUDE_LINK_ENTRIES
                ],
                {"strategy": strategy_symlink_preserve_real,
                 "source": CONFIG_HOME / "mcp-servers.json", "target_rel": "mcp-servers.json"},
            ],
        },
        {
            "name": "codex",
            "home": Path.home() / ".codex",
            "role": "symlink",
            # Codex signature: either its config file or the AGENTS.md (possibly already our symlink)
            "detect": lambda h: (h / "config.toml").exists() or (h / "AGENTS.md").exists(),
            "artifacts": [
                {"strategy": strategy_symlink,
                 "source": CONFIG_HOME / "agents",       "target_rel": "agents"},
                {"strategy": strategy_symlink,
                 "source": CONFIG_HOME / "AGENTS.md",    "target_rel": "AGENTS.md"},
                {"strategy": strategy_symlink,
                 "source": _claude_memory(),             "target_rel": "memories"},
                {"strategy": strategy_symlink_children,
                 "source": CONFIG_HOME / "skills",       "target_rel": "skills",
                 "opts": {"preserve": [".system"]}},
                # Codex 0.117+ removed ~/.codex/prompts/ discovery; project every
                # command as a Codex skill so it remains reachable.
                # See ../references/codex.md "Command Projection" section.
                {"strategy": strategy_command_to_codex_skill,
                 "source": CONFIG_HOME / "commands",     "target_rel": "skills"},
                # Hook scripts are harness-agnostic, so per-child symlinks keep
                # them in sync without copying.
                {"strategy": strategy_symlink_children,
                 "source": CONFIG_HOME / "hooks",        "target_rel": "hooks"},
                # Codex reads `hooks.json` directly; shared hook wiring lives in
                # repo `settings.json`. Translate the hooks block over, rewriting
                # path prefixes to keep Codex's config self-contained.
                {"strategy": strategy_translate_hooks_to_codex_json,
                 "source": CONFIG_HOME / "settings.json", "target_rel": "hooks.json"},
                {"strategy": strategy_mcp_to_codex,
                 "source": MCP_MANIFEST_PATH,            "target_rel": "config.toml"},
            ],
        },
        {
            "name": "gemini",
            "home": Path.home() / ".gemini",
            "role": "translate",
            "detect": lambda h: (h / "settings.json").exists(),
            "artifacts": [
                {"strategy": strategy_translate_commands_to_toml,
                 "source": CONFIG_HOME / "commands",     "target_rel": "commands"},
                {"strategy": strategy_translate_skills_to_toml,
                 "source": CONFIG_HOME / "skills",       "target_rel": "commands",
                 "opts": {"prefix": "skill-",
                          "skill_mount": "~/.gemini/skills"}},
                {"strategy": strategy_generate_skill_index,
                 "source": CONFIG_HOME / "skills",       "target_rel": "skills/index.json",
                 "opts": {"skill_mount": "~/.gemini/skills"}},
                {"strategy": strategy_mcp_to_gemini,
                 "source": MCP_MANIFEST_PATH,            "target_rel": "settings.json"},
            ],
        },
        {
            "name": "cursor",
            "home": Path.home() / ".cursor",
            "role": "symlink",
            # Cursor-owned signatures. Do not detect from generated ~/.cursor/skills.
            "detect": lambda h: (h / "argv.json").exists() or (h / "skills-cursor").is_dir(),
            "artifacts": [
                # Cursor reserves ~/.cursor/skills-cursor for built-ins. User/project
                # skills belong under ~/.cursor/skills, so project Claude skills there.
                {"strategy": strategy_symlink_children,
                 "source": CONFIG_HOME / "skills",       "target_rel": "skills"},
                {"strategy": strategy_command_to_cursor_skill,
                 "source": CONFIG_HOME / "commands",     "target_rel": "skills"},
                {"strategy": strategy_symlink_children,
                 "source": CONFIG_HOME / "hooks",        "target_rel": "hooks"},
                {"strategy": strategy_translate_bootstrap_hook_to_cursor_json,
                 "source": CONFIG_HOME / "settings.json", "target_rel": "hooks.json"},
                {"strategy": strategy_mcp_to_cursor,
                 "source": MCP_MANIFEST_PATH,            "target_rel": "mcp.json"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def sync(
    only: set[str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Report:
    report = Report()

    if not CONFIG_HOME.is_dir():
        print(f"FATAL: config repo missing: {CONFIG_HOME}", file=sys.stderr)
        sys.exit(2)
    if not (
        (CONFIG_HOME / "CLAUDE.md").exists()
        and (CONFIG_HOME / "skills").is_dir()
        and (CONFIG_HOME / "hooks").is_dir()
    ):
        print(f"FATAL: config repo incomplete: {CONFIG_HOME}", file=sys.stderr)
        sys.exit(2)

    for spec in _harness_specs():
        name = spec["name"]
        home = spec["home"]
        if only is not None and name not in only:
            continue
        if not home.exists():
            report.harnesses_skipped.append(f"{name} (no {home})")
            continue
        if not spec["detect"](home):
            report.harnesses_skipped.append(f"{name} (signature not found in {home})")
            continue
        if spec["role"] == "source":
            report.harnesses_detected.append(f"{name} [source]")
            continue

        report.harnesses_detected.append(f"{name} [{spec['role']}]")
        for art in spec["artifacts"]:
            target = home / art["target_rel"]
            opts = art.get("opts", {})
            try:
                art["strategy"](
                    source=art["source"],
                    target=target,
                    report=report,
                    harness=name,
                    dry_run=dry_run,
                    **opts,
                )
            except Exception as exc:  # noqa: BLE001 - one bad artifact must not abort the whole sync
                # e.g. Windows WinError 1314 when symlink privilege is unavailable.
                report.add(Change(name, "error", str(target),
                                  f"{type(exc).__name__}: {exc}", ok=False))

    if verbose:
        for c in report.changes:
            mark = "OK" if c.ok else "FAIL"
            print(f"[{mark}] {c.harness:7} {c.action:10} {c.target}"
                  + (f"  ({c.detail})" if c.detail else ""), file=sys.stderr)

    return report


def list_detected() -> None:
    for spec in _harness_specs():
        home = spec["home"]
        present = home.exists() and spec["detect"](home)
        mark = "✓" if present else "·"
        print(f"  {mark} {spec['name']:8} {spec['role']:10} {home}")


def print_summary(report: Report, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLIED"
    counts = report.by_action()
    print(f"harness_sync [{mode}]", file=sys.stderr)
    if report.harnesses_detected:
        print(f"  detected:  {', '.join(report.harnesses_detected)}", file=sys.stderr)
    if report.harnesses_skipped:
        print(f"  skipped:   {', '.join(report.harnesses_skipped)}", file=sys.stderr)
    if counts:
        parts = [f"{a}={n}" for a, n in sorted(counts.items())]
        print(f"  changes:   {' '.join(parts)}", file=sys.stderr)
    errs = report.errors()
    if errs:
        print(f"  errors:    {len(errs)}", file=sys.stderr)
        for e in errs:
            print(f"    {e.harness} {e.target}: {e.detail}", file=sys.stderr)


def main() -> int:
    # Windows consoles default to a legacy code page (e.g. cp1252) that can't
    # encode the status glyphs (✓/·), which would crash on the first print.
    # Force UTF-8 so the tool runs on a vanilla console; no-op on POSIX.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--dry-run", action="store_true", help="report changes, write nothing")
    p.add_argument("-v", "--verbose", action="store_true", help="trace each action")
    p.add_argument("--only", help="comma-separated harness names (e.g. codex,cursor,gemini)")
    p.add_argument("--list", action="store_true", help="list detected harnesses and exit")
    args = p.parse_args()

    if args.list:
        list_detected()
        return 0

    only = set(args.only.split(",")) if args.only else None
    report = sync(only=only, dry_run=args.dry_run, verbose=args.verbose)
    print_summary(report, args.dry_run)
    return 1 if report.errors() else 0


if __name__ == "__main__":
    sys.exit(main())

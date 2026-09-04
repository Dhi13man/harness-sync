#!/usr/bin/env python3
# ruff: noqa: T201
"""
Unified harness sync for the central config repo plus ~/.claude, ~/.codex,
~/.cursor, ~/.gemini, ~/.pi/agent, ~/.omp, and future harnesses.

Treats the cloned config repo as the source of truth and projects agents /
commands / skills / guidance / hooks into every detected harness using the
strategy that harness needs (symlink, per-child symlink, or translation).

Declarative. Idempotent. Extensible — adding a new harness is a dict entry.

Usage:
    harness_sync.py              # sync all detected harnesses
    harness_sync.py -v           # verbose (trace every action)
    harness_sync.py --dry-run    # report planned changes, write nothing
    harness_sync.py --only codex # only sync specific harnesses (comma-sep)
    harness_sync.py --only-capability mcp  # only sync MCP definitions
    harness_sync.py --list       # show detected harnesses and exit

Exit codes:
    0  success (all detected harnesses synced)
    1  partial failure (some artifacts failed; others ok)
    2  fatal (config repo missing / unrecoverable)
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import ntpath
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlsplit


def _reexec_with_modern_python() -> None:
    """Rerun under Python 3.11+ so stdlib tomllib is available."""
    override = os.environ.get("HARNESS_SYNC_PYTHON")
    candidates = [override] if override else []
    candidates.extend(["python3.13", "python3.12", "python3.11"])

    for candidate in candidates:
        if not candidate or not shutil.which(candidate):
            continue
        version = subprocess.run(
            [
                candidate,
                "-c",
                "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if version.returncode == 0 and tuple(
            map(int, version.stdout.strip().split("."))
        ) >= (3, 11):
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
                os.execvpe(
                    pyenv, [pyenv, "exec", "python", __file__, *sys.argv[1:]], env
                )


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

    return CONFIG_HOME / "mcp-servers.json"


def _claude_config_json_path() -> Path:
    configured = _expand_path_env("CLAUDE_CONFIG_JSON")
    if configured is not None:
        return configured
    return Path(
        os.environ.get("CLAUDE_CONFIG_PATH", str(Path.home() / ".claude.json"))
    ).expanduser()


def _pi_agent_home() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))
    return Path.home() / ".pi" / "agent"


def _cursor_cli_config_path() -> Path:
    configured = os.environ.get("CURSOR_CONFIG_DIR")
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser())) / "cli-config.json"
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    bsd_or_linux = sys.platform.startswith(
        ("linux", "freebsd", "openbsd", "netbsd", "dragonfly")
    )
    if xdg_home and bsd_or_linux:
        return (
            Path(os.path.abspath(Path(xdg_home).expanduser()))
            / "cursor"
            / "cli-config.json"
        )
    return Path.home() / ".cursor" / "cli-config.json"


_OMP_PROFILE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_OMP_RESERVED_PROFILE = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", re.IGNORECASE
)


def _normalize_omp_profile(value: str | None) -> str | None:
    profile = value.strip() if value is not None else ""
    if not profile or profile == "default":
        return None
    if (
        not _OMP_PROFILE_NAME.fullmatch(profile)
        or profile in {".", ".."}
        or profile.endswith(".")
        or _OMP_RESERVED_PROFILE.fullmatch(profile)
    ):
        raise ValueError(f"invalid OMP profile: {value!r}")
    return profile


def _omp_agent_home() -> Path:
    config_dir_name = os.environ.get("PI_CONFIG_DIR") or ".omp"
    if (os.name == "nt" and ntpath.splitdrive(config_dir_name)[0]) or ".." in re.split(
        r"[\\/]", config_dir_name
    ):
        raise ValueError(f"invalid OMP config directory: {config_dir_name!r}")
    profile_value = (
        os.environ["OMP_PROFILE"]
        if "OMP_PROFILE" in os.environ
        else os.environ.get("PI_PROFILE")
    )
    profile = _normalize_omp_profile(profile_value)
    config_home = Path(os.path.abspath(Path.home() / config_dir_name.lstrip("/\\")))
    if profile:
        return config_home / "profiles" / profile / "agent"
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured:
        configured_path = Path(os.path.abspath(Path(configured).expanduser()))
        try:
            legacy_profile = _normalize_omp_profile(os.environ.get("PI_PROFILE"))
        except ValueError:
            legacy_profile = None
        profile_derived = (
            config_home / "profiles" / legacy_profile / "agent"
            if legacy_profile
            else None
        )
        if configured_path != profile_derived:
            return configured_path
    return config_home / "agent"


CONFIG_HOME = _config_home()
CLAUDE_HOME = Path(
    os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude"))
).expanduser()
PI_AGENT_HOME = _pi_agent_home()
CURSOR_HOME = Path.home() / ".cursor"
CURSOR_CLI_CONFIG_PATH = _cursor_cli_config_path()
try:
    OMP_AGENT_HOME = _omp_agent_home()
    OMP_PROFILE_VALID = True
except ValueError:
    OMP_AGENT_HOME = Path.home() / ".omp" / "agent"
    OMP_PROFILE_VALID = False
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


def _claude_mcp_managed_path(target: Path) -> Path:
    """Keep ownership state scoped to one resolved Claude registry target."""
    basename = target.name if target.name.startswith(".") else f".{target.name}"
    return target.with_name(f"{basename}.harness-sync-managed-mcp.json")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


@dataclass
class Change:
    harness: str
    action: str  # "symlink", "retarget", "translate", "prune", "skip", "error"
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
_IO_REPARSE_TAG_LX_SYMLINK = 0xA000001D


def _is_reparse_point(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return bool(
        getattr(metadata, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


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
    return (
        not path.is_symlink()
        and getattr(st, "st_reparse_tag", None) == _IO_REPARSE_TAG_LX_SYMLINK
    )


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


def _links_into(link: Path, source: Path) -> bool:
    """Return whether a child link lexically targets its matching source child."""
    try:
        stored = Path(os.readlink(link))
    except OSError:
        return False
    if not stored.is_absolute():
        stored = link.parent / stored
    return _norm_link(str(stored)) == _norm_link(str(source / link.name))


_XXH64_MASK = (1 << 64) - 1
_XXH64_PRIME_1 = 11400714785074694791
_XXH64_PRIME_2 = 14029467366897019727
_XXH64_PRIME_3 = 1609587929392839161
_XXH64_PRIME_4 = 9650029242287828579
_XXH64_PRIME_5 = 2870177450012600261
_OMP_LOCK_HIGH_SEED = 0x4F4D502D4C4F434B
_OMP_LOCK_LOW_SEED = 0x50492D46494C454C


def _rotl64(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & _XXH64_MASK


def _xxh64_round(accumulator: int, lane: int) -> int:
    accumulator = (accumulator + lane * _XXH64_PRIME_2) & _XXH64_MASK
    accumulator = _rotl64(accumulator, 31)
    return accumulator * _XXH64_PRIME_1 & _XXH64_MASK


def _xxh64_merge(accumulator: int, value: int) -> int:
    accumulator ^= _xxh64_round(0, value)
    return (accumulator * _XXH64_PRIME_1 + _XXH64_PRIME_4) & _XXH64_MASK


def _xxh64(data: bytes, seed: int) -> int:
    """Small stdlib implementation matching OMP's xxhash-rust lock naming."""
    length = len(data)
    offset = 0
    if length >= 32:
        lanes = [
            (seed + _XXH64_PRIME_1 + _XXH64_PRIME_2) & _XXH64_MASK,
            (seed + _XXH64_PRIME_2) & _XXH64_MASK,
            seed & _XXH64_MASK,
            (seed - _XXH64_PRIME_1) & _XXH64_MASK,
        ]
        while offset <= length - 32:
            for index in range(4):
                lane = int.from_bytes(
                    data[offset + index * 8 : offset + index * 8 + 8], "little"
                )
                lanes[index] = _xxh64_round(lanes[index], lane)
            offset += 32
        digest = (
            _rotl64(lanes[0], 1)
            + _rotl64(lanes[1], 7)
            + _rotl64(lanes[2], 12)
            + _rotl64(lanes[3], 18)
        ) & _XXH64_MASK
        for lane in lanes:
            digest = _xxh64_merge(digest, lane)
    else:
        digest = (seed + _XXH64_PRIME_5) & _XXH64_MASK

    digest = (digest + length) & _XXH64_MASK
    while offset <= length - 8:
        lane = int.from_bytes(data[offset : offset + 8], "little")
        digest ^= _xxh64_round(0, lane)
        digest = (_rotl64(digest, 27) * _XXH64_PRIME_1 + _XXH64_PRIME_4) & _XXH64_MASK
        offset += 8
    if offset <= length - 4:
        lane = int.from_bytes(data[offset : offset + 4], "little")
        digest ^= lane * _XXH64_PRIME_1 & _XXH64_MASK
        digest = (_rotl64(digest, 23) * _XXH64_PRIME_2 + _XXH64_PRIME_3) & _XXH64_MASK
        offset += 4
    while offset < length:
        digest ^= data[offset] * _XXH64_PRIME_5 & _XXH64_MASK
        digest = _rotl64(digest, 11) * _XXH64_PRIME_1 & _XXH64_MASK
        offset += 1

    digest ^= digest >> 33
    digest = digest * _XXH64_PRIME_2 & _XXH64_MASK
    digest ^= digest >> 29
    digest = digest * _XXH64_PRIME_3 & _XXH64_MASK
    digest ^= digest >> 32
    return digest & _XXH64_MASK


def _omp_memory_lock_name(lock_path: str) -> str:
    encoded = lock_path.encode()
    high = _xxh64(encoded, _OMP_LOCK_HIGH_SEED)
    low = _xxh64(encoded, _OMP_LOCK_LOW_SEED)
    return f"omp-file-lock-{high:016x}{low:016x}"


def _try_omp_lock(lock_path: str) -> tuple[str, object] | None:
    if sys.platform == "linux":
        holder = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            holder.bind("\0" + _omp_memory_lock_name(lock_path))
        except OSError as error:
            holder.close()
            if error.errno == errno.EADDRINUSE:
                return None
            raise
        return ("socket", holder)
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        create_mutex.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (ctypes.c_void_p,)
        close_handle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        handle = create_mutex(None, 0, "Global\\" + _omp_memory_lock_name(lock_path))
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            if not close_handle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            return None
        return ("mutex", (close_handle, handle))

    import fcntl

    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return ("flock", (fcntl, descriptor))


def _release_omp_lock(holder: tuple[str, object]) -> None:
    kind, resource = holder
    if kind == "socket":
        resource.close()  # type: ignore[union-attr]
    elif kind == "mutex":
        close_handle, handle = resource  # type: ignore[misc]
        if not close_handle(handle):
            import ctypes

            raise ctypes.WinError(ctypes.get_last_error())
    else:
        fcntl, descriptor = resource  # type: ignore[misc]
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _omp_config_lock(target: Path, retries: int = 50, delay: float = 0.1):
    lock_path = os.path.abspath(str(target)) + ".lock"
    holder = None
    for attempt in range(retries):
        holder = _try_omp_lock(lock_path)
        if holder is not None:
            break
        if attempt + 1 < retries:
            time.sleep(delay)
    if holder is None:
        raise TimeoutError(f"could not acquire Oh My Pi MCP lock for {target}")
    try:
        yield
    finally:
        _release_omp_lock(holder)


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
    if _is_reparse_point(link):
        return ("error", f"refusing to replace unknown reparse point: {link}")
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
    create_source_directory: bool = False,
    **_: object,
) -> None:
    if not source.exists():
        if (
            not create_source_directory
            or source.is_symlink()
            or _is_foreign_link(source)
        ):
            report.add(
                Change(
                    harness, "error", str(target), f"source missing: {source}", ok=False
                )
            )
            return
        if not dry_run:
            source.mkdir(parents=True, exist_ok=False)
    action, detail = _safe_symlink(
        source, target, dry_run, adopt_matching=adopt_matching
    )
    report.add(Change(harness, action, str(target), detail, ok=(action != "error")))


def strategy_symlink_preserve_real(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Create/repair a symlink, but leave an existing real target alone."""
    if not source.exists():
        report.add(Change(harness, "skip", str(target), f"source missing: {source}"))
        return
    if (
        _safe_exists(target)
        and not target.is_symlink()
        and not _is_foreign_link(target)
    ):
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
        report.add(
            Change(harness, "error", str(target), f"source missing: {source}", ok=False)
        )
        return

    preserve_set = set(preserve)
    if target.is_symlink() or _is_reparse_point(target):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "target must be a real directory, not a link",
                ok=False,
            )
        )
        return
    if _safe_exists(target) and not target.is_dir():
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "target must be a real directory",
                ok=False,
            )
        )
        return
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
        if not _links_into(existing, source):
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


def _write_if_changed(
    path: Path,
    content: str,
    dry_run: bool,
    create_mode: int | None = None,
) -> bool:
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
        if create_mode is not None and not _safe_exists(path):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, create_mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
        else:
            path.write_text(content, encoding="utf-8")
    return True


def _atomic_write_if_changed(
    path: Path,
    content: str,
    dry_run: bool,
    create_mode: int = 0o644,
    enforce_mode: bool = False,
) -> bool:
    """Publish changed text by same-directory replace with explicit mode policy."""
    if path.is_symlink() or _is_reparse_point(path):
        raise OSError(f"managed target must be a real file: {path}")
    existing_mode = create_mode
    if _safe_exists(path):
        try:
            existing_mode = stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
            content_matches = path.read_text(encoding="utf-8") == content
            mode_matches = (
                not enforce_mode or os.name == "nt" or existing_mode == create_mode
            )
            if content_matches and mode_matches:
                return False
            if content_matches:
                if not dry_run:
                    os.chmod(path, create_mode, follow_symlinks=False)
                return True
        except OSError:
            pass
    if dry_run:
        return True

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, create_mode if enforce_mode else existing_mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def strategy_translate_commands_to_toml(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Convert config commands/*.md into target/*.toml. Removes .toml whose .md vanished."""
    if not source.exists():
        report.add(
            Change(harness, "error", str(target), f"source missing: {source}", ok=False)
        )
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
        report.add(
            Change(
                harness,
                "translate" if changed else "skip",
                str(out),
                "gemini command toml" if changed else "already current",
            )
        )

    _prune_stale_tomls(
        target,
        source_names,
        prefix_keep="skill-",
        report=report,
        harness=harness,
        dry_run=dry_run,
    )


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
        report.add(
            Change(harness, "error", str(target), f"source missing: {source}", ok=False)
        )
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
        report.add(
            Change(
                harness,
                "translate" if changed else "skip",
                str(out),
                "gemini skill toml" if changed else "already current",
            )
        )

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
        report.add(
            Change(harness, "error", str(target), f"source missing: {source}", ok=False)
        )
        return
    if (
        target.is_symlink()
        or _is_reparse_point(target)
        or (_safe_exists(target) and not target.is_dir())
    ):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "command-skill target must be a real directory",
                ok=False,
            )
        )
        return

    produced: set[str] = set()

    for md in sorted(source.glob("*.md")):
        name = md.stem
        produced.add(f"cmd-{name}")
        meta, body = _parse_md(md)
        skill_dir = target / f"cmd-{name}"
        skill_md_path = skill_dir / "SKILL.md"

        content = builder(name, meta, body)

        if (
            skill_dir.is_symlink()
            or _is_reparse_point(skill_dir)
            or (_safe_exists(skill_dir) and not skill_dir.is_dir())
            or skill_md_path.is_symlink()
            or _is_reparse_point(skill_md_path)
            or (_safe_exists(skill_md_path) and not skill_md_path.is_file())
        ):
            report.add(
                Change(
                    harness,
                    "error",
                    str(skill_md_path),
                    "command-skill target must be a real directory and file",
                    ok=False,
                )
            )
            continue

        if not dry_run:
            skill_dir.mkdir(parents=True, exist_ok=True)
        changed = _write_if_changed(skill_md_path, content, dry_run)
        report.add(
            Change(
                harness,
                "translate" if changed else "skip",
                str(skill_md_path),
                detail if changed else "already current",
            )
        )

    # Prune stale cmd-* skill dirs (source command removed or no longer portable).
    if target.exists():
        for child in sorted(target.iterdir()):
            if not child.name.startswith("cmd-"):
                continue
            if child.name in produced:
                continue
            if child.is_symlink() or _is_reparse_point(child):
                report.add(
                    Change(
                        harness,
                        "skip",
                        str(child),
                        "unowned link-like stale wrapper preserved",
                    )
                )
                continue
            if not child.is_dir():
                continue
            wrapper = child / "SKILL.md"
            if (
                not wrapper.is_file()
                or wrapper.is_symlink()
                or _is_reparse_point(wrapper)
            ):
                report.add(
                    Change(
                        harness,
                        "skip",
                        str(child),
                        "unowned stale wrapper directory preserved",
                    )
                )
                continue
            try:
                wrapper_content = wrapper.read_text(encoding="utf-8")
            except OSError as error:
                report.add(Change(harness, "error", str(wrapper), str(error), ok=False))
                continue
            label = "Codex" if harness == "codex" else "Cursor"
            source_name = child.name.removeprefix("cmd-")
            marker = f"# {source_name} ({label} skill wrapper for a shared command)"
            if marker not in wrapper_content:
                report.add(
                    Change(
                        harness,
                        "skip",
                        str(child),
                        "unowned stale wrapper directory preserved",
                    )
                )
                continue
            if not dry_run:
                wrapper.unlink()
                # Refuse to remove the dir if it has any other contents — would
                # indicate user/Codex placed something there we shouldn't touch.
                try:
                    child.rmdir()
                except OSError:
                    report.add(
                        Change(
                            harness,
                            "error",
                            str(child),
                            "stale wrapper dir not empty; left in place",
                            ok=False,
                        )
                    )
                    continue
            report.add(
                Change(
                    harness,
                    "prune",
                    str(child),
                    "source command removed",
                )
            )


def strategy_command_to_codex_skill(
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    **_: object,
) -> None:
    _strategy_command_to_skill(
        source,
        target,
        report,
        harness,
        dry_run,
        _build_codex_skill_wrapper,
        "codex command-skill wrapper",
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
    source: Path,
    target: Path,
    report: Report,
    harness: str,
    dry_run: bool,
    **_: object,
) -> None:
    _strategy_command_to_skill(
        source,
        target,
        report,
        harness,
        dry_run,
        _build_cursor_skill_wrapper,
        "cursor command-skill wrapper",
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

# Secret shape heuristics. Shared-manifest matches block projection; matches in
# Claude's machine-local connector registry stay local. See mcp-manifest.md.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ctx7sk-[a-f0-9-]{20,}"), "Context7 API key"),
    (
        re.compile(
            r"\bsk-(?:[A-Za-z0-9]{10,}|(?:proj|svcacct|ant-api\d{2})-[A-Za-z0-9_-]{10,})\b"
        ),
        "OpenAI/Anthropic-style key",
    ),
    (re.compile(r"gh[oprs]_[A-Za-z0-9]{36,}"), "GitHub token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{30,}"), "GitHub token"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{20,}"), "GitLab PAT"),
    (re.compile(r"hf_[A-Za-z0-9]{30,}"), "HuggingFace token"),
    (re.compile(r"xox[bpoars]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"A(?:KI|SI)A[A-Z0-9]{16}"), "AWS access key ID"),
    (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        "private key",
    ),
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
        "JWT",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9_.\-+/=]{10,}"), "Bearer token"),
    (re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{16,}"), "Basic auth credential"),
]
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_REFERENCE_WITH_DEFAULT = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-[^}]*\}")


def _scan_secrets(obj: object, hits: list[str]) -> None:
    """Recursively record the label of any secret-shaped string found under `obj`."""
    if isinstance(obj, str):
        for pattern, label in _SECRET_PATTERNS:
            if pattern.search(obj):
                hits.append(label)
                return
        try:
            parsed = urlsplit(obj)
            if (
                parsed.scheme
                and parsed.netloc
                and (parsed.username is not None or parsed.password is not None)
            ):
                hits.append("URI userinfo credential")
        except ValueError:
            pass
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _scan_secrets(key, hits)
            _scan_secrets(value, hits)
    elif isinstance(obj, list):
        for v in obj:
            _scan_secrets(v, hits)


def _credential_key(name: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower().replace("_", "-")
    compact = normalized.replace("-", "")
    segments = {part for part in normalized.split("-") if part}
    sensitive_segments = {
        "auth",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "jwt",
        "passphrase",
        "passwd",
        "password",
        "sas",
        "secret",
        "sig",
        "signature",
        "token",
    }
    return (
        bool(segments & sensitive_segments)
        or ({"api", "key"} <= segments)
        or ({"access", "key"} <= segments)
        or ({"private", "key"} <= segments)
        or ({"secret", "key"} <= segments)
        or normalized
        in {
            "authorization",
            "proxy-authorization",
            "cookie",
            "auth",
            "token",
            "password",
            "passwd",
            "secret",
            "api-key",
            "apikey",
            "jwt",
            "sas",
            "sig",
            "signature",
            "x-amz-signature",
            "x-goog-signature",
        }
        or compact.endswith(
            (
                "apikey",
                "token",
                "password",
                "passwd",
                "secret",
                "credential",
                "accesskey",
                "privatekey",
                "secretkey",
                "signature",
                "jwt",
            )
        )
        or normalized.endswith(("-token", "-password", "-passwd", "-secret"))
        or (
            normalized.endswith("-key")
            and any(
                part in normalized for part in ("api", "access", "private", "secret")
            )
        )
    )


def _environment_reference_issues(servers: dict[str, dict]) -> list[str]:
    """Find references that cannot retain one meaning in every target harness."""
    issues: list[str] = []

    def contains_reference(value: object) -> bool:
        if isinstance(value, str):
            return "${" in value
        if isinstance(value, list):
            return any(contains_reference(item) for item in value)
        if isinstance(value, dict):
            return any(contains_reference(item) for item in value.values())
        return False

    for server_name, config in servers.items():
        for field_name, value in config.items():
            if field_name in {"env", "headers"}:
                continue
            if contains_reference(value):
                issues.append(f"{server_name}.{field_name}")

        for section in ("env", "headers"):
            values = config.get(section)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    issues.append(f"{server_name}.{section}.{key}")
                    continue
                exact = _ENV_REFERENCE.fullmatch(value)
                bearer = (
                    section == "headers"
                    and key.lower() == "authorization"
                    and re.fullmatch(r"Bearer\s+\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
                )
                same_name_env = (
                    section == "env" and exact is not None and exact.group(1) == key
                )
                if not (same_name_env or (section == "headers" and (exact or bearer))):
                    issues.append(f"{server_name}.{section}.{key}")
    return issues


def _unreferenced_credential_fields(servers: dict[str, dict]) -> list[str]:
    issues: list[str] = []
    for server_name, config in servers.items():

        def find_nested(value: object, path: str) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    child_path = f"{path}.{key}"
                    if isinstance(key, str) and _credential_key(key):
                        issues.append(child_path)
                    find_nested(nested, child_path)
            elif isinstance(value, list):
                for index, nested in enumerate(value):
                    find_nested(nested, f"{path}[{index}]")

        for field_name, value in config.items():
            if field_name in {"env", "headers"}:
                continue
            field_path = f"{server_name}.{field_name}"
            if _credential_key(field_name):
                issues.append(field_path)
            find_nested(value, field_path)

        args = config.get("args")
        if isinstance(args, list):
            for value in args:
                if not isinstance(value, str):
                    continue
                flag = value.lstrip("-").split("=", 1)[0]
                credential_flag = value.startswith("-") and (
                    flag.lower().replace("_", "-") == "key" or _credential_key(flag)
                )
                credential_header = re.search(
                    r"(?i)(?:authorization|api[-_]?key|(?:access[-_]?)?token|client[-_]?secret|password|passwd|secret)\s*[:=]",
                    value,
                )
                if credential_flag or credential_header:
                    issues.append(f"{server_name}.args")
                    break
        url = config.get("url")
        if isinstance(url, str):
            try:
                parsed_url = urlsplit(url)
                credential_query = any(
                    key.lower().replace("_", "-") == "key" or _credential_key(key)
                    for key, _value in parse_qsl(
                        parsed_url.query, keep_blank_values=True
                    )
                )
                if parsed_url.username is not None or credential_query:
                    issues.append(f"{server_name}.url")
            except ValueError:
                pass
        for unsupported in ("auth", "oauth"):
            if unsupported in config:
                issues.append(f"{server_name}.{unsupported}")
        for section in ("env", "headers"):
            values = config.get(section)
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if not isinstance(key, str) or not _credential_key(key):
                    continue
                exact = isinstance(value, str) and _ENV_REFERENCE.fullmatch(value)
                bearer = (
                    section == "headers"
                    and isinstance(value, str)
                    and re.fullmatch(r"Bearer\s+\$\{[A-Za-z_][A-Za-z0-9_]*\}", value)
                )
                if not exact and not bearer:
                    issues.append(f"{server_name}.{section}.{key}")
    return list(dict.fromkeys(issues))


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


def _codex_mcp_server(name: str, source: dict) -> dict:
    """Translate portable secret references to Codex's native indirection fields."""
    config = dict(source)
    config.pop("type", None)

    env = config.pop("env", None)
    static_env: dict[str, object] = {}
    env_vars: list[str] = []
    if env is not None:
        if not isinstance(env, dict):
            raise ValueError(f"MCP server {name!r} env must be an object")
        for target_name, value in env.items():
            if not isinstance(target_name, str) or not isinstance(value, str):
                raise ValueError(f"MCP server {name!r} env must contain strings")
            match = _ENV_REFERENCE.fullmatch(value)
            if match:
                source_name = match.group(1)
                if target_name != source_name:
                    raise ValueError(
                        f"MCP server {name!r} cannot map ${{{source_name}}} to {target_name!r} in Codex; use the same name"
                    )
                env_vars.append(source_name)
            elif _ENV_REFERENCE_WITH_DEFAULT.search(value):
                raise ValueError(
                    f"MCP server {name!r} uses an environment default that Codex cannot preserve"
                )
            elif _ENV_REFERENCE.search(value):
                raise ValueError(
                    f"MCP server {name!r} must use an exact ${{VAR}} environment reference"
                )
            else:
                static_env[target_name] = value
    if static_env:
        config["env"] = static_env
    if env_vars:
        config["env_vars"] = env_vars

    headers = config.pop("headers", None)
    static_headers: dict[str, str] = {}
    env_headers: dict[str, str] = {}
    if headers is not None:
        if not isinstance(headers, dict):
            raise ValueError(f"MCP server {name!r} headers must be an object")
        for header, value in headers.items():
            if not isinstance(header, str) or not isinstance(value, str):
                raise ValueError(f"MCP server {name!r} headers must contain strings")
            match = _ENV_REFERENCE.fullmatch(value)
            bearer = re.fullmatch(r"Bearer\s+\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
            if header.lower() == "authorization" and bearer:
                config["bearer_token_env_var"] = bearer.group(1)
            elif match:
                env_headers[header] = match.group(1)
            elif _ENV_REFERENCE_WITH_DEFAULT.search(value):
                raise ValueError(
                    f"MCP server {name!r} uses a header environment default that Codex cannot preserve"
                )
            elif _ENV_REFERENCE.search(value):
                raise ValueError(
                    f"MCP server {name!r} must use an exact ${{VAR}} header reference or Bearer ${{VAR}} authorization"
                )
            else:
                static_headers[header] = value
    if static_headers:
        config["http_headers"] = static_headers
    if env_headers:
        config["env_http_headers"] = env_headers

    return config


def _cursor_mcp_value(value: object) -> object:
    """Translate canonical `${VAR}` references to Cursor's `${env:VAR}` form."""
    if isinstance(value, str):
        if _ENV_REFERENCE_WITH_DEFAULT.search(value):
            raise ValueError("Cursor cannot preserve `${VAR:-default}` MCP references")
        return _ENV_REFERENCE.sub(r"${env:\1}", value)
    if isinstance(value, list):
        return [_cursor_mcp_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _cursor_mcp_value(item) for key, item in value.items()}
    return value


def _cursor_mcp_server(source: dict) -> dict:
    config = dict(source)
    for key in ("env", "headers"):
        if key in config:
            config[key] = _cursor_mcp_value(config[key])
    return config


def _emit_codex_mcp_block(servers: dict[str, dict]) -> str:
    """Serialize a {name: config} map into `[mcp_servers.NAME]` TOML tables."""
    lines: list[str] = []
    for name in sorted(servers):
        cfg = _codex_mcp_server(name, servers[name])
        nested = {
            key: cfg.pop(key)
            for key in ("env", "http_headers", "env_http_headers")
            if key in cfg
        }
        lines.append(f"[mcp_servers.{_toml_key(name)}]")
        for k, v in cfg.items():
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        for key, values in nested.items():
            if not isinstance(values, dict):
                raise ValueError(f"MCP server {name!r} {key} must be an object")
            if not values:
                continue
            lines.append("")
            lines.append(f"[mcp_servers.{_toml_key(name)}.{key}]")
            for k, v in sorted(values.items()):
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
            report.add(
                Change(harness, "skip", str(target), f"no {source_kind} at {path}")
            )
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.add(
            Change(
                harness, "error", str(target), f"invalid {source_kind}: {e}", ok=False
            )
        )
        return None
    if not isinstance(data, dict):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"{source_kind} is not a JSON object",
                ok=False,
            )
        )
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
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"{source_kind} 'mcpServers' is not an object",
                ok=False,
            )
        )
        return None
    invalid = [
        name
        for name, cfg in servers.items()
        if not isinstance(name, str) or not isinstance(cfg, dict)
    ]
    if invalid:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"{source_kind} has {len(invalid)} non-object MCP entr{'y' if len(invalid) == 1 else 'ies'}",
                ok=False,
            )
        )
        return None
    return servers


def _mcp_server_shape_issues(servers: dict[str, dict]) -> list[str]:
    """Validate the portable subset every target harness can represent."""
    issues: list[str] = []
    allowed_fields = {"command", "args", "type", "env", "cwd", "url", "headers"}
    for name, config in servers.items():
        if not name:
            issues.append("<empty-name>")
        for field_name in config.keys() - allowed_fields:
            issues.append(f"{name}.{field_name}")
        transport = config.get("type")
        if "type" in config and (
            not isinstance(transport, str) or transport not in {"stdio", "http"}
        ):
            issues.append(f"{name}.type")
            continue

        has_command = isinstance(config.get("command"), str) and bool(config["command"])
        has_url = isinstance(config.get("url"), str) and bool(config["url"])
        remote = transport == "http"
        if remote:
            if not has_url:
                issues.append(f"{name}.url")
            for field_name in ("command", "args", "env", "cwd"):
                if field_name in config:
                    issues.append(f"{name}.{field_name}")
        else:
            if not has_command:
                issues.append(f"{name}.command")
            if "url" in config:
                issues.append(f"{name}.url")
            if "headers" in config:
                issues.append(f"{name}.headers")

        args = config.get("args")
        if args is not None and (
            not isinstance(args, list)
            or not all(isinstance(value, str) for value in args)
        ):
            issues.append(f"{name}.args")
        for field_name in ("env", "headers"):
            values = config.get(field_name)
            if values is not None and (
                not isinstance(values, dict)
                or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in values.items()
                )
            ):
                issues.append(f"{name}.{field_name}")
        if "cwd" in config and not isinstance(config["cwd"], str):
            issues.append(f"{name}.cwd")
    return list(dict.fromkeys(issues))


def _load_mcp_manifest_servers(
    source: Path,
    report: Report,
    harness: str,
    target: Path,
) -> McpServerSet | None:
    """Load the authoritative manifest without Claude's local connector overlay."""
    manifest_data = _read_json_object(
        source,
        report,
        harness,
        target,
        "mcp manifest",
        missing_ok=False,
    )
    if manifest_data is None:
        return None
    raw_servers = manifest_data.get("mcpServers", {})
    hits: list[str] = []
    _scan_secrets(raw_servers, hits)
    if hits:
        kinds = ", ".join(sorted(set(hits)))
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"mcp manifest contains inline secrets ({kinds}); replace values with ${{VAR}} references",
                ok=False,
            )
        )
        return None
    manifest_servers = _extract_mcp_servers(
        manifest_data, report, harness, target, "mcp manifest"
    )
    if manifest_servers is None:
        return None

    credential_fields = _unreferenced_credential_fields(manifest_servers)
    if credential_fields:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "mcp manifest contains non-portable credential fields: "
                + ", ".join(credential_fields)
                + "; use ${VAR} in env/headers and keep OAuth state harness-native",
                ok=False,
            )
        )
        return None

    shape_fields = _mcp_server_shape_issues(manifest_servers)
    if shape_fields:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "mcp manifest contains invalid portable server fields: "
                + ", ".join(shape_fields),
                ok=False,
            )
        )
        return None

    reference_fields = _environment_reference_issues(manifest_servers)
    if reference_fields:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "mcp manifest contains non-portable environment references: "
                + ", ".join(reference_fields)
                + "; use same-name ${VAR} in env or ${VAR}/Bearer ${VAR} in Authorization",
                ok=False,
            )
        )
        return None
    return McpServerSet(
        servers=dict(manifest_servers),
        source_detail=f"manifest={len(manifest_servers)}",
    )


def _read_managed_mcp_names(
    path: Path,
    report: Report,
    harness: str,
    target: Path,
) -> set[str] | None:
    if path.is_symlink() or _is_reparse_point(path):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"MCP ownership sidecar must be a real file: {path}",
                ok=False,
            )
        )
        return None
    if not path.exists():
        return set()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"invalid MCP ownership sidecar {path}: {exc}",
                ok=False,
            )
        )
        return None
    if not isinstance(loaded, list) or not all(
        isinstance(name, str) for name in loaded
    ):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"MCP ownership sidecar is not a JSON string list: {path}",
                ok=False,
            )
        )
        return None
    return set(loaded)


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mcp_transaction_path(sidecar: Path) -> Path:
    return sidecar.with_name(f"{sidecar.name}.transaction")


def _recover_mcp_transaction(
    target: Path,
    sidecar: Path,
    report: Report,
    harness: str,
    dry_run: bool,
) -> bool:
    """Finish or roll back an interrupted target + ownership publication."""
    transaction = _mcp_transaction_path(sidecar)
    if transaction.is_symlink() or _is_reparse_point(transaction):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"MCP transaction journal must be a real file: {transaction}",
                ok=False,
            )
        )
        return False
    if not transaction.exists():
        return True
    if dry_run:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "pending MCP transaction requires a non-dry-run recovery",
                ok=False,
            )
        )
        return False
    if sidecar.is_symlink() or _is_reparse_point(sidecar):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"MCP ownership sidecar must be a real file: {sidecar}",
                ok=False,
            )
        )
        return False

    try:
        payload = json.loads(transaction.read_text(encoding="utf-8"))
        expected_fields = {
            "version",
            "targetBeforeSha256",
            "targetAfterSha256",
            "sidecarBeforePresent",
            "sidecarBeforeNames",
            "sidecarAfterNames",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ValueError("unexpected transaction fields")
        before_hash = payload["targetBeforeSha256"]
        after_hash = payload["targetAfterSha256"]
        before_present = payload["sidecarBeforePresent"]
        before_names = payload["sidecarBeforeNames"]
        after_names = payload["sidecarAfterNames"]
        digest = re.compile(r"[0-9a-f]{64}")
        if before_hash is not None and not (
            isinstance(before_hash, str) and digest.fullmatch(before_hash)
        ):
            raise ValueError("invalid pre-transaction target digest")
        if not (isinstance(after_hash, str) and digest.fullmatch(after_hash)):
            raise ValueError("invalid post-transaction target digest")
        if not isinstance(before_present, bool):
            raise ValueError("invalid pre-transaction sidecar state")
        if not all(
            isinstance(names, list) and all(isinstance(name, str) for name in names)
            for names in (before_names, after_names)
        ):
            raise ValueError("invalid transaction ownership names")
        if payload["version"] != 1:
            raise ValueError("unsupported transaction version")
        current_hash = _file_sha256(target)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"invalid MCP transaction journal {transaction}: {error}",
                ok=False,
            )
        )
        return False

    if current_hash == after_hash:
        recovered_names = after_names
        recovered_present = True
        direction = "completed"
    elif current_hash == before_hash:
        recovered_names = before_names
        recovered_present = before_present
        direction = "rolled back"
    else:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "MCP target changed outside an interrupted transaction; refusing ownership recovery",
                ok=False,
            )
        )
        return False

    if recovered_present:
        recovered_content = json.dumps(sorted(set(recovered_names)), indent=2) + "\n"
        _atomic_write_if_changed(sidecar, recovered_content, False, create_mode=0o600)
    elif sidecar.exists():
        sidecar.unlink()
    transaction.unlink()
    report.add(
        Change(
            harness,
            "sync_mcp",
            str(target),
            f"{direction} interrupted MCP ownership transaction",
        )
    )
    return True


def strategy_mcp_to_codex(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Upsert merged MCP servers into ~/.codex/config.toml inside a managed block.

    Preserves any pre-existing [mcp_servers.*] tables whose canonical name is
    NOT in the merged source set — those are user-managed and left outside the block.
    Canonical matching unifies drifted spellings (e.g., `sequential_thinking`
    in Codex gets replaced by the source set's `sequential-thinking`).
    """
    if target.is_symlink() or _is_reparse_point(target):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "managed config.toml must be a real file, not a link",
                ok=False,
            )
        )
        return
    server_set = _load_mcp_manifest_servers(source, report, harness, target)
    if server_set is None:
        return
    servers = server_set.servers
    if not target.exists():
        report.add(
            Change(
                harness, "error", str(target), "target config.toml missing", ok=False
            )
        )
        return

    try:
        target_before_bytes = target.read_bytes()
        raw = target_before_bytes.decode("utf-8")
    except UnicodeError as error:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"config.toml is not UTF-8: {error}",
                ok=False,
            )
        )
        return
    target_before_hash = hashlib.sha256(target_before_bytes).hexdigest()
    # Normalize CRLF to LF so idempotence comparisons hold on cross-platform
    # Syncthing'd files. We emit LF-only, so a CRLF config would be rewritten
    # every run otherwise.
    current = raw.replace("\r\n", "\n")
    try:
        parsed = tomllib.loads(current)
    except tomllib.TOMLDecodeError as e:
        report.add(
            Change(
                harness, "error", str(target), f"config.toml unparseable: {e}", ok=False
            )
        )
        return
    existing_names = set(parsed.get("mcp_servers", {}).keys())
    source_canonicals = {_canonical_mcp_name(n) for n in servers}
    preserved = {
        n for n in existing_names if _canonical_mcp_name(n) not in source_canonicals
    }

    try:
        new_block = _emit_codex_mcp_block(servers) if servers else ""
    except ValueError as error:
        report.add(Change(harness, "error", str(target), str(error), ok=False))
        return
    # Self-heal: unbalanced markers (e.g., a prior buggy run left an orphan
    # BEGIN/END) would make in-place replace target the wrong region. Strip
    # every marker line and treat as first-time.
    begin_count = current.count(_CODEX_MCP_BEGIN)
    end_count = current.count(_CODEX_MCP_END)
    if begin_count != end_count or begin_count > 1:
        current = "".join(
            line
            for line in current.splitlines(keepends=True)
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
        report.add(
            Change(
                harness, "error", str(target), f"generated TOML invalid: {e}", ok=False
            )
        )
        return

    if not dry_run:
        if _file_sha256(target) != target_before_hash:
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    "managed config.toml changed during projection; rerun",
                    ok=False,
                )
            )
            return
        backup_dir = target.parent / ".parity-backups"
        if (
            backup_dir.is_symlink()
            or _is_reparse_point(backup_dir)
            or (_safe_exists(backup_dir) and not backup_dir.is_dir())
        ):
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    f"Codex MCP recovery directory must be real: {backup_dir}",
                    ok=False,
                )
            )
            return
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(backup_dir, 0o700)
        backup = backup_dir / "config.toml.pre-mcp-sync"
        if (
            backup.is_symlink()
            or _is_reparse_point(backup)
            or (_safe_exists(backup) and not backup.is_file())
        ):
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    f"Codex MCP recovery backup must be a real file: {backup}",
                    ok=False,
                )
            )
            return
        if not backup.exists():
            _atomic_write_if_changed(backup, current, False, create_mode=0o600)
        if _file_sha256(target) != target_before_hash:
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    "managed config.toml changed before publication; rerun",
                    ok=False,
                )
            )
            return
        _atomic_write_if_changed(target, updated, False)

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
    sidecar_path: Path | None = None,
    sidecar_name: str = ".harness-sync-managed-mcp.json",
    create_mode: int = 0o644,
    enforce_target_mode: bool = False,
    preserve_local_server_fields: tuple[str, ...] = (),
) -> None:
    if target.is_symlink() or _is_reparse_point(target):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"managed {target.name} must be a real file, not a link",
                ok=False,
            )
        )
        return

    sidecar = sidecar_path or target.parent / sidecar_name
    if not _recover_mcp_transaction(target, sidecar, report, harness, dry_run):
        return

    server_set = _load_mcp_manifest_servers(source, report, harness, target)
    if server_set is None:
        return
    try:
        servers = (
            {
                name: _cursor_mcp_server(config)
                for name, config in server_set.servers.items()
            }
            if harness == "cursor"
            else server_set.servers
        )
    except ValueError as error:
        report.add(Change(harness, "error", str(target), str(error), ok=False))
        return
    target_before_hash: str | None
    if not target.exists():
        if create_if_missing:
            settings: dict[str, object] = {}
            target_before_hash = None
        else:
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    f"target {target.name} missing",
                    ok=False,
                )
            )
            return
    else:
        try:
            target_before_bytes = target.read_bytes()
            loaded = json.loads(target_before_bytes.decode("utf-8"))
            target_before_hash = hashlib.sha256(target_before_bytes).hexdigest()
        except (UnicodeError, json.JSONDecodeError) as e:
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    f"invalid {target.name}: {e}",
                    ok=False,
                )
            )
            return
        if not isinstance(loaded, dict):
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    f"{target.name} is not a JSON object",
                    ok=False,
                )
            )
            return
        settings = loaded

    managed_names = _read_managed_mcp_names(sidecar, report, harness, target)
    if managed_names is None:
        return

    existing = (
        settings.get("mcpServers")
        if isinstance(settings.get("mcpServers"), dict)
        else {}
    )
    source_canonicals = {_canonical_mcp_name(n) for n in servers}
    managed_canonicals = {_canonical_mcp_name(n) for n in managed_names}
    existing_by_canonical = {
        _canonical_mcp_name(name): cfg
        for name, cfg in existing.items()
        if isinstance(name, str) and isinstance(cfg, dict)
    }
    new_servers: dict[str, dict] = {}
    for name, cfg in existing.items():
        canon = _canonical_mcp_name(name)
        if canon in source_canonicals:
            continue  # drift-tolerant: will be overwritten with source spelling
        if canon in managed_canonicals:
            continue  # was ours, now gone from manifest → prune
        new_servers[name] = cfg  # preserve user-managed entry
    for name, cfg in servers.items():
        projected = dict(cfg)
        local = existing_by_canonical.get(_canonical_mcp_name(name))
        local_fields = (
            [
                field_name
                for field_name in preserve_local_server_fields
                if field_name in local
            ]
            if local is not None
            else []
        )
        if local_fields:
            same_endpoint = (
                local.get("type", "stdio") == cfg.get("type", "stdio")
                and local.get("url") == cfg.get("url")
                and local.get("command") == cfg.get("command")
            )
            if not same_endpoint:
                report.add(
                    Change(
                        harness,
                        "error",
                        str(target),
                        f"refusing to replace MCP server {name!r} endpoint while local auth metadata exists",
                        ok=False,
                    )
                )
                return
            for field_name in local_fields:
                projected[field_name] = local[field_name]
        new_servers[name] = projected

    new_settings = dict(settings)
    new_settings["mcpServers"] = new_servers
    new_json = json.dumps(new_settings, indent=2) + "\n"
    sidecar_content = json.dumps(sorted(servers.keys()), indent=2) + "\n"
    changed = _atomic_write_if_changed(
        target,
        new_json,
        True,
        create_mode=create_mode,
        enforce_mode=enforce_target_mode,
    )
    sidecar_changed = _atomic_write_if_changed(
        sidecar, sidecar_content, True, create_mode=0o600
    )

    if not dry_run and (changed or sidecar_changed):
        transaction = _mcp_transaction_path(sidecar)
        if _file_sha256(target) != target_before_hash:
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    f"managed {target.name} changed during projection; rerun",
                    ok=False,
                )
            )
            return
        target_after_hash = hashlib.sha256(new_json.encode()).hexdigest()
        sidecar_before_present = sidecar.exists()
        transaction_content = (
            json.dumps(
                {
                    "version": 1,
                    "targetBeforeSha256": target_before_hash,
                    "targetAfterSha256": target_after_hash,
                    "sidecarBeforePresent": sidecar_before_present,
                    "sidecarBeforeNames": sorted(managed_names),
                    "sidecarAfterNames": sorted(servers.keys()),
                },
                indent=2,
            )
            + "\n"
        )
        _atomic_write_if_changed(
            transaction, transaction_content, False, create_mode=0o600
        )
        if _file_sha256(target) != target_before_hash:
            transaction.unlink()
            report.add(
                Change(
                    harness,
                    "error",
                    str(target),
                    f"managed {target.name} changed before publication; rerun",
                    ok=False,
                )
            )
            return
        _atomic_write_if_changed(
            target,
            new_json,
            False,
            create_mode=create_mode,
            enforce_mode=enforce_target_mode,
        )
        _atomic_write_if_changed(sidecar, sidecar_content, False, create_mode=0o600)
        if (
            _file_sha256(target) != target_after_hash
            or _file_sha256(sidecar)
            != hashlib.sha256(sidecar_content.encode()).hexdigest()
        ):
            raise OSError("MCP target or ownership sidecar changed during publication")
        transaction.unlink()

    if changed or sidecar_changed:
        report.add(
            Change(
                harness,
                "sync_mcp",
                str(target),
                f"{len(servers)} server{'s' if len(servers) != 1 else ''} ({server_set.source_detail})",
            )
        )
    else:
        report.add(
            Change(
                harness,
                "skip",
                str(target),
                f"mcp servers already current ({server_set.source_detail})",
            )
        )


def strategy_mcp_to_claude(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Project manifest-owned servers into Claude's user registry."""
    _sync_mcp_to_json_config(
        source,
        target,
        report,
        harness,
        dry_run,
        create_if_missing=True,
        sidecar_path=_claude_mcp_managed_path(target),
        create_mode=0o600,
    )


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


def strategy_mcp_to_omp(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Upsert merged MCP definitions into OMP without touching its auth store."""
    try:
        if dry_run:
            _sync_mcp_to_json_config(
                source,
                target,
                report,
                harness,
                True,
                create_if_missing=True,
                create_mode=0o600,
                enforce_target_mode=True,
                preserve_local_server_fields=("auth", "oauth"),
            )
            return
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with _omp_config_lock(target):
            _sync_mcp_to_json_config(
                source,
                target,
                report,
                harness,
                False,
                create_if_missing=True,
                create_mode=0o600,
                enforce_target_mode=True,
                preserve_local_server_fields=("auth", "oauth"),
            )
    except (OSError, TimeoutError) as error:
        report.add(
            Change(
                harness,
                "error",
                str(target),
                f"Oh My Pi MCP projection aborted safely: {error}",
                ok=False,
            )
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
        report.add(
            Change(harness, "error", str(target), f"source missing: {source}", ok=False)
        )
        return
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.add(
            Change(
                harness, "error", str(target), f"invalid settings.json: {e}", ok=False
            )
        )
        return

    hooks_block = settings.get("hooks", {})
    if not isinstance(hooks_block, dict):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "settings.json 'hooks' is not an object",
                ok=False,
            )
        )
        return

    def _rewrite(obj: object) -> object:
        if isinstance(obj, dict):
            return {k: _rewrite(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_rewrite(v) for v in obj]
        if isinstance(obj, str):
            return obj.replace("$HOME/.claude/hooks/", "$HOME/.codex/hooks/").replace(
                "~/.claude/hooks/", "~/.codex/hooks/"
            )
        return obj

    rewritten = _rewrite(hooks_block)
    content = json.dumps({"hooks": rewritten}, indent=2) + "\n"
    changed = _write_if_changed(target, content, dry_run)
    report.add(
        Change(
            harness,
            "translate" if changed else "skip",
            str(target),
            "codex hooks.json from claude settings.json"
            if changed
            else "already current",
        )
    )


def strategy_translate_bootstrap_hook_to_cursor_json(
    source: Path, target: Path, report: Report, harness: str, dry_run: bool, **_: object
) -> None:
    """Project Claude's bootstrap SessionStart hook into Cursor's user hooks.json."""
    if not source.exists():
        report.add(
            Change(harness, "error", str(target), f"source missing: {source}", ok=False)
        )
        return
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        report.add(
            Change(
                harness, "error", str(target), f"invalid settings.json: {e}", ok=False
            )
        )
        return
    if target.is_symlink() or _is_reparse_point(target):
        report.add(
            Change(
                harness,
                "error",
                str(target),
                "Cursor hooks target must be a real file",
                ok=False,
            )
        )
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
            if (
                not isinstance(command, str)
                or "bootstrap-agent-config.sh" not in command
            ):
                continue
            cursor_hook: dict[str, object] = {"command": managed_command}
            timeout = hook.get("timeout")
            if isinstance(timeout, int):
                cursor_hook["timeout"] = max(1, (timeout + 999) // 1000)
            managed_hooks.append(cursor_hook)

    try:
        existing = (
            json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
        )
    except json.JSONDecodeError as e:
        report.add(
            Change(harness, "error", str(target), f"invalid hooks.json: {e}", ok=False)
        )
        return
    if not isinstance(existing, dict):
        report.add(
            Change(
                harness, "error", str(target), "hooks.json is not an object", ok=False
            )
        )
        return

    hooks = existing.get("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
    current_session = hooks.get("sessionStart", [])
    if not isinstance(current_session, list):
        current_session = []
    preserved = [
        hook
        for hook in current_session
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
    try:
        changed = _atomic_write_if_changed(target, content, dry_run)
    except OSError as error:
        report.add(Change(harness, "error", str(target), str(error), ok=False))
        return
    report.add(
        Change(
            harness,
            "translate" if changed else "skip",
            str(target),
            "cursor bootstrap hooks.json" if changed else "already current",
        )
    )


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
        report.add(
            Change(harness, "error", str(target), f"source missing: {source}", ok=False)
        )
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
        entries.append(
            {
                "name": skill_dir.name,
                "description": meta.get(
                    "description", f"Load the {skill_dir.name} skill."
                ),
                "path": f"{mount}/{skill_dir.name}",
            }
        )
    content = json.dumps(entries, indent=2) + "\n"
    changed = _write_if_changed(target, content, dry_run)
    report.add(
        Change(
            harness,
            "translate" if changed else "skip",
            str(target),
            f"{len(entries)} entries" if changed else "already current",
        )
    )


# ---------------------------------------------------------------------------
# Harness specs — declarative. Add a dict here to teach the engine a new harness.
# ---------------------------------------------------------------------------


def _claude_memory() -> Path:
    # Claude encodes the session CWD into the project dir by replacing both
    # '/' and '.' with '-'. Sessions run from $HOME, so /Users/foo.bar
    # becomes projects/-Users-foo-bar/memory. The dir gets auto-cleaned when
    # empty. The symlink strategy creates it only during a real apply so spec
    # construction and dry runs remain side-effect-free.
    # Match how Claude Code encodes the project dir: replace path separators and
    # '.' with '-'. On Windows that includes '\' and the drive ':' (C:\Users\x
    # -> C--Users-x); on POSIX only '/' and '.' ('\\'/':' can be literal
    # filename chars there, so leaving them preserves original behavior).
    seps = r"[/.\\:]" if os.name == "nt" else r"[/.]"
    encoded = re.sub(seps, "-", str(Path.home()))
    return CLAUDE_HOME / "projects" / encoded / "memory"


def _harness_specs() -> list[dict]:
    return [
        {
            "name": "config_repo",
            "home": CONFIG_HOME,
            "role": "source",
            "detect": lambda h: (
                (h / "CLAUDE.md").exists()
                and (h / "skills").is_dir()
                and (h / "hooks").is_dir()
            ),
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
                {
                    "strategy": strategy_symlink_preserve_real,
                    "source": MCP_MANIFEST_PATH,
                    "target_rel": "mcp-servers.json",
                },
                {
                    "strategy": strategy_mcp_to_claude,
                    "source": MCP_MANIFEST_PATH,
                    "target": CLAUDE_CONFIG_JSON_PATH,
                    "capability": "mcp",
                },
            ],
        },
        {
            "name": "codex",
            "home": Path.home() / ".codex",
            "role": "symlink",
            # Codex signature: either its config file or the AGENTS.md (possibly already our symlink)
            "detect": lambda h: (
                (h / "config.toml").exists() or (h / "AGENTS.md").exists()
            ),
            "artifacts": [
                {
                    "strategy": strategy_symlink,
                    "source": CONFIG_HOME / "agents",
                    "target_rel": "agents",
                },
                {
                    "strategy": strategy_symlink,
                    "source": CONFIG_HOME / "AGENTS.md",
                    "target_rel": "AGENTS.md",
                },
                {
                    "strategy": strategy_symlink,
                    "source": _claude_memory(),
                    "target_rel": "memories",
                    "opts": {"create_source_directory": True},
                },
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "skills",
                    "target_rel": "skills",
                    "opts": {"preserve": [".system"]},
                },
                # Codex 0.117+ removed ~/.codex/prompts/ discovery; project every
                # command as a Codex skill so it remains reachable.
                # See ../references/codex.md "Command Projection" section.
                {
                    "strategy": strategy_command_to_codex_skill,
                    "source": CONFIG_HOME / "commands",
                    "target_rel": "skills",
                },
                # Hook scripts are harness-agnostic, so per-child symlinks keep
                # them in sync without copying.
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "hooks",
                    "target_rel": "hooks",
                },
                # Codex reads `hooks.json` directly; shared hook wiring lives in
                # repo `settings.json`. Translate the hooks block over, rewriting
                # path prefixes to keep Codex's config self-contained.
                {
                    "strategy": strategy_translate_hooks_to_codex_json,
                    "source": CONFIG_HOME / "settings.json",
                    "target_rel": "hooks.json",
                },
                {
                    "strategy": strategy_mcp_to_codex,
                    "source": MCP_MANIFEST_PATH,
                    "target_rel": "config.toml",
                    "capability": "mcp",
                },
            ],
        },
        {
            "name": "gemini",
            "home": Path.home() / ".gemini",
            "role": "translate",
            "detect": lambda h: (h / "settings.json").exists(),
            "artifacts": [
                {
                    "strategy": strategy_translate_commands_to_toml,
                    "source": CONFIG_HOME / "commands",
                    "target_rel": "commands",
                },
                {
                    "strategy": strategy_translate_skills_to_toml,
                    "source": CONFIG_HOME / "skills",
                    "target_rel": "commands",
                    "opts": {"prefix": "skill-", "skill_mount": "~/.gemini/skills"},
                },
                {
                    "strategy": strategy_generate_skill_index,
                    "source": CONFIG_HOME / "skills",
                    "target_rel": "skills/index.json",
                    "opts": {"skill_mount": "~/.gemini/skills"},
                },
                {
                    "strategy": strategy_mcp_to_gemini,
                    "source": MCP_MANIFEST_PATH,
                    "target_rel": "settings.json",
                    "capability": "mcp",
                },
            ],
        },
        {
            "name": "cursor",
            "home": CURSOR_HOME,
            "role": "symlink",
            # Desktop and Agent CLI share user-level agents, skills, hooks, and MCP.
            # cli-config.json may live outside ~/.cursor through CLI/XDG overrides.
            "detect": lambda h: (
                (h / "argv.json").exists()
                or (h / "skills-cursor").is_dir()
                or CURSOR_CLI_CONFIG_PATH.is_file()
            ),
            "artifacts": [
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "agents",
                    "target_rel": "agents",
                },
                # Cursor reserves ~/.cursor/skills-cursor for built-ins. User/project
                # skills belong under ~/.cursor/skills, so project Claude skills there.
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "skills",
                    "target_rel": "skills",
                },
                {
                    "strategy": strategy_command_to_cursor_skill,
                    "source": CONFIG_HOME / "commands",
                    "target_rel": "skills",
                },
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "hooks",
                    "target_rel": "hooks",
                },
                {
                    "strategy": strategy_translate_bootstrap_hook_to_cursor_json,
                    "source": CONFIG_HOME / "settings.json",
                    "target_rel": "hooks.json",
                },
                {
                    "strategy": strategy_mcp_to_cursor,
                    "source": MCP_MANIFEST_PATH,
                    "target_rel": "mcp.json",
                    "capability": "mcp",
                },
            ],
        },
        {
            "name": "pi",
            "home": PI_AGENT_HOME,
            "role": "symlink",
            "detect": lambda h: (
                (h / "settings.json").is_file()
                or (h / "auth.json").is_file()
                or (h / "sessions").is_dir()
            ),
            "unsupported_capabilities": {
                "mcp": "Pi core has no native MCP; manage a reviewed Pi extension separately",
            },
            "artifacts": [
                {
                    "strategy": strategy_symlink,
                    "source": CONFIG_HOME / "AGENTS.md",
                    "target_rel": "AGENTS.md",
                },
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "commands",
                    "target_rel": "prompts",
                },
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "skills",
                    "target_rel": "skills",
                },
            ],
        },
        {
            "name": "ohmypi",
            "home": OMP_AGENT_HOME,
            "role": "hybrid",
            "detect": lambda h: (
                OMP_PROFILE_VALID
                and any(
                    (h / filename).is_file()
                    for filename in ("config.yml", "config.yaml", "agent.db")
                )
            ),
            "artifacts": [
                {
                    "strategy": strategy_symlink,
                    "source": CONFIG_HOME / "AGENTS.md",
                    "target_rel": "AGENTS.md",
                },
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "agents",
                    "target_rel": "agents",
                },
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "commands",
                    "target_rel": "commands",
                },
                {
                    "strategy": strategy_symlink_children,
                    "source": CONFIG_HOME / "skills",
                    "target_rel": "skills",
                },
                {
                    "strategy": strategy_mcp_to_omp,
                    "source": MCP_MANIFEST_PATH,
                    "target_rel": "mcp.json",
                    "capability": "mcp",
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def sync(
    only: set[str] | None = None,
    only_capability: str | None = None,
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
        if not spec["detect"](home):
            report.harnesses_skipped.append(f"{name} (signature not found in {home})")
            continue
        if spec["role"] == "source":
            report.harnesses_detected.append(f"{name} [source]")
            continue

        report.harnesses_detected.append(f"{name} [{spec['role']}]")
        artifacts = [
            artifact
            for artifact in spec["artifacts"]
            if only_capability is None or artifact.get("capability") == only_capability
        ]
        if only_capability is not None and not artifacts:
            reason = spec.get("unsupported_capabilities", {}).get(
                only_capability, f"capability {only_capability!r} is not supported"
            )
            report.add(Change(name, "skip", str(home), reason))
            continue
        for art in artifacts:
            target = art["target"] if "target" in art else home / art["target_rel"]
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
                report.add(
                    Change(
                        name,
                        "error",
                        str(target),
                        f"{type(exc).__name__}: {exc}",
                        ok=False,
                    )
                )

    if verbose:
        for c in report.changes:
            mark = "OK" if c.ok else "FAIL"
            print(
                f"[{mark}] {c.harness:7} {c.action:10} {c.target}"
                + (f"  ({c.detail})" if c.detail else ""),
                file=sys.stderr,
            )

    return report


def list_detected() -> None:
    for spec in _harness_specs():
        home = spec["home"]
        present = spec["detect"](home)
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
    p.add_argument(
        "--dry-run", action="store_true", help="report changes, write nothing"
    )
    p.add_argument("-v", "--verbose", action="store_true", help="trace each action")
    p.add_argument(
        "--only", help="comma-separated harness names (e.g. codex,cursor,pi,ohmypi)"
    )
    p.add_argument(
        "--only-capability",
        choices=("mcp",),
        help="sync only one capability across selected harnesses",
    )
    p.add_argument(
        "--list", action="store_true", help="list detected harnesses and exit"
    )
    args = p.parse_args()

    if args.list:
        list_detected()
        return 0

    only = set(args.only.split(",")) if args.only else None
    report = sync(
        only=only,
        only_capability=args.only_capability,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )
    print_summary(report, args.dry_run)
    return 1 if report.errors() else 0


if __name__ == "__main__":
    sys.exit(main())

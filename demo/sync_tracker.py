"""State-tracking wrapper around the deterministic harness_sync engine.

Drives the REAL engine one harness at a time through the four phases the
meta-agent-sync command defines — detect, plan, apply, verify — and yields one
structured event per state transition so a UI can render live progress.

No engine logic is duplicated. Detection reuses each spec's own `detect`
predicate; every phase is a real `harness_sync.sync()` call:

    plan    = sync(only={h}, dry_run=True)     # what would change
    apply   = sync(only={h}, dry_run=live)     # write it (or simulate in dry mode)
    verify  = sync(only={h}, dry_run=True)     # re-plan; live apply should collapse to all-skip

Idempotence is *observed*, not asserted: verify reports whether the re-plan
collapsed to skip-only. The engine spends zero LLM tokens — it is plain Python.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Iterator

# The engine imports stdlib tomllib; on Python < 3.11 it re-execs itself under a
# newer interpreter, which would hijack the server process and run a real sync
# instead of serving. Fail loudly here rather than let that happen silently.
if sys.version_info < (3, 11):
    raise RuntimeError(
        "the meta-agent-sync demo needs Python 3.11+ (the engine requires stdlib "
        f"tomllib); this interpreter is {sys.version_info.major}.{sys.version_info.minor}. "
        "Launch via ./run.sh (it selects one) or run: python3.11 server.py"
    )

# The demo lives at demo/ in the repo root; the engine ships in the plugin's
# skill. parents[1] == repo root.
_REPO = Path(__file__).resolve().parents[1]
_ENGINE = _REPO / "skills" / "harness-sync" / "scripts" / "harness_sync.py"

_spec = importlib.util.spec_from_file_location("harness_sync", _ENGINE)
if _spec is None or _spec.loader is None:  # pragma: no cover - import wiring
    raise RuntimeError(f"cannot load engine at {_ENGINE}")
hs = importlib.util.module_from_spec(_spec)
sys.modules["harness_sync"] = hs
_spec.loader.exec_module(hs)

STEPS = ("plan", "apply", "verify")
_HOME = str(Path.home())

HARNESS_DETAILS = {
    "config_repo": {
        "label": "Source of truth",
        "summary": "Canonical agents, skills, commands, hooks, MCP, and memory live here.",
    },
    "claude": {
        "label": "Native links",
        "summary": "Claude mostly consumes repo artifacts directly, so its verified count is small.",
    },
    "codex": {
        "label": "Projected runtime",
        "summary": "Codex needs skills, commands, hooks, memory, and MCP projected into its own layout.",
    },
    "gemini": {
        "label": "Translated runtime",
        "summary": "Gemini receives TOML/JSON translations because its harness shape is different.",
    },
    "cursor": {
        "label": "Projected IDE runtime",
        "summary": "Cursor gets skills, commands, hooks, and MCP materialized for its IDE agent.",
    },
}

FAMILY_ORDER = ("guidance", "skills", "commands", "hooks", "mcp", "memory", "settings", "other")


def _short(path: str) -> str:
    return path.replace(_HOME, "~", 1) if path.startswith(_HOME) else path


def _family(target: str) -> str:
    path = target.lower()
    name = Path(path).name
    if "skill-" in name or "/skills/" in path or path.endswith("/skills") or path.endswith("skills/index.json"):
        return "skills"
    if "/commands/" in path or path.endswith("/commands"):
        return "commands"
    if name in {"mcp.json", "mcp-servers.json", "config.toml"} or "mcp" in name:
        return "mcp"
    if ".gemini" in path and name == "settings.json":
        return "mcp"
    if "/hooks/" in path or name in {"hooks.json", "settings.json"}:
        return "hooks"
    if "memor" in path:
        return "memory"
    if name in {"agents", "agents.md", "claude.md"}:
        return "guidance"
    if name.endswith((".json", ".toml", ".md")):
        return "settings"
    return "other"


def _detail_for(name: str) -> dict:
    return HARNESS_DETAILS.get(name, {"label": "Harness", "summary": ""})


def detect() -> dict:
    """Enumerate harnesses without running any sync (drives the empty state)."""
    source = None
    targets = []
    for spec in hs._harness_specs():
        home = spec["home"]
        present = home.exists() and bool(spec["detect"](home))
        detail = _detail_for(spec["name"])
        entry = {"name": spec["name"], "role": spec["role"],
                 "home": _short(str(home)), "present": present,
                 "label": detail["label"], "summary": detail["summary"],
                 "artifact_rules": len(spec.get("artifacts", []))}
        if spec["role"] == "source":
            source = entry
        else:
            targets.append(entry)
    return {"source": source, "targets": targets}


def _summarize(report: "hs.Report") -> dict:
    counts = report.by_action()
    family_counts: dict[str, dict[str, int]] = {}
    for change in report.changes:
        family = _family(change.target)
        bucket = family_counts.setdefault(family, {"total": 0})
        bucket["total"] += 1
        bucket[change.action] = bucket.get(change.action, 0) + 1
    families = [
        {"name": name, **family_counts[name]}
        for name in FAMILY_ORDER
        if name in family_counts
    ]
    return {
        "counts": counts,
        "families": families,
        "total": len(report.changes),
        "errors": len(report.errors()),
        "to_change": len(report.changes) - counts.get("skip", 0),
        "changes": [
            {"action": c.action, "target": _short(c.target),
             "detail": c.detail, "ok": c.ok}
            for c in report.changes
        ],
    }


def run_events(mode: str = "live", delay_ms: int = 400) -> Iterator[dict]:
    """Yield one event dict per state transition. `mode` is 'live' or 'dry'."""
    live = mode == "live"
    delay = max(0, min(delay_ms, 5000)) / 1000.0
    t0 = time.monotonic()

    yield {"type": "run_start", "mode": "live" if live else "dry", "delay_ms": delay_ms}

    # The engine sys.exit(2)s on a missing/incomplete repo; pre-check so the
    # server never dies mid-stream.
    repo = hs.CONFIG_HOME
    if not ((repo / "CLAUDE.md").exists() and (repo / "skills").is_dir()
            and (repo / "hooks").is_dir()):
        yield {"type": "fatal", "msg": f"config repo missing or incomplete at {_short(str(repo))}"}
        return

    # ---- DETECT ---------------------------------------------------------
    yield {"type": "phase", "phase": "detect", "state": "start"}
    det = detect()
    if det["source"]:
        yield {"type": "source", **det["source"]}
    targets = [t for t in det["targets"] if t["present"]]
    skipped = [t for t in det["targets"] if not t["present"]]
    for t in targets:
        time.sleep(delay)
        yield {"type": "harness_detected", **t}
    for s in skipped:
        yield {"type": "harness_skipped", **s}
    yield {"type": "phase", "phase": "detect", "state": "done",
           "detected": [t["name"] for t in targets],
           "skipped": [s["name"] for s in skipped]}

    # ---- PLAN / APPLY / VERIFY, per harness -----------------------------
    grand: dict[str, int] = {}
    idempotent = True if live else None  # can only be proven when we actually apply

    for t in targets:
        h = t["name"]
        planned = 0
        for step in STEPS:
            yield {"type": "step", "harness": h, "step": step, "state": "running"}
            time.sleep(delay)
            dry = step in ("plan", "verify") or not live
            try:
                report = hs.sync(only={h}, dry_run=dry, verbose=False)
            except SystemExit as exc:  # engine fatal, already guarded but be safe
                yield {"type": "step", "harness": h, "step": step, "state": "error",
                       "status": "error",
                       "summary": {"error": f"engine exit {exc.code}"}}
                idempotent = False
                break

            summ = _summarize(report)
            status = "ok"
            if summ["errors"]:
                status = "error"
                idempotent = False
            elif step == "plan":
                planned = summ["to_change"]
            elif step == "verify" and live and summ["to_change"] > 0:
                status = "drift"       # re-plan did not collapse to skip
                idempotent = False

            for action, n in summ["counts"].items():
                grand[action] = grand.get(action, 0) + n

            yield {"type": "step", "harness": h, "step": step, "state": "done",
                   "status": status, "summary": summ}

        yield {"type": "harness_done", "harness": h, "planned": planned}

    yield {"type": "run_done", "mode": "live" if live else "dry",
           "totals": grand, "idempotent": idempotent,
           "duration_ms": int((time.monotonic() - t0) * 1000),
           "harnesses": len(targets)}

"""Where smoke result files live and how they are read, written and amended.

One file per revision per worker: ``<smoke dir>/<revision>/<worker>.json``.
The smoke dir is ``$AI_SHUFFLE_SMOKE_DIR`` when set, else ``smoke/`` under the
worker's data dir (``$DATA_DIR``, ``/data`` in Docker, otherwise the platform
default the app uses). Result files never hold credentials.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

STATUSES = ("pass", "fail", "not_run")
REVISION = re.compile(r"[0-9a-fA-F]{7,64}")


def app_data_dir() -> Path:
    if os.environ.get("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    if Path("/.dockerenv").exists():
        return Path("/data")
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE") or Path.home()) / ".ai-shuffle"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ai-shuffle"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "ai-shuffle"


def smoke_dir(override: str | None = None) -> Path:
    if override:
        return Path(override)
    if os.environ.get("AI_SHUFFLE_SMOKE_DIR"):
        return Path(os.environ["AI_SHUFFLE_SMOKE_DIR"])
    return app_data_dir() / "smoke"


def safe_name(name: str) -> str:
    """A worker label as a file name (labels are user-chosen device names)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._")
    return cleaned or "worker"


def result_path(base: Path, revision: str, worker: str, partial: str | None = None) -> Path:
    if not REVISION.fullmatch(revision or ""):
        raise ValueError(f"not a git revision: {revision!r}")
    name = safe_name(worker) + (f"-partial-{safe_name(partial)}" if partial else "")
    return base / revision.lower() / f"{name}.json"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write(path: Path, result: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record(path: Path, row_id: str, status: str, evidence: str, by: str = "manual") -> dict:
    """Add a browser/owner row result to an existing run file.

    Script rows cannot be overwritten: they are only ever written by a run.
    """
    if status not in ("pass", "fail"):
        raise ValueError("status must be pass or fail")
    if not (evidence or "").strip():
        raise ValueError("evidence is required (session id, snapshot path or what you saw)")
    data = read(path)
    if data is None:
        raise FileNotFoundError(f"no result file at {path}; run the smoke first")
    for row in data.get("rows", []):
        if row.get("id") != row_id:
            continue
        if row.get("kind") == "script":
            raise ValueError(f"{row_id} is a script row; rerun the smoke instead of recording it")
        row.update(status=status, reason=None, recorded={"by": by, "at": now_iso()},
                   evidence={"note": evidence.strip()})
        write(path, data)
        return row
    raise KeyError(f"{row_id} is not in {path.name}")

"""Bounded, read-only runtime facts and credential-free provisioning receipts.

Filesystem presence is not proof a build works. Never run repository interpreters,
package hooks or installers while gathering prompt context.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_RECEIPT = "fluid-runtime-provisioning.json"
_KEYS = ("linked", "installed", "synced", "skipped", "errors")


def _receipt_path(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", _RECEIPT], cwd=root,
            capture_output=True, text=True, timeout=1,
        )
        if result.returncode:
            return None
        path = Path(result.stdout.strip())
        return path if path.is_absolute() else root / path
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def record_provisioning(cwd: str, summary: dict) -> None:
    """Persist only outcome counts, not command output, paths or secret values.

    Stored in this worktree's git administration directory, never in tracked
    source or a credential file. Failure must not invalidate a checkout.
    """
    temporary = None
    try:
        target = _receipt_path(Path(cwd))
        if target is None or target.is_symlink():
            return
        counts = {key: len(summary.get(key, [])) for key in _KEYS}
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, delete=False,
            prefix=".fluid-runtime-", suffix=".tmp",
        ) as output:
            temporary = Path(output.name)
            json.dump({"version": 1, "counts": counts}, output)
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _provisioning_fact(root: Path) -> str | None:
    try:
        path = _receipt_path(root)
        if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return None
        receipt = json.loads(path.read_text(encoding="utf-8"))
        counts = receipt["counts"]
        if receipt.get("version") != 1 or not all(
            type(counts.get(key)) is int and 0 <= counts[key] <= 10000 for key in _KEYS
        ):
            return None
        return "Last provisioning outcome: " + ", ".join(
            f"{key}={counts[key]}" for key in _KEYS
        ) + " (historical receipt; current presence is listed below)"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def runtime_facts(cwd: str) -> str:
    """Live availability facts; refresh on user turns, not in the cached prefix."""
    root = Path(cwd)
    lines = [f"Worker Python: {json.dumps(sys.executable)}"]
    try:
        pytest_found = importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError, AttributeError):
        pytest_found = False
    lines.append("pytest in worker Python: " + ("discoverable (not executed)" if pytest_found else "not found"))
    pytest_cli = shutil.which("pytest")
    if pytest_cli:
        lines.append(f"pytest executable on PATH: {json.dumps(pytest_cli)} (not executed)")
    candidates = [root / env / folder / name for env in ("venv", ".venv")
                  for folder, name in (("Scripts", "python.exe"), ("bin", "python"))]
    present = [json.dumps(str(path)) for path in candidates if path.is_file()]
    lines.append("Project Python candidates: " + (", ".join(present) + " (not executed)" if present else "none in venv/.venv"))
    receipt = _provisioning_fact(root)
    if receipt:
        lines.append(receipt)
    try:
        packages = ([root] if (root / "package.json").is_file() else []) + [
            child for child in sorted(root.iterdir())
            if child.is_dir() and not child.name.startswith(".")
            and child.name != "node_modules" and (child / "package.json").is_file()
        ]
        for package in packages[:8]:
            label = str(package.relative_to(root))
            modules = package / "node_modules"
            if not modules.is_dir():
                state = "MISSING; setup required before its scripts can run"
            elif next(modules.iterdir(), None) is None:
                state = "EMPTY; setup required before its scripts can run"
            else:
                state = "present (not validated)"
            lines.append(f"Node dependencies {json.dumps(label)}: {state}")
        if len(packages) > 8:
            lines.append("Additional package directories omitted; inspect before running their scripts.")
        if (root / "app").is_dir() and (root / "frontend" / "package.json").is_file():
            assets = root / "app" / "static"
            if not assets.is_dir():
                asset_state = "MISSING"
            elif not (assets / "index.html").is_file():
                asset_state = "INCOMPLETE; index.html missing"
            else:
                asset_state = "present (build identity unverified)"
            lines.append("Built frontend app/static: " + asset_state)
    except OSError:
        lines.append("Dependency presence could not be inspected.")
    lines.append("Availability is not a successful test run. Missing dependencies require explicit setup; never copy credentials or assume shared links exist.")
    return "\n".join(lines)

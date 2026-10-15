"""Isolated worker setup: interpreter receipt and owner-opted-in tracked setup.

Local git config (not a tracked script's mere presence) authorizes automatic
setup. Commands always run in the new checkout, through Monitor's existing
registry, cancellation, bounded deadline and completion wake machinery.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace


def _local_setting(path: str, key: str) -> str:
    from app.core.git_service import git_service

    try:
        result = git_service._run_git(path, ["config", "--local", "--get", key], timeout=5)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def runtime_receipt(source: str, checkout: str) -> dict:
    """Record an equivalent interpreter without linking/copying any environment."""
    configured = _local_setting(source, "fluid.workerPython")
    candidates = ([Path(configured)] if configured else []) + [
        Path(source) / env / folder / name
        for env in (".venv", "venv")
        for folder, name in (("Scripts", "python.exe"), ("bin", "python"))
    ]
    python = next((str(p) for p in candidates if p.is_absolute() and p.is_file()), sys.executable)
    root = Path(checkout)
    commands = []
    # Inspect only the root and immediate package folders; never walk dependencies.
    try:
        folders = [root, *sorted(p for p in root.iterdir()
                                if p.is_dir() and not p.is_symlink() and not p.name.startswith("."))]
    except OSError:
        folders = [root]
    for folder in folders:
        if not (folder / "package-lock.json").is_file():
            continue
        command = "npm ci"
        package = folder / "package.json"
        try:
            data = json.loads(package.read_text(encoding="utf-8")) if package.stat().st_size < 65536 else {}
            if "@sveltejs/kit" in {**data.get("dependencies", {}), **data.get("devDependencies", {})}:
                command += " && npx svelte-kit sync"
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        commands.append({"command": command, "cwd": str(folder)})
    return {
        "python": python,
        "python_note": "Existing interpreter, not a dependency compatibility check; never pip install into the parent's environment.",
        "setup_status": (
            "configured; scheduled after initial turn submission if shell tools permit. "
            "Find the task ID in TaskList or its completion wake; do not race setup."
            if _local_setting(source, "fluid.workerSetupCommand") else
            "not configured; automatic setup requires local git config fluid.workerSetupCommand"
        ),
        "frontend_monitor_inputs": commands,
    }


async def start_worker_runtime(checkout, session_id, profile_id, tool_allowlist=None, tool_denylist=None):
    """Start, never join, an owner-configured recipe; report failures without secrets."""
    from app.core.monitor_spawn import make_fluid_monitor_spawner
    from app.core.tool_access import resolve_tool_access, shell_companion_tools, tool_permitted
    from app.db import database

    if checkout is None or checkout.runtime is None:
        return
    command = await asyncio.to_thread(_local_setting, checkout.path, "fluid.workerSetupCommand")
    if not command:
        return
    profile = await asyncio.to_thread(database.get_profile, profile_id) or {}
    access = resolve_tool_access(profile.get("config") or {})
    blocked = list(access.disallowed or ()) + list(tool_denylist or ())
    allowed = shell_companion_tools(tool_allowlist or access.allowed, blocked)
    # Use the same builtin spellings on both legs. Explicit blocks win.
    bare = lambda name: str(name).removeprefix("mcp__fluid_bundled__").removeprefix("mcp__builtins__")
    allowed = [bare(name) for name in allowed] if allowed else None
    blocked = [bare(name) for name in blocked]
    if not all(tool_permitted(name, allowed, blocked) for name in ("Bash", "Monitor")):
        checkout.runtime["setup_status"] = "skipped: agent shell tools are restricted"
        return
    try:
        spawn = make_fluid_monitor_spawner(session_id, max_budget_seconds=600)
        outcome = await spawn({"command": command, "cwd": checkout.path},
                              SimpleNamespace(cwd=checkout.path, extra={}))
        receipt = getattr(outcome, "background_task", None)
        if not getattr(receipt, "task_id", None):
            checkout.runtime["setup_status"] = "failed to start; no setup task was registered"
            return
        checkout.runtime.update(setup_status="started at launch; completion arrives as a Monitor wake (10 minute maximum)",
                                setup_task_id=receipt.task_id)
    except Exception:
        # Command output and exception text may include owner secrets. Monitor
        # owns output; the persistent worker instructions carry only status.
        checkout.runtime["setup_status"] = "failed to start; use Monitor for setup"

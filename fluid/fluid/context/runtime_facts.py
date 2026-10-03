"""Bounded, read-only runtime facts and credential-free provisioning receipts.

Filesystem presence is not proof a build works. Never run repository interpreters,
package hooks or installers while gathering prompt context.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_log = logging.getLogger(__name__)

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


# ─── mechanical fact harvest ─────────────────────────────────────────────────
#
# The note above is per turn and per process. The harvesters below turn the
# same observations ("no pytest in the worker Python", "this worktree has no
# node_modules") into FactStore entries with ``provenance="tool"`` and a stable
# key, so a later session on the same project sees them in the # Memory
# section (rendered with a "confirm with Remember" hint) without the model
# or the synthesizer having to do anything.

# Keys are stable per fact class so a re-harvest updates in place. They must
# match facts.save's key pattern ``[a-zA-Z0-9_.:-]{1,96}``.
_KEY_PYTEST = "env.pytest"
_KEY_NODE_MODULES = "env.node_modules"
_KEY_BROWSER_HOST = "env.browser_host"

# The app's browser tools answer a loopback URL with "Browse http://<host>:
# <port> instead" when they run on another machine; the host is the durable
# part, the port is whatever server the session happened to start.
_BROWSE_INSTEAD_RE = re.compile(r"[Bb]rowse\s+(https?://[^\s/]+)(?:[/\s]|$)[^\n]*?\binstead\b")
_BROWSER_TOOL_RE = re.compile(r"(?:^|__)browser_[a-z_]+$")


def _safe_key(suffix: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]", "-", suffix)[:96]


def harvest_environment_facts(cwd: str) -> list[tuple[str, str]]:
    """``(key, text)`` facts a worker can establish without the model.

    Only the facts worth a memory: a missing pytest (with where Python lives,
    since that is the thing sessions get wrong), and package directories whose
    node_modules are missing or empty. Text is deliberately free of dates and
    counts so a re-harvest with the same state is a no-op save.
    """
    root = Path(cwd)
    facts: list[tuple[str, str]] = []
    try:
        pytest_found = importlib.util.find_spec("pytest") is not None
    except (ImportError, ValueError, AttributeError):
        pytest_found = False
    exe = sys.executable
    if not pytest_found:
        cli = shutil.which("pytest")
        hint = f"; a pytest executable is on PATH at {cli}" if cli else (
            "; run tests with a project venv or install pytest there first"
        )
        facts.append((_KEY_PYTEST, f"pytest is not importable from the worker Python {exe}{hint}."))
    else:
        facts.append((_KEY_PYTEST, f"pytest is importable from the worker Python {exe}."))
    try:
        packages = ([root] if (root / "package.json").is_file() else []) + [
            child for child in sorted(root.iterdir())
            if child.is_dir() and not child.name.startswith(".")
            and child.name != "node_modules" and (child / "package.json").is_file()
        ]
    except OSError:
        packages = []
    missing: list[str] = []
    for package in packages[:8]:
        modules = package / "node_modules"
        try:
            empty = modules.is_dir() and next(modules.iterdir(), None) is None
        except OSError:
            empty = False
        if not modules.is_dir() or empty:
            missing.append(str(package.relative_to(root)) or ".")
    if missing:
        facts.append((
            _KEY_NODE_MODULES,
            "Node dependencies are missing in " + ", ".join(missing)
            + " (run the package's install step in each fresh worktree before its scripts).",
        ))
    return facts


def harvest_tool_facts(name: str, output: str) -> list[tuple[str, str]]:
    """Facts a tool RESULT establishes mechanically (currently: browser host).

    Only browser_* tool outputs are inspected, and only for the harness's own
    "Browse <url> instead" redirection — that sentence means the browser runs
    on a host other than the worker, which is the fact two sessions six
    minutes apart each rediscovered and Remembered separately.
    """
    leaf = str(name or "").split("__")[-1]
    if not _BROWSER_TOOL_RE.search(leaf) or not output:
        return []
    match = _BROWSE_INSTEAD_RE.search(output[:4000])
    if not match:
        return []
    origin = match.group(1)
    scheme, _, authority = origin.partition("://")
    if authority.startswith("["):  # bracketed IPv6 literal, maybe with a port
        host = authority[: authority.find("]") + 1]
    else:
        host = authority.split(":", 1)[0]
    if not host or host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "[::]"):
        return []
    # Built from scheme + host, not by cutting the last ":" off the origin —
    # a portless origin has no port colon, and that cut left "http:".
    return [(
        _KEY_BROWSER_HOST,
        f"The browser tools run on another host: local servers must bind "
        f"0.0.0.0 and be browsed via {scheme}://{host}:<port> "
        f"(loopback URLs are rewritten to this host).",
    )]


def write_harvested_facts(store, project: str | None, facts) -> list[str]:
    """Save harvested facts as unprotected tool-provenance memories.

    Each fact describes this worker (its interpreter, checkout, browser
    host), so it is stored per worker: another worker's harvest under the
    same key is its own fact, shown and recalled only on that worker.

    Same text under the same key is a no-op save, so calling this every turn
    costs nothing once the state is recorded. A key the user has protected by
    an explicit Remember is left alone (``save`` refuses to overwrite it, and
    that refusal is the right answer). Returns the ids written; never raises.
    """
    written: list[str] = []
    if store is None:
        return written
    for key, text in facts:
        try:
            saved = store.save_memory(
                text, scope=project, provenance="tool", protected=False, key=_safe_key(key),
                per_worker=True,
            )
        except Exception as exc:  # noqa: BLE001 — a fact is never worth a turn
            _log.debug("runtime fact %s not saved: %s", key, exc)
            continue
        written.append(saved.id)
    return written

"""The Claude Code CLI's own model list, for the picker on a subscription sign-in.

The CLI reports which models it can run in its reply to the ``initialize``
control request: each entry has the value ``--model`` takes (``opus``,
``claude-opus-5``), a display name ("Opus 5.5"), the concrete model an alias
runs (``resolvedModel``) and the effort levels it accepts. Answering
``initialize`` sends no prompt and spends no usage.

Two ways in:

* every delegated chat process sends ``initialize`` anyway, and its reply is
  handed to :func:`record_initialize`;
* :func:`refresh` runs the same handshake by itself (no prompt) for the
  Settings Refresh button and the startup status probe.

The list belongs to THIS worker's CLI and sign-in. It is kept in memory and in
``claude-cli-models.json`` in the worker's data directory, with the CLI version
it came from. A chat that runs on another worker hands the picked id to that
worker's CLI unchanged; a CLI that does not know the id fails the turn with its
own "issue with the selected model" error (it never substitutes a model).

Only a subscription sign-in uses the list (:func:`catalog` is None in any other
mode), so API-key and cloud modes keep the static registry.

Which versioned entries the pickers offer is the owner's choice
(``ENABLED_SETTING``, a JSON list in ``system_settings``); the four aliases are
always offered and versioned entries are off until turned on.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ENABLED_SETTING = "claude_cli_enabled_models"
"""System setting: JSON list of versioned CLI model ids the pickers offer."""

_FILE_NAME = "claude-cli-models.json"
_HANDSHAKE_TIMEOUT_SECONDS = 30.0
# The CLI's "Default (recommended)" entry duplicates one of the aliases.
_SKIPPED_VALUES = frozenset({"default"})
ALIASES = ("haiku", "sonnet", "opus", "fable")

_lock = threading.Lock()
_refresh_lock = threading.Lock()
_snapshot: Optional[Dict[str, Any]] = None
_loaded = False


# ─── Parsing ────────────────────────────────────────────────────────────────


def _effort_levels(entry: Dict[str, Any]) -> Optional[List[str]]:
    """Levels the CLI accepts: a list, ``[]`` for none, None when unreported."""
    if "supportsEffort" not in entry:
        return None
    if entry.get("supportsEffort") is not True:
        return []  # Haiku 4.5 reports ``supportsEffort: null``: no effort control.
    levels = entry.get("supportedEffortLevels")
    if not isinstance(levels, list):
        return None
    return [level for level in levels if isinstance(level, str)]


def parse_initialize(payload: Any) -> Optional[Dict[str, Any]]:
    """The model list in an ``initialize`` reply payload, or None without one."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return None
    models: List[Dict[str, Any]] = []
    seen = set()
    for entry in payload["models"]:
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if not isinstance(value, str) or not value.strip() or value in _SKIPPED_VALUES or value in seen:
            continue
        seen.add(value)
        resolved = entry.get("resolvedModel")
        label = entry.get("displayName")
        description = entry.get("description")
        models.append({
            "value": value,
            "label": label if isinstance(label, str) and label.strip() else value,
            "description": description if isinstance(description, str) else "",
            "resolved": resolved if isinstance(resolved, str) and resolved.strip() else None,
            "effort_levels": _effort_levels(entry),
        })
    if not models:
        return None
    version = payload.get("claude_code_version")
    return {
        "cli_version": version if isinstance(version, str) and version else None,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "models": models,
    }


# ─── Cache ──────────────────────────────────────────────────────────────────


def _path() -> Optional[Path]:
    from app.core.config import settings

    if settings.data_dir is None:
        return None
    return Path(settings.data_dir) / _FILE_NAME


def _load() -> None:
    global _snapshot, _loaded
    if _loaded:
        return
    _loaded = True
    path = _path()
    if path is None or not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read the saved Claude Code model list", exc_info=True)
        return
    if isinstance(data, dict) and isinstance(data.get("models"), list) and data["models"]:
        _snapshot = data


def peek() -> Optional[Dict[str, Any]]:
    """The last captured list (any mode), or None before the first capture."""
    with _lock:
        _load()
        return _snapshot


def _same(a: Optional[Dict[str, Any]], b: Dict[str, Any]) -> bool:
    return bool(a) and a.get("cli_version") == b.get("cli_version") and a.get("models") == b.get("models")


def store(snapshot: Dict[str, Any]) -> bool:
    """Keep ``snapshot``; True when it differs from the saved list.

    A changed list is written to the data directory and announced, so open
    pickers re-read the registry.
    """
    global _snapshot
    with _lock:
        _load()
        if _same(_snapshot, snapshot):
            return False
        _snapshot = snapshot
        path = _path()
        if path is not None:
            try:
                tmp = path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                logger.warning("Could not save the Claude Code model list", exc_info=True)
    logger.info("Claude Code %s reports %d models", snapshot.get("cli_version") or "", len(snapshot["models"]))
    from app.core import claude_cli_status

    claude_cli_status.announce_account_change()
    return True


def _subscription_mode() -> bool:
    from app.core import claude_auth_mode

    return claude_auth_mode.current_mode() == claude_auth_mode.MODE_SUBSCRIPTION


def catalog() -> Optional[Dict[str, Any]]:
    """The list the pickers use: only on a subscription sign-in, else None."""
    snap = peek()
    if not snap:
        return None
    try:
        return snap if _subscription_mode() else None
    except Exception:  # noqa: BLE001 - the static registry is the fallback
        return None


def entry(value: Optional[str]) -> Optional[Dict[str, Any]]:
    """The subscription catalog's entry for ``value``, or None."""
    snap = catalog() if value else None
    if not snap:
        return None
    return next((m for m in snap["models"] if m.get("value") == value), None)


def alias_targets() -> Dict[str, str]:
    """``{alias: concrete id}`` for the aliases the subscription CLI resolves."""
    snap = catalog()
    if not snap:
        return {}
    return {m["value"]: m["resolved"] for m in snap["models"]
            if m.get("value") in ALIASES and m.get("resolved")}


def versioned_entries() -> List[Dict[str, Any]]:
    """The subscription catalog's entries other than the four aliases."""
    snap = catalog()
    if not snap:
        return []
    return [m for m in snap["models"] if m.get("value") not in ALIASES]


# ─── Owner's choice ─────────────────────────────────────────────────────────


def enabled_models() -> List[str]:
    """Versioned ids the owner turned on (default: none)."""
    from app.db.settings import get_system_setting_cached

    try:
        raw = get_system_setting_cached(ENABLED_SETTING)
        values = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001 - unreadable means nothing extra is offered
        logger.warning("Could not read the enabled Claude Code models", exc_info=True)
        return []
    return [v for v in values if isinstance(v, str)] if isinstance(values, list) else []


def set_enabled_models(values: List[str]) -> List[str]:
    """Save which versioned ids the pickers offer; returns the saved list."""
    from app.db.settings import set_system_setting

    clean: List[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value not in ALIASES and value not in clean:
            clean.append(value.strip())
    set_system_setting(ENABLED_SETTING, json.dumps(clean))
    from app.core import claude_cli_status

    claude_cli_status.announce_account_change()
    return clean


# ─── Capture ────────────────────────────────────────────────────────────────


def _record(payload: Any) -> None:
    try:
        if not _subscription_mode():
            return
        snapshot = parse_initialize(payload)
        if snapshot is not None:
            store(snapshot)
    except Exception:  # noqa: BLE001 - capture is best effort
        logger.warning("Could not record the Claude Code model list", exc_info=True)


def record_initialize(payload: Dict[str, Any]) -> None:
    """``DelegatedLoop(on_initialize=...)``: keep the reply's model list.

    Called on the event loop, so the mode read and the file write run on a
    thread.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return
    threading.Thread(target=_record, args=(payload,), name="claude-cli-models", daemon=True).start()


def _handshake(binary: str, env: Dict[str, str]) -> Dict[str, Any]:
    """Start the CLI, send ``initialize`` (no prompt), return its reply payload."""
    proc = subprocess.Popen(
        [binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        cwd=str(Path.home()),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, name="claude-cli-models-read", daemon=True).start()
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps({
            "type": "control_request", "request_id": "models-1",
            "request": {"subtype": "initialize"},
        }) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Claude Code did not answer in time.")
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise RuntimeError("Claude Code exited before it listed its models.")
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict) or frame.get("type") != "control_response":
                continue
            response = frame.get("response") or {}
            if response.get("request_id") != "models-1":
                continue
            if response.get("subtype") == "error":
                raise RuntimeError(str(response.get("error") or "Claude Code refused the request."))
            return response.get("response") or {}
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def refresh() -> Dict[str, Any]:
    """Ask this worker's CLI for its list now (no prompt) and keep it.

    Raises ``RuntimeError`` with a plain reason when it cannot. Concurrent
    callers share one handshake.
    """
    from app.core import claude_cli_status

    if not _subscription_mode():
        raise RuntimeError("The Claude Code model list is used only with a Claude subscription sign-in.")
    with _refresh_lock:
        binary = claude_cli_status._default_resolver()
        try:
            payload = _handshake(binary, claude_cli_status._probe_env())
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise RuntimeError(f"Could not ask Claude Code for its models: {exc}") from exc
        snapshot = parse_initialize(payload)
        if snapshot is None:
            raise RuntimeError("Claude Code did not list any models. Update it and try again.")
        store(snapshot)
        return snapshot


def refresh_if_stale(cli_version: Optional[str]) -> None:
    """Startup: list the models when none are saved or the CLI version changed."""
    try:
        if not _subscription_mode():
            return
        snap = peek()
        if snap and (not cli_version or snap.get("cli_version") == cli_version):
            return
        refresh()
    except Exception as exc:  # noqa: BLE001 - startup never fails on this
        logger.info("Claude Code model list not refreshed: %s", exc)

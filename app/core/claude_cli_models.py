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

The list also belongs to the account it was captured under, since plans list
different models. The saved list records ``account``, a short hash of the
email and organization in the same ``initialize`` reply (never the email
itself). It is served only while the active sign-in that
``claude_cli_status`` reports hashes the same, and a reply is stored only when
it matches the active sign-in and CLI version, so a handshake or chat process
started under the previous account cannot overwrite the new one. A saved-login
switch asks the CLI again (:func:`refresh_soon`).

The pickers offer every entry the CLI lists: the four aliases first, then the
versioned ids in the CLI's order.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
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

_FILE_NAME = "claude-cli-models.json"
_HANDSHAKE_TIMEOUT_SECONDS = 30.0
# The CLI's "Default (recommended)" entry duplicates one of the aliases.
_SKIPPED_VALUES = frozenset({"default"})
ALIASES = ("haiku", "sonnet", "opus", "fable")

_lock = threading.Lock()
_refresh_lock = threading.Lock()
_snapshot: Optional[Dict[str, Any]] = None
_loaded = False

_UNPINNED = object()
# Set by :func:`pinned`: one served list for every read in a projection.
_pin: contextvars.ContextVar[Any] = contextvars.ContextVar("claude_cli_models_pin", default=_UNPINNED)


@contextlib.contextmanager
def pinned():
    """Serve one Claude auth mode and one :func:`catalog` read for the block.

    A projection that reads them several times (the registry payload) would
    otherwise mix two accounts' lists, or a subscription's list with Cloud's
    metadata, if the owner switched midway. Yields the list (None when no
    list is served)."""
    from app.core import claude_auth_mode

    current = _pin.get()
    if current is not _UNPINNED:
        yield current
        return
    with claude_auth_mode.pinned_mode():
        snap = catalog()
        token = _pin.set(snap)
        try:
            yield snap
        finally:
            _pin.reset(token)


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


def account_key(email: Any, organization: Any) -> Optional[str]:
    """A non-secret id for a Claude sign-in (a hash), None without an email."""
    if not isinstance(email, str) or not email.strip():
        return None
    org = organization.strip() if isinstance(organization, str) else ""
    return hashlib.sha256(f"{email.strip().lower()}\n{org}".encode("utf-8")).hexdigest()[:16]


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
    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    return {
        "cli_version": version if isinstance(version, str) and version else None,
        "account": account_key(account.get("email"), account.get("organization")),
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
    return bool(a) and all(a.get(k) == b.get(k) for k in ("cli_version", "account", "models"))


def store(snapshot: Dict[str, Any]) -> Optional[bool]:
    """Keep ``snapshot``; True when it differs from the saved list, None when
    it is not the active sign-in's.

    Stored only when its account (and CLI version) match the sign-in
    ``claude_cli_status`` holds in memory, checked under the same lock as the
    write; an unknown sign-in refuses. A saved-login switch drops that status
    first, so a capture started under the previous account cannot land.
    A changed list is written to the data directory and announced, so open
    pickers re-read the registry.
    """
    global _snapshot
    from app.core import claude_cli_status

    with _lock:
        _load()
        status = claude_cli_status.peek()
        if not snapshot.get("account") or snapshot["account"] != _account_of(status):
            logger.info("Claude Code model list from another sign-in ignored")
            return None
        if status.cli_version and snapshot.get("cli_version") not in (None, status.cli_version):
            logger.info("Claude Code model list from version %s ignored (running %s)",
                        snapshot["cli_version"], status.cli_version)
            return None
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
    claude_cli_status.announce_account_change()
    return True


def subscription_mode() -> bool:
    from app.core import claude_auth_mode

    return claude_auth_mode.current_mode() == claude_auth_mode.MODE_SUBSCRIPTION


def _account_of(status: Any) -> Optional[str]:
    """The ``account_key`` of a ``claude_cli_status`` result, None unless signed in."""
    if status is None or not status.signed_in:
        return None
    return account_key(status.email, status.org_name)


def catalog() -> Optional[Dict[str, Any]]:
    """The list the pickers use, else None.

    Only on a subscription sign-in, and only while the active sign-in is the
    account the list was captured under (unknown counts as different)."""
    held = _pin.get()
    if held is not _UNPINNED:
        return held
    snap = peek()
    if not snap or not snap.get("account"):
        return None
    try:
        from app.core import claude_cli_status

        if not subscription_mode() or snap["account"] != _account_of(claude_cli_status.peek()):
            return None
        return snap
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


# ─── Capture ────────────────────────────────────────────────────────────────


def _record(payload: Any) -> None:
    try:
        if not subscription_mode():
            return
        snapshot = parse_initialize(payload)
        if snapshot is not None:
            from app.core import claude_cli_status

            claude_cli_status.get_status()  # knows the active sign-in (may probe, no lock held)
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

    if not subscription_mode():
        raise RuntimeError("The Claude Code model list is used only with a Claude subscription sign-in.")
    with _refresh_lock:
        claude_cli_status.get_status()  # the active sign-in, before asking
        binary = claude_cli_status._default_resolver()
        try:
            payload = _handshake(binary, claude_cli_status._probe_env())
        except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
            raise RuntimeError(f"Could not ask Claude Code for its models: {exc}") from exc
        snapshot = parse_initialize(payload)
        if snapshot is None:
            raise RuntimeError("Claude Code did not list any models. Update it and try again.")
        if store(snapshot) is None:
            raise RuntimeError("The Claude sign-in changed while Claude Code listed its models. Try again.")
        return snapshot


def refresh_if_stale(cli_version: Optional[str]) -> None:
    """Startup: list the models when none are saved for the active sign-in or
    the CLI version changed."""
    try:
        if not subscription_mode():
            return
        snap = catalog()
        if snap and (not cli_version or snap.get("cli_version") == cli_version):
            return
        refresh()
    except Exception as exc:  # noqa: BLE001 - startup never fails on this
        logger.info("Claude Code model list not refreshed: %s", exc)


def refresh_soon() -> None:
    """A saved-login switch: ask the CLI for the new account's list (no prompt),
    off the caller's thread. Never raises."""
    def run() -> None:
        try:
            if subscription_mode():
                refresh()
        except Exception as exc:  # noqa: BLE001 - the next chat or Refresh retries
            logger.info("Claude Code model list not refreshed after the sign-in changed: %s", exc)

    threading.Thread(target=run, name="claude-cli-models-switch", daemon=True).start()

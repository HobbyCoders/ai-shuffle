"""Saved Claude subscription sign-ins on this worker, and which one is active.

Each worker keeps its own list in ``claude_cli_logins:<instance id>``, a
worker-local system setting that never syncs (``app.network.policy``): every
worker keeps its own CLI sign-ins. A saved entry is either:

- ``default``: the CLI's own folder (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``),
  registered with *save current*. Forgetting it never signs the CLI out.
- ``saved``: an extra sign-in in an app-made folder
  (:mod:`app.core.claude_cli_home`), added with *add another*.

One subscription is active at a time. No active entry means the default
folder, exactly as before saved sign-ins existed. Activating an entry turns its
siblings off; deleting the active one returns Claude Code to the default
folder without activating another saved entry. The Claude Code mode
(subscription, API key, cloud, off) is separate and never changed here.

Every ``claude`` spawn applies :func:`apply_cli_home`, which points it at the
active entry's folder. Identity (email, plan, organization) only ever comes from
``claude auth status`` run against an entry's folder; nothing here reads, copies
or stores the CLI's credentials. On disk this module only prepares and removes
whole folders (:mod:`app.core.claude_cli_home`): preparing one links the shared
state and creates an empty ``settings.json`` (``{}``) in the default folder if it
has none, and deleting a saved entry removes its app-made folder entirely after
``claude auth logout`` there.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Optional
from uuid import uuid4

from app.core import claude_cli_home as home
from app.db import database

logger = logging.getLogger(__name__)

SETTING_PREFIX = "claude_cli_logins:"
KIND_DEFAULT = "default"
KIND_SAVED = "saved"
DEFAULT_LABEL = "Claude subscription"
_PROBE_INTERVAL_SECONDS = 3.0
_VERIFY_INTERVAL_SECONDS = 30.0
_lock = threading.RLock()
# Serializes creating/repairing a saved folder on the spawn path against
# removing one, so a spawn never recreates a folder that is being deleted.
_folder_lock = threading.Lock()
_verified: dict[str, float] = {}
_last_pending_probe = 0.0
# Set when the active folder changes; the API then retires idle pooled CLIs.
_reap_due = False


class SubscriptionError(RuntimeError):
    """A safe error with a stable API code and HTTP status."""

    def __init__(self, code: str, detail: str, status: int = 409) -> None:
        super().__init__(detail)
        self.code, self.status = code, status


# ─── Storage ─────────────────────────────────────────────────────────────────

def setting_key() -> str:
    from app.core.config import settings

    return SETTING_PREFIX + settings.effective_instance_id


def _empty() -> dict:
    return {"accounts": [], "active_id": None, "pending": None}


def _parse(raw: Optional[str]) -> dict:
    if not raw:
        return _empty()
    try:
        value = json.loads(raw)
        accounts = [a for a in value.get("accounts", []) if isinstance(a, dict)
                    and isinstance(a.get("id"), str) and a.get("kind") in (KIND_DEFAULT, KIND_SAVED)]
        pending = value.get("pending")
        if not (isinstance(pending, dict) and home.valid_id(pending.get("id"))):
            pending = None
        active = value.get("active_id")
        if not any(a["id"] == active for a in accounts):
            active = None
        return {"accounts": accounts, "active_id": active, "pending": pending}
    except (ValueError, AttributeError, TypeError):
        logger.warning("The saved Claude sign-in list could not be read; using the default sign-in")
        return _empty()


def _load(*, cached: bool = False) -> dict:
    getter = database.get_system_setting_cached if cached else database.get_system_setting
    return _parse(getter(setting_key()))


def _save(state: dict) -> None:
    database.set_system_setting(setting_key(), json.dumps(state, separators=(",", ":")))


def _find(state: dict, account_id: str) -> Optional[dict]:
    return next((a for a in state["accounts"] if a["id"] == account_id), None)


def _active_saved_id(state: dict) -> Optional[str]:
    """The folder id of the active extra sign-in, or None for the default folder."""
    account = _find(state, state["active_id"]) if state["active_id"] else None
    if account is not None and account["kind"] == KIND_SAVED and home.valid_id(account["id"]):
        return account["id"]
    return None


# ─── What every claude spawn uses ────────────────────────────────────────────

def active_home_id() -> Optional[str]:
    """The active extra sign-in's id, or None (the default folder). Never raises."""
    try:
        if not home.platform_support()[0]:
            return None
        return _active_saved_id(_load(cached=True))
    except Exception:  # noqa: BLE001 — a DB hiccup keeps the default folder
        logger.warning("Could not read the active Claude sign-in; using the default", exc_info=True)
        return None


def active_home_env() -> dict[str, str]:
    """``{"CLAUDE_CONFIG_DIR": <folder>}`` for an extra sign-in, ``{}`` for the default.

    The folder's shared links are re-checked at most every 30 seconds and
    repaired, so a removed folder never makes the CLI write transcripts to an
    unshared place. A folder that cannot be repaired is still returned: the
    CLI then reports no sign-in and the credential gate asks to sign in again,
    rather than running on a different account.
    """
    account_id = active_home_id()
    if account_id is None:
        return {}
    now = time.monotonic()
    if now - _verified.get(account_id, 0.0) > _VERIFY_INTERVAL_SECONDS:
        with _folder_lock:
            # Re-read under the lock: a delete saves the shortened list before
            # it removes the folder, so a deleted entry is never recreated.
            account_id = active_home_id()
            if account_id is None:
                return {}
            try:
                home.ensure(account_id)
                _verified[account_id] = now
            except (home.HomeError, OSError):
                logger.warning("The active Claude sign-in folder could not be repaired", exc_info=True)
    return home.env_for(account_id)


def apply_cli_home(env: dict) -> dict:
    """Point a ``claude`` spawn env at the active sign-in. Returns ``env``."""
    env.update(active_home_env())
    return env


def fingerprint_part() -> str:
    """Non-secret pooled-session identity of the active sign-in folder."""
    return active_home_id() or "default"


# ─── Identity, from `claude auth status` only ────────────────────────────────

def _probe(account_id: Optional[str]):
    from app.core import claude_cli_status

    return claude_cli_status.probe_home(home.env_for(account_id))


def _record_identity(account: dict, status) -> None:
    account["signed_in"] = status.signed_in
    account["checked_at"] = time.time()
    if status.signed_in:
        account["email"], account["plan"], account["org_name"] = status.email, status.plan, status.org_name


# ─── Public payload ──────────────────────────────────────────────────────────

def support() -> dict[str, Any]:
    supported, reason = home.platform_support()
    return {"supported": supported, "reason": reason}


def _quote_powershell(value: str) -> str:
    """A PowerShell single-quoted literal (every single-quote form doubled)."""
    return "'" + re.sub("['\u2018\u2019\u201a\u201b]", lambda m: m.group(0) * 2, value) + "'"


def _quote_posix(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _cmd_command(folder: str) -> Optional[str]:
    """The Command Prompt line, or None when the path cannot be quoted for cmd
    (a ``"`` ends the quoted ``set``; ``%`` and ``!`` would be expanded)."""
    if any(ch in folder for ch in '"%!\r\n'):
        return None
    return f'set "CLAUDE_CONFIG_DIR={folder}" && claude auth login'


def _login_state(pending: Optional[dict]) -> Optional[dict]:
    if not pending:
        return None
    from app.core import claude_cli_login

    terminal = claude_cli_login.terminal_supported()
    folder = str(home.account_dir(pending["id"]))
    state = {
        "id": pending["id"],
        "status": pending.get("status", "waiting"),
        "error": pending.get("error"),
        "terminal": terminal,
        # The in-app terminal runs it; otherwise the owner runs one of these
        # on this worker. ``env`` is what any shell needs.
        "command": "claude auth login",
        "env": {"CLAUDE_CONFIG_DIR": folder},
        "commands": None if terminal else {
            "powershell": f"$env:CLAUDE_CONFIG_DIR={_quote_powershell(folder)}; claude auth login",
            "cmd": _cmd_command(folder),
            "posix": f"CLAUDE_CONFIG_DIR={_quote_posix(folder)} claude auth login",
        },
    }
    if terminal:
        run = claude_cli_login.login_terminal.read_for(_tag(pending["id"]))
        state["terminal_state"] = run
        if run is not None and run["running"]:
            state["status"] = "running"
    return state


def _tag(account_id: str) -> str:
    return "add-subscription:" + account_id


def payload() -> dict[str, Any]:
    """The ``subscription`` part of the saved-accounts envelope."""
    with _lock:
        try:
            _poll_pending()
            state = _load()
        except Exception:  # noqa: BLE001 — keys still list; the error is named
            logger.warning("Could not read saved Claude sign-ins", exc_info=True)
            return {"subscription": [], "default_in_use": True, "login_pending": False, "login": None,
                    "recovery_required": False, "subscription_error": "storage_unavailable"}
        active_saved = _active_saved_id(state)
        rows = []
        for account in state["accounts"]:
            active = (account["id"] == state["active_id"]) if state["active_id"] else (
                account["kind"] == KIND_DEFAULT)
            folder_ok = account["kind"] == KIND_DEFAULT or home.intact(account["id"])
            rows.append({
                "id": account["id"],
                "label": account.get("label") or DEFAULT_LABEL,
                "kind": account["kind"],
                "active": active,
                "email": account.get("email"),
                "plan": account.get("plan"),
                "org_name": account.get("org_name"),
                "signed_in": account.get("signed_in"),
                "checked_at": account.get("checked_at"),
                "needs_sign_in": not folder_ok or account.get("signed_in") is False,
            })
        active_row = next((r for r in rows if r["id"] == active_saved), None)
        return {
            "subscription": rows,
            # True while Claude Code runs in the CLI's own default folder.
            "default_in_use": active_saved is None,
            "login_pending": state["pending"] is not None,
            "login": _login_state(state["pending"]),
            "recovery_required": bool(active_row and active_row["needs_sign_in"]),
        }


# ─── Mutations ───────────────────────────────────────────────────────────────

def _require_support() -> None:
    supported, reason = home.platform_support()
    if not supported:
        raise SubscriptionError("unsupported", reason or "Not available on this worker.", 501)


def _label(value: Any, default: str) -> str:
    if not isinstance(value, str) or len(value.strip()) > 120:
        raise SubscriptionError("invalid_label", "Use a label of at most 120 characters.", 400)
    return value.strip() or default


def _switched() -> None:
    """A different folder is now active: new spawns and status reads follow it."""
    global _reap_due
    from app.core import claude_cli_status

    _reap_due = True
    claude_cli_status.invalidate()
    claude_cli_status.announce_account_change()


def save_current(label: str = "") -> str:
    """Register the default folder's current sign-in as a named entry."""
    _require_support()
    label = _label(label, "")
    status = _probe(None)
    if not status.signed_in:
        raise SubscriptionError(
            "not_signed_in", "Claude Code is not signed in. Sign in first, then save it.", 409)
    with _lock:
        state = _load()
        account = next((a for a in state["accounts"] if a["kind"] == KIND_DEFAULT), None)
        if account is None:
            account = {"id": uuid4().hex[:16], "kind": KIND_DEFAULT}
            state["accounts"].insert(0, account)
        account["label"] = label or account.get("label") or status.email or DEFAULT_LABEL
        _record_identity(account, status)
        _save(state)
        return account["id"]


def change(account_id: str, action: str, label: str = "") -> None:
    """Rename, activate or delete one saved sign-in on this worker."""
    _require_support()
    with _lock:
        state = _load()
        account = _find(state, account_id)
        if account is None:
            raise SubscriptionError("not_found", "Saved Claude account not found.", 404)
        if action == "rename":
            account["label"] = _label(label, account.get("label") or DEFAULT_LABEL)
            _save(state)
            return
        if action == "activate":
            if account["kind"] == KIND_SAVED:
                try:
                    home.ensure(account["id"])
                except home.HomeError as exc:
                    raise SubscriptionError("folder_unavailable", str(exc), 409) from None
            previous = _active_saved_id(state)
            state["active_id"] = account["id"]
            _save(state)
            if _active_saved_id(state) != previous:
                _switched()
            _refresh_quietly(account["id"])
            return
        if action == "delete":
            was_active_folder = _active_saved_id(state) == account["id"]
            # Drop the entry first, so no new spawn picks this folder (and
            # recreates it) while it is being removed.
            state["accounts"] = [a for a in state["accounts"] if a["id"] != account["id"]]
            if state["active_id"] == account["id"]:
                state["active_id"] = None
            _save(state)
            _verified.pop(account["id"], None)
            if was_active_folder:
                _switched()
            if account["kind"] == KIND_SAVED:
                _sign_out_folder(account["id"])
                try:
                    with _folder_lock:
                        home.remove(account["id"])
                except (home.HomeError, OSError) as exc:
                    raise SubscriptionError(
                        "storage_unavailable",
                        "The sign-in was removed from the list, but its folder could not be "
                        f"deleted ({type(exc).__name__}).", 503,
                    ) from None
            return
        raise SubscriptionError("invalid_action", "Unknown account action.", 400)


def _refresh_quietly(account_id: str) -> None:
    """Re-read one entry's identity from the CLI; failures keep the old values."""
    try:
        account = _find(_load(), account_id)
        if account is None:
            return
        status = _probe(account["id"] if account["kind"] == KIND_SAVED else None)
        if status.signed_in is None:
            return
        state = _load()
        account = _find(state, account_id)
        if account is not None:
            _record_identity(account, status)
            _save(state)
    except Exception:  # noqa: BLE001 — display data only
        logger.debug("Could not refresh a saved Claude sign-in", exc_info=True)


def _sign_out_folder(account_id: str) -> None:
    """Let the CLI remove its own sign-in from an extra folder (best effort)."""
    from app.core import claude_cli_status

    try:
        claude_cli_status.run_home(["auth", "logout"], home.env_for(account_id))
    except Exception:  # noqa: BLE001 — the folder is removed next either way
        logger.info("`claude auth logout` for a saved sign-in folder did not finish", exc_info=True)


# ─── Add another: a CLI sign-in into a fresh folder ──────────────────────────

def start_login() -> dict:
    """Prepare a fresh folder and start ``claude auth login`` into it.

    With the in-app terminal (Docker, Linux) the login runs there; otherwise the
    owner runs the returned command on this worker. The sign-in becomes a saved,
    active entry once ``claude auth status`` in that folder reports it.
    """
    _require_support()
    with _lock:
        state = _load()
        pending = state["pending"]
        if pending is None:
            pending = {"id": uuid4().hex[:16], "started_at": time.time(), "status": "waiting"}
            state["pending"] = pending
            _save(state)  # recorded first, so cancel can always clean up
        try:
            home.ensure(pending["id"])
        except home.HomeError as exc:
            raise SubscriptionError("folder_unavailable", str(exc), 409) from None
        pending.update(status="waiting", error=None)
        from app.core import claude_cli_login

        if claude_cli_login.terminal_supported():
            try:
                claude_cli_login.login_terminal.start(
                    home_env=home.env_for(pending["id"]), tag=_tag(pending["id"]))
            except RuntimeError as exc:  # another sign-in running, or no CLI
                from fluid.engine.delegated import ClaudeCliMissingError

                code = "cli_missing" if isinstance(exc, ClaudeCliMissingError) else "login_busy"
                raise SubscriptionError(code, str(exc), 409) from None
        _save(state)
        return _login_state(pending)


def _adopt(state: dict, pending: dict, status) -> str:
    """Save the pending folder's finished sign-in as the active entry."""
    account = {"id": pending["id"], "kind": KIND_SAVED,
               "label": status.email or DEFAULT_LABEL}
    _record_identity(account, status)
    state["accounts"].append(account)
    state["active_id"] = account["id"]
    state["pending"] = None
    _save(state)
    _verified[account["id"]] = time.monotonic()
    logger.info("Saved another Claude subscription sign-in and made it active")
    _switched()
    return account["id"]


def _signed_in_status(account_id: str):
    """``claude auth status`` for an intact pending folder that reports a sign-in, else None."""
    if not home.intact(account_id):
        return None
    status = _probe(account_id)
    return status if status.signed_in else None


def _poll_pending() -> None:
    """Finish a pending add-another login once the CLI reports it.

    With the in-app terminal, a pending login whose run is gone (stopped from
    another view, never started, or expired) or has exited without signing in
    is marked failed, so the owner sees it and can try again or cancel.
    """
    global _last_pending_probe
    state = _load()
    pending = state["pending"]
    if pending is None:
        return
    from app.core import claude_cli_login

    ended = False
    if claude_cli_login.terminal_supported():
        run = claude_cli_login.login_terminal.read_for(_tag(pending["id"]))
        if run is not None and run["running"]:
            return
        ended = True  # no run of ours is going: nothing else will finish it
    now = time.monotonic()
    if now - _last_pending_probe < _PROBE_INTERVAL_SECONDS:
        return
    _last_pending_probe = now
    status = _signed_in_status(pending["id"])
    if status is None:
        if ended and pending.get("status") != "failed":
            pending.update(status="failed", error="The sign-in did not finish. Try again or cancel it.")
            _save(state)
        return
    _adopt(state, pending, status)


def cancel_login() -> Optional[str]:
    """Stop a pending add-another login and remove its folder.

    When the CLI already finished signing in to that folder (before the poll
    caught up), the sign-in is kept instead: it is saved and made active just
    as the poll would, and its id is returned. Otherwise returns None.
    """
    _require_support()
    with _lock:
        state = _load()
        pending = state["pending"]
        if pending is None:
            return None
        from app.core import claude_cli_login

        # Stop first, then ask: a login that finished meanwhile is still kept.
        claude_cli_login.login_terminal.cancel_for(_tag(pending["id"]))
        status = _signed_in_status(pending["id"])
        if status is not None:
            return _adopt(state, pending, status)
        try:
            home.remove(pending["id"])
        except (home.HomeError, OSError) as exc:
            raise SubscriptionError(
                "storage_unavailable",
                f"The unfinished sign-in folder could not be removed ({type(exc).__name__}).", 503,
            ) from None
        state["pending"] = None
        _save(state)
        return None


# ─── Startup ─────────────────────────────────────────────────────────────────

def repair_on_startup() -> None:
    """Re-link every saved folder once at boot (never fatal, never creates one)."""
    if not home.platform_support()[0]:
        return
    try:
        state = _load()
    except Exception:  # noqa: BLE001
        logger.warning("Could not read saved Claude sign-ins at startup", exc_info=True)
        return
    for account in state["accounts"]:
        if account["kind"] != KIND_SAVED:
            continue
        try:
            report = home.ensure(account["id"], create=False)
            if not report["exists"]:
                logger.warning("A saved Claude sign-in folder is missing; sign in to it again")
        except (home.HomeError, OSError):
            logger.warning("Could not re-link a saved Claude sign-in folder", exc_info=True)


async def reap_idle_pooled_clis() -> int:
    """After a switch, evict idle pooled Claude sessions still on the old sign-in.

    Busy sessions keep running and reconnect on their next turn, when the
    credential fingerprint no longer matches. Existing chats resume on the new
    sign-in from the shared transcripts.
    """
    global _reap_due
    if not _reap_due:
        return 0
    _reap_due = False
    from app.core import claude_auth_mode
    from app.core.session_pool import get_session_pool

    try:
        fingerprint = claude_auth_mode.spawn_fingerprint()
        return len(await get_session_pool().evict_idle_claude_auth_changed(fingerprint))
    except Exception:  # noqa: BLE001 — the next turn reconnects on its own anyway
        logger.debug("Could not evict idle sessions after a Claude sign-in switch", exc_info=True)
        return 0


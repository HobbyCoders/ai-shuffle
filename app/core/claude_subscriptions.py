"""Saved Claude subscription sign-ins on this worker, and which one is active.

Each worker keeps its own list in ``claude_cli_logins:<instance id>``, a
worker-local system setting that never syncs (``app.network.policy``): every
worker keeps its own CLI sign-ins. A saved entry is either:

- ``default``: the CLI's own folder (``$CLAUDE_CONFIG_DIR`` or ``~/.claude``),
  registered with *save current*. Forgetting it never signs the CLI out.
- ``saved``: an extra sign-in in an app-made folder
  (:mod:`app.core.claude_cli_home`), added with *add another*.

A shared selection names the sign-in identity, not a folder or credential.
Each worker resolves it against its own CLI sign-ins. Missing or different
sign-ins must not fall back to the default folder. Until the first explicit
choice, the legacy worker-local selection still applies. Selecting a saved
subscription does not change the Claude Code auth mode.

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

import hashlib
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
SELECTION_SETTING = "claude_subscription_selection"
UNRESOLVED = "unresolved"
SELECTION_UNAVAILABLE = (
    "The selected Claude subscription is not signed in on this worker. "
    "Sign in to that Claude account and organization here, or select a different account in Settings > Providers."
)
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
_identity_cache: dict[Optional[str], tuple[float, Any]] = {}
# Independent of the subscription/terminal locks. Never hold this across a
# probe, database operation or terminal read: login completion takes it while
# holding the terminal lock, whereas payload() holds the subscription lock.
_identity_lock = threading.Lock()
_identity_generation = 0
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
    return {"accounts": [], "active_id": None, "pending": None, "cleanup": []}


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
        cleanup = value.get("cleanup", [])
        cleanup = [item for item in cleanup if home.valid_id(item)] if isinstance(cleanup, list) else []
        return {"accounts": accounts, "active_id": active, "pending": pending, "cleanup": cleanup}
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

def _legacy_home_id() -> Optional[str]:
    """The active extra sign-in's id, or None (the default folder)."""
    try:
        if not home.platform_support()[0]:
            return None
        return _active_saved_id(_load(cached=True))
    except Exception:  # noqa: BLE001 — a DB hiccup keeps the default folder
        logger.warning("Could not read the active Claude sign-in; using the default", exc_info=True)
        return None


def _binding(status) -> Optional[str]:
    # An email alone does not distinguish this user's billing organizations.
    # CLI auth status supplies both; never inspect its credential store.
    if not (status.signed_in and status.auth_method == "claude.ai" and status.email and status.org_id):
        return None
    identity = [status.email.strip().casefold(), status.org_id.strip().lower()]
    if not all(identity):
        return None
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def shared_target(choice: Optional[dict] = None, *, read_choice: bool = True) -> Optional[str]:
    from app.core import claude_auth_mode

    if read_choice:
        choice = claude_auth_mode.shared_selection(cached=True)
    if choice and "subscription" in choice:
        target = choice["subscription"]
        return target if isinstance(target, str) and target else UNRESOLVED
    # Read old subscription rows during upgrade, but never for a new choice.
    target = database.get_system_setting_cached(SELECTION_SETTING)
    if target is not None:
        return target
    return UNRESOLVED if choice and choice["mode"] == claude_auth_mode.MODE_SUBSCRIPTION else None


def legacy_binding() -> Optional[str]:
    return _binding(_current_identity(_legacy_home_id(), force=True))


def select_default() -> None:
    """Owner explicitly chooses this worker's verified default CLI sign-in."""
    from app.core import claude_auth_mode

    with _lock:
        target = _binding(_current_identity(None, force=True))
        if target is None:
            raise SubscriptionError("unverified_subscription", SELECTION_UNAVAILABLE)
        claude_auth_mode.publish_subscription(target)
        _switched()


def shared_home(target: str, *, force: bool = False):
    """Return (local home id, matching CLI status), or raise if unavailable."""
    if re.fullmatch(r"[0-9a-f]{64}", target):
        state = _load(cached=True) if home.platform_support()[0] else _empty()
        saved = [a["id"] for a in state["accounts"]
                 if a["kind"] == KIND_SAVED and home.valid_id(a["id"]) and home.intact(a["id"])]
        preferred = _active_saved_id(state)
        homes = [preferred, *(candidate for candidate in [None, *saved] if candidate != preferred)]
        if preferred is not None and preferred not in saved:
            homes.remove(preferred)
        for home_id in homes:
            status = _current_identity(home_id, force=force)
            if _binding(status) == target:
                return home_id, status
    raise SubscriptionError("selected_subscription_unavailable", SELECTION_UNAVAILABLE)


def active_home_env(*, force: bool = False) -> dict[str, str]:
    """``{"CLAUDE_CONFIG_DIR": <folder>}`` for an extra sign-in, ``{}`` for the default.

    The folder's shared links are re-checked at most every 30 seconds and
    repaired, so a removed folder never makes the CLI write transcripts to an
    unshared place. A folder that cannot be repaired is still returned: the
    CLI then reports no sign-in and the credential gate asks to sign in again,
    rather than running on a different account.
    """
    target = shared_target()
    if target is not None:
        return verified_home_env(target, force=force)
    return legacy_home_env()


def verified_home_env(target: str, *, force: bool = True) -> dict[str, str]:
    return home.env_for(shared_home(target, force=force)[0])


def legacy_home_env() -> dict[str, str]:
    account_id = _legacy_home_id()
    if account_id is None:
        return {}
    now = time.monotonic()
    if now - _verified.get(account_id, 0.0) > _VERIFY_INTERVAL_SECONDS:
        with _folder_lock:
            # Re-read under the lock: a delete saves the shortened list before
            # it removes the folder, so a deleted entry is never recreated.
            account_id = _legacy_home_id()
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
    target = shared_target()
    if target is not None:
        home.bind_sign_in(env, verified_home_env(target))
    else:
        env.update(legacy_home_env())
    return env


def fingerprint_part() -> str:
    """Non-secret pooled-session identity of the active sign-in folder."""
    target = shared_target()
    if target is None:
        return _legacy_home_id() or "default"
    try:
        local_home, _ = shared_home(target, force=True)
    except SubscriptionError:
        return target + ":unavailable"
    return f"{target}:{local_home or 'default'}"


# ─── Identity, from `claude auth status` only ────────────────────────────────

def _probe(account_id: Optional[str]):
    from app.core import claude_cli_status

    return claude_cli_status.probe_home(home.env_for(account_id))


def invalidate_identity() -> None:
    """A login/switch ended: never serve the prior per-folder auth result."""
    global _identity_generation
    # Also called while the login terminal holds its own lock. Do not acquire
    # _lock here: payload() holds it while reading the terminal's state.
    with _identity_lock:
        _identity_generation += 1
        _identity_cache.clear()


def _current_identity(account_id: Optional[str], *, force: bool = False):
    """Generation-guarded auth status for one folder (None means default)."""
    while True:
        with _identity_lock:
            cached = _identity_cache.get(account_id)
            if not force and cached is not None and time.monotonic() - cached[0] < _VERIFY_INTERVAL_SECONDS:
                return cached[1]
            generation = _identity_generation
        status = _probe(account_id)
        with _identity_lock:
            if generation != _identity_generation:
                # Login completed while the CLI was reading the old identity.
                # Reject it for both this response and subsequent cached reads.
                continue
            _identity_cache[account_id] = (time.monotonic(), status)
            return status


def _record_identity(account: dict, status) -> bool:
    """Apply a readable result; return whether its durable fields changed."""
    # A timeout or unreadable CLI result says nothing about authentication.
    # Keep the last readable state, including a known need to sign in again.
    if status.signed_in is None:
        return False
    previous = account.copy()
    account["signed_in"] = status.signed_in
    # A cached read is not a new check. This also avoids rewriting storage on
    # every listing while preserving the latest determinate probe across expiry.
    account["checked_at"] = status.checked_at
    if status.signed_in:
        account["email"], account["plan"], account["org_name"] = status.email, status.plan, status.org_name
    return account != previous


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


def _login_state(pending: Optional[dict], *, terminal: Optional[bool] = None) -> Optional[dict]:
    if not pending:
        return None
    from app.core import claude_cli_login

    if terminal is None:
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
        refresh_error = {}
        try:
            _poll_pending()
        except Exception:  # noqa: BLE001 — a failed mutation need not prevent reading
            logger.warning("Could not update a pending Claude sign-in", exc_info=True)
            refresh_error["subscription_error"] = "storage_unavailable"
        try:
            # Polling mutates its own loaded copy. Always read durable state,
            # including when an adoption or failed-status write did not commit.
            state = _load()
        except Exception:  # noqa: BLE001 — keys still list; the error is named
            logger.warning("Could not read saved Claude sign-ins", exc_info=True)
            return {"subscription": [], "default_in_use": True, "login_pending": False, "login": None,
                    "recovery_required": False, "subscription_error": "storage_unavailable"}
        target = shared_target()
        unavailable = False
        if target is None:
            active_saved = _active_saved_id(state)
        else:
            try:
                active_saved, _ = shared_home(target)
            except SubscriptionError:
                active_saved, unavailable = None, True
        rows = []
        identities_changed = False
        for account in state["accounts"]:
            if target is None:
                active = (account["id"] == state["active_id"]) if state["active_id"] else (
                    account["kind"] == KIND_DEFAULT)
            else:
                active = not unavailable and (
                    account["kind"] == KIND_DEFAULT if active_saved is None else account["id"] == active_saved
                )
            folder_ok = account["kind"] == KIND_DEFAULT or home.intact(account["id"])
            if folder_ok:
                home_id = account["id"] if account["kind"] == KIND_SAVED else None
                identities_changed = _record_identity(account, _current_identity(home_id)) or identities_changed
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
        if identities_changed:
            try:
                _save(state)
            except Exception:  # noqa: BLE001 — keep readable rows and API keys available
                logger.warning("Could not refresh saved Claude sign-ins", exc_info=True)
                refresh_error["subscription_error"] = "storage_unavailable"
        active_row = next((r for r in rows if r["id"] == active_saved), None)
        return {
            "subscription": rows,
            # True while Claude Code runs in the CLI's own default folder.
            "default_in_use": active_saved is None and not unavailable,
            "login_pending": state["pending"] is not None,
            "login": _login_state(state["pending"]),
            "recovery_required": unavailable or bool(active_row and active_row["needs_sign_in"]),
            **refresh_error,
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
    from app.core import claude_cli_models, claude_cli_status

    _reap_due = True
    invalidate_identity()
    claude_cli_status.invalidate()
    claude_cli_status.announce_account_change()
    # The picker's model list belongs to the account: ask the CLI again.
    claude_cli_models.refresh_soon()


def save_current(label: str = "") -> str:
    """Register the default folder's current sign-in as a named entry."""
    _require_support()
    label = _label(label, "")
    with _lock:
        status = _current_identity(None, force=True)
        if not status.signed_in:
            raise SubscriptionError(
                "not_signed_in", "Claude Code is not signed in. Sign in first, then save it.", 409)
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
            home_id = account["id"] if account["kind"] == KIND_SAVED else None
            status = _current_identity(home_id, force=True)
            target = _binding(status)
            if not target:
                raise SubscriptionError("unverified_subscription", SELECTION_UNAVAILABLE)
            _record_identity(account, status)
            state["active_id"] = account["id"]
            _save(state)
            from app.core import claude_auth_mode

            claude_auth_mode.publish_subscription(target)
            _switched()
            return
        if action == "delete":
            was_active_folder = _active_saved_id(state) == account["id"]
            # Drop the entry first, so no new spawn picks this folder (and
            # recreates it) while it is being removed.
            state["accounts"] = [a for a in state["accounts"] if a["id"] != account["id"]]
            if state["active_id"] == account["id"]:
                state["active_id"] = None
            if account["kind"] == KIND_SAVED and account_id not in state["cleanup"]:
                state["cleanup"].append(account_id)
            _save(state)
            _verified.pop(account["id"], None)
            if was_active_folder:
                _switched()
            if account["kind"] == KIND_SAVED:
                _sign_out_folder(account["id"])
                try:
                    with _folder_lock:
                        home.remove(account["id"])
                    state["cleanup"].remove(account_id)
                    _save(state)
                except (home.HomeError, OSError) as exc:
                    raise SubscriptionError(
                        "storage_unavailable",
                        "The sign-in was removed from the list, but its folder could not be "
                        f"deleted ({type(exc).__name__}). Cleanup will retry at startup.", 503,
                    ) from None
            return
        raise SubscriptionError("invalid_action", "Unknown account action.", 400)


def _sign_out_folder(account_id: str) -> None:
    """Let the CLI remove its own sign-in from an extra folder (best effort)."""
    from app.core import claude_cli_status

    try:
        claude_cli_status.run_home(["auth", "logout"], home.env_for(account_id))
    except Exception:  # noqa: BLE001 — the folder is removed next either way
        logger.info("`claude auth logout` for a saved sign-in folder did not finish", exc_info=True)


# ─── Add another: a CLI sign-in into a fresh folder ──────────────────────────

def sign_in_commands(account_id: str, terminal: bool = False) -> dict:
    """Folder-scoped external login commands, without switching the active account."""
    _require_support()
    with _lock:
        account = _find(_load(), account_id)
        if account is None or account["kind"] != KIND_SAVED:
            raise SubscriptionError("not_found", "Saved Claude sign-in not found.", 404)
        try:
            home.ensure(account_id)
        except home.HomeError as exc:
            raise SubscriptionError("folder_unavailable", str(exc), 409) from None
        invalidate_identity()
        if terminal:
            from app.core import claude_cli_login

            if claude_cli_login.terminal_supported():
                try:
                    return {"terminal_run": claude_cli_login.login_terminal.start(home_env=home.env_for(account_id))}
                except RuntimeError as exc:
                    raise SubscriptionError("login_unavailable", str(exc)) from None
        return {"login": _login_state({"id": account_id, "status": "waiting"}, terminal=False)}


def start_login() -> dict:
    """Prepare a fresh folder and start ``claude auth login`` into it.

    With the in-app terminal (Docker, Linux) the login runs there; otherwise the
    owner runs the returned command on this worker. The sign-in becomes a saved,
    entry once ``claude auth status`` in that folder reports it.
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
    """Save the finished sign-in. Only an explicit owner switch selects it.

    Even a revision check in this worker's database cannot see a peer's switch
    while offline, so a late poll must never publish an owner-wide choice.
    """
    account = {"id": pending["id"], "kind": KIND_SAVED,
               "label": status.email or DEFAULT_LABEL}
    _record_identity(account, status)
    state["accounts"].append(account)
    state["pending"] = None
    _save(state)
    logger.info("Saved another Claude subscription sign-in")
    return account["id"]


def _signed_in_status(account_id: str):
    """``claude auth status`` for an intact pending folder that reports a sign-in, else None."""
    if not home.intact(account_id):
        return None
    status = _current_identity(account_id, force=True)
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
    caught up), the sign-in is kept without selecting it and its id is
    returned. Otherwise returns None.
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
    # Retry only explicitly deleted app-owned folders, not arbitrary unlisted
    # directories. Keep failed ids durable for the next startup.
    listed = {a["id"] for a in state["accounts"]}
    pending_id = (state.get("pending") or {}).get("id")
    for account_id in list(state["cleanup"]):
        if account_id in listed or account_id == pending_id:
            continue
        try:
            with _folder_lock:
                home.remove(account_id)
            state["cleanup"].remove(account_id)
            _save(state)
        except (home.HomeError, OSError):
            logger.warning("Could not finish removing a deleted Claude sign-in folder", exc_info=True)
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


"""Rebind the Claude-in-Chrome browser integration after an account switch.

Why this exists
---------------
The "Claude in Chrome" integration has TWO independent identities that must name
the same Claude account for browser tools to work:

* the **CLI OAuth token** on disk (``~/.claude/.credentials.json``) — this is
  what :mod:`app.core.claude_accounts` rewrites on every account switch, and
* the **browser's own claude.ai session** — the Chrome extension is signed into
  claude.ai independently (its cookies / IndexedDB live in the container's Chrome
  profile ``~/chrome-profile/Default``). That session decides which account's
  *device registry* the browser advertises itself in.

``list_connected_browsers`` runs under the CLI's OAuth account and lists only the
browsers registered under THAT account. So the instant account-failover-on-limit
rotates the CLI token from account A to account B, the container's browser is
still signed in to A: the CLI (now B) sees zero browsers, and any browser tool
call fails with "different claude.ai account". A stale
``chromeExtension.pairedDeviceId`` pin in ``~/.claude.json`` makes it worse — it
pins a device id that was registered under A and is invisible under B, so even a
later correct re-sign-in of the extension into B wouldn't be auto-selected.

What this module does (the automatic remediation)
--------------------------------------------------
On an account switch, when the integration is actually set up (container mode,
builtin enabled, and either a pin exists or a browser is currently bridged):

1. **Clear the stale pairing pin** (``chromeExtension`` block in
   ``~/.claude.json``). This is the one piece of cross-account state we can
   safely fix headlessly, and clearing it is *required* so that once the browser
   is re-signed into the new account the CLI auto-selects it (a single connected
   browser is auto-selected only when nothing stale is pinned).
2. **Optionally restart the supervised Chrome** (``CHROME_REBIND_RESTART=1``) to
   tear down the now-cross-account native-messaging bridge. Off by default: the
   browser's claude.ai session is persisted in the profile volume, so a restart
   re-signs the extension into the SAME (old) account — it drops the stale
   bridge but does not itself rebind, and it interrupts any in-flight browser
   work in other sessions. It is exposed for operators who prefer a clean
   "disconnected" status over a misleading "connected (wrong account)" one.
3. **Emit an actionable notification** telling the user the browser must be
   re-signed into the new account via *Settings → Set up browser*.

Hard limit (documented, not a bug): fully re-authenticating the browser's
claude.ai session into the new account cannot be done headlessly — it needs the
new account's interactive claude.ai sign-in (password / 2FA), which the app does
not hold. So this remediation makes the desync *self-clearing and visible*
rather than silent, and prepares the state so the one manual re-sign-in works
cleanly. Everything here is best-effort and NEVER raises — it runs downstream of
the credential switch and must not break failover/retry.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import signal
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_HOME = Path.home()
_CLAUDE_JSON = _HOME / ".claude.json"
_PROFILE_DIR = _HOME / "chrome-profile"
_BRIDGE_NAME_PREFIX = "claude-mcp-browser-bridge-"


# ---------------------------------------------------------------------------
# Environment / eligibility probes (kept independent of chrome_mcp.py so this
# hot-path helper never drags in that router's httpx/websockets imports).
# ---------------------------------------------------------------------------

def _is_container() -> bool:
    """Container deployment (supervised headless Chrome) vs native desktop.

    Native installs drive the user's OWN desktop Chrome, whose claude.ai sign-in
    the user manages in the browser directly — nothing here to rebind.
    """
    try:
        from app.core.platform import is_docker_mode
        return is_docker_mode()
    except Exception:  # noqa: BLE001
        return False


def _chrome_enabled() -> bool:
    """True iff the ``claude-in-chrome`` builtin is enabled (not disabled)."""
    try:
        from app.core.builtin_mcp_servers import BROWSER_SERVER_NAME
        from app.core.mcp_service import get_mcp_service
        return BROWSER_SERVER_NAME not in set(get_mcp_service().get_disabled_servers())
    except Exception:  # noqa: BLE001
        return False


def _bridge_active() -> bool:
    """True if a browser⇄CLI native-messaging bridge socket exists right now."""
    patterns = {
        f"/tmp/{_BRIDGE_NAME_PREFIX}*",
        os.path.join(tempfile.gettempdir(), _BRIDGE_NAME_PREFIX + "*"),
    }
    try:
        return any(glob.glob(p) for p in patterns)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Stale-pairing clear (~/.claude.json → chromeExtension)
# ---------------------------------------------------------------------------

def _clear_stale_pairing() -> bool:
    """Remove the ``chromeExtension`` pairing pin from ``~/.claude.json``.

    Returns True iff a pin (``pairedDeviceId``) was present and removed. Atomic
    (temp file + ``os.replace``) so a concurrent CLI reader never sees a partial
    write. Never raises.
    """
    try:
        try:
            raw = _CLAUDE_JSON.read_text()
        except OSError:
            return False
        try:
            data = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False

        ext = data.get("chromeExtension")
        if not isinstance(ext, dict) or not ext.get("pairedDeviceId"):
            return False

        # Drop the whole chromeExtension block — every field in it
        # (pairedDeviceId / pairedDeviceName) is bound to the previous account.
        data.pop("chromeExtension", None)

        fd, tmp_path = tempfile.mkstemp(
            dir=str(_CLAUDE_JSON.parent), prefix=".claude.json.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, _CLAUDE_JSON)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False

        logger.info(
            "Cleared stale Claude-in-Chrome pairing pin after account switch "
            "(browser was bound to the previous account)."
        )
        return True
    except Exception as e:  # noqa: BLE001 — must never break the switch path
        logger.warning(f"Could not clear stale chrome pairing (ignored): {e}")
        return False


# ---------------------------------------------------------------------------
# Optional supervised-Chrome restart (env-gated, off by default)
# ---------------------------------------------------------------------------

def _restart_supervised_chrome() -> bool:
    """SIGTERM the supervised headless Chrome so its supervisor respawns it.

    Matches ONLY our own profile's Chrome (``--user-data-dir=<profile>``), never
    an unrelated headless Chromium sharing the box (a Remotion/Puppeteer render),
    mirroring ``docker/start-chrome.sh``'s stale-lock guard. The
    ``chrome-supervisor.sh`` loop restarts Chrome on exit, so a plain SIGTERM is
    a restart. Returns True if at least one process was signalled. Never raises.
    """
    # /proc/<pid>/cmdline is a NUL-separated arg vector, so match on WHOLE args
    # rather than a substring — a substring test would also hit a sibling profile
    # whose path merely starts with ours (e.g. ``chrome-profile-2``) and SIGTERM
    # an unrelated Chrome. Chrome may pass the flag combined (``--user-data-dir=<p>``)
    # or split (``--user-data-dir`` ``<p>``); accept either, exactly.
    combined = f"--user-data-dir={_PROFILE_DIR}".encode()
    flag = b"--user-data-dir"
    value = str(_PROFILE_DIR).encode()
    signalled = False
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read()
            except OSError:
                continue
            args = cmd.split(b"\x00")
            if combined not in args and not (flag in args and value in args):
                continue
            # The main browser process is the one whose parent is the supervisor
            # (i.e. not a renderer with --type=). Signal only the top process;
            # killing it takes its children down and the supervisor respawns.
            if b"--type=" in cmd:
                continue
            try:
                os.kill(int(pid), signal.SIGTERM)
                signalled = True
            except OSError:
                continue
    except OSError:
        return False
    if signalled:
        logger.info("Signalled supervised Chrome to restart after account switch.")
    return signalled


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

async def rebind_browser_after_account_switch(
    *,
    session_id: Optional[str],
    api_user_id: Optional[str],
    new_account_name: Optional[str],
) -> None:
    """Clear stale browser binding + notify, after the active account changed.

    Safe to call on EVERY account switch: it silently no-ops unless the
    integration is genuinely set up in a container (builtin enabled, and a pin
    or a live bridge exists). Never raises.

    Trigger points: the auto-failover switch (:mod:`app.core.account_failover`)
    and the admin activate route (:mod:`app.api.claude_accounts`).
    """
    try:
        if not _is_container():
            return
        if not _chrome_enabled():
            return

        had_pin = _clear_stale_pairing()
        bridge = _bridge_active()

        # Never set up on this container (no pin, no live browser) → nothing to
        # rebind and nothing to warn about.
        if not had_pin and not bridge:
            return

        if os.environ.get("CHROME_REBIND_RESTART") == "1":
            _restart_supervised_chrome()

        who = f" to {new_account_name}" if new_account_name else ""
        try:
            from app.services.notification_service import emit_notification
            await emit_notification(
                api_user_id or "admin",
                severity="warning",
                title="Reconnect Claude in Chrome",
                message=(
                    f"Switched Claude account{who}. The in-container browser is "
                    "still signed in to the previous account, so browser tools "
                    "won't work until you reconnect it. Open Settings → Set up "
                    "browser and sign the Claude extension into the new account."
                ),
                category="chrome_rebind",
                surface="banner",
                session_id=session_id,
                persist=True,
            )
        except Exception:  # noqa: BLE001
            logger.debug("chrome_rebind notification emit failed", exc_info=True)

        logger.info(
            "Chrome rebind after account switch: cleared_pin=%s bridge_was_active=%s "
            "(browser needs manual re-sign-in for the new account)",
            had_pin, bridge,
        )
    except Exception as e:  # noqa: BLE001 — must never break the switch/retry path
        logger.warning(f"rebind_browser_after_account_switch failed (ignored): {e}")

"""Owner-controlled lifecycle of the unmodified, native Claude Code install."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fluid.tools.child_env import scrub_child_env

from app.core import claude_cli_status, cli_update_check
from app.core.config import settings
from app.core.platform import is_docker_mode
from app.db import database

TIMEOUT_SECONDS = 600
# "Keep Claude Code installed" was removed. Images built before that still
# read this file when the container starts and install Claude Code while it
# says 1, or while it is missing and a Claude home exists.
LEGACY_MARKER = "claude-cli-keep-installed"
# Anthropic's release channels: each file holds one version number.
RELEASES_URL = "https://downloads.claude.ai/claude-code-releases/"
CHECK_TIMEOUT_SECONDS = 15
# The running job's process, so a job orphaned by a worker restart still
# blocks a second one (and is stopped once past the time limit).
JOB_RECORD = "claude-cli-job.json"
IN_USE_MESSAGE = ("Claude Code is running (a Claude chat or a terminal is using it). "
                  "Stop it, then remove Claude Code.")
ORPHAN_MESSAGE = ("A Claude Code install or update started before the worker restarted "
                  "is still running. Try again in a few minutes.")
_lock = threading.RLock()
_job = None
logger = logging.getLogger(__name__)


def _key(name):
    return name + ":" + settings.effective_instance_id


def retire_keep_installed():
    """Stop an older Docker image's entrypoint from installing Claude Code.

    Newer images never read the file. Nothing is installed or removed here.
    """
    if platform() != "docker":
        return
    directory = settings.effective_data_dir
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as marker:
        marker.write("0\n")
        name = marker.name
    os.replace(name, directory / LEGACY_MARKER)


def platform():
    return "docker" if is_docker_mode() else "desktop" if os.environ.get("SHUFFLE_DESKTOP") == "1" else "local"


def _launcher():
    return Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")


def _owned(cli):
    # Never delete an unrelated npm/system install or follow the launcher symlink.
    native = not cli.binary_path or Path(cli.binary_path).absolute() == _launcher().absolute()
    recorded = database.get_system_setting(_key("claude_cli_installed_by_app")) == "1"
    docker_install = platform() == "docker" and (
        bool(cli.binary_path) or (Path.home() / ".local" / "share" / "claude").exists())
    return native and (recorded or docker_install)


def _in_use():
    """Pids of running processes started from this native install."""
    try:
        import psutil
    except Exception:  # noqa: BLE001 — optional; then nothing can be proven in use
        return []
    launcher, share = _launcher(), Path.home() / ".local" / "share" / "claude"
    launchers = {launcher.absolute(), launcher.resolve()}
    shares = {share.absolute(), share.resolve()}
    pids = []
    for proc in psutil.process_iter(["exe"]):
        exe = proc.info.get("exe")
        if exe and (Path(exe) in launchers or any(s in Path(exe).parents for s in shares)):
            pids.append(proc.pid)
    return pids


def _job_record():
    return settings.effective_data_dir / JOB_RECORD


def _forget_job():
    try:
        _job_record().unlink(missing_ok=True)
    except OSError:
        pass


def _record_job(pid):
    import psutil
    record = {"pid": pid, "created": psutil.Process(pid).create_time()}
    _job_record().write_text(json.dumps(record))


def _clear_orphan():
    """Refuse while a job from an earlier worker process runs; stop it past the limit."""
    try:
        import psutil
        record = json.loads(_job_record().read_text())
        proc = psutil.Process(int(record["pid"]))
        if abs(proc.create_time() - float(record["created"])) > 1:
            raise LookupError("pid reused")
    except Exception:  # noqa: BLE001 — no record, or its process is gone
        _forget_job()
        return
    if time.time() - float(record["created"]) < TIMEOUT_SECONDS:
        raise ValueError(ORPHAN_MESSAGE)
    for child in [*proc.children(recursive=True), proc]:
        try:
            child.kill()
        except Exception:  # noqa: BLE001 — already gone
            pass
    _forget_job()


def payload():
    cli = claude_cli_status.get_status()
    with _lock:
        job = dict(_job) if _job else None
    owned = _owned(cli)
    return {"installed": cli.installed, "version": cli.cli_version, "path": cli.binary_path,
            "check_updates": cli_update_check.enabled(cli_update_check.CLAUDE),
            "latest_version": (cli_update_check.latest_version(cli_update_check.CLAUDE, cli.cli_version)
                               if cli.installed else None),
            "installed_by_app": owned,
            "can_install": sys.platform in ("linux", "darwin", "win32"),
            "can_remove": owned, "platform": platform(), "job": job}


def _now():
    return datetime.now(timezone.utc).isoformat()


# The installer and updater see only what they need to download and place
# the CLI: the search path, the home and config folders, locale, temp
# folders, proxies, CA bundles, npm location/registry settings and Claude
# Code's mTLS client certificate. Credentials and other worker settings in
# the environment (API keys, database URLs, tokens) are never passed on.
_PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")
# npm settings an npm-installed CLI's "Check for update" may only have in the
# environment: where it lives, which registry, which CA, which proxy. Named
# one by one, never NPM_CONFIG_* as a prefix: npm also reads registry auth
# tokens from npm_config__auth* / "//host/:_authToken" variables, and those
# stay out like every other credential.
_NPM_ENV = tuple(f"NPM_CONFIG_{name}" for name in (
    "PREFIX", "REGISTRY", "CAFILE", "CA", "STRICT_SSL", "PROXY", "HTTPS_PROXY",
    "NOPROXY", "USERCONFIG", "GLOBALCONFIG", "CACHE",
))
_INSTALLER_ENV = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "TMPDIR", "TMP", "TEMP", "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    *_PROXY_ENV, *(name.lower() for name in _PROXY_ENV),
    *_NPM_ENV, *(name.lower() for name in _NPM_ENV),
    # Claude Code's mTLS settings, for a proxy that requires a client certificate.
    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY", "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE",
    # Windows: PowerShell and Anthropic's install.ps1 need these.
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "USERNAME",
    "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PSMODULEPATH", "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_ARCHITEW6432", "NUMBER_OF_PROCESSORS", "OS",
})


def _installer_env():
    # Windows environment names are case-insensitive (os.environ uppercases them).
    fold = str.upper if sys.platform == "win32" else str
    env = {k: v for k, v in os.environ.items() if fold(k) in _INSTALLER_ENV}
    # `claude update` and the installer use the active saved sign-in's folder,
    # like every other spawn (only CLAUDE_CONFIG_DIR, never a credential).
    from app.core import claude_subscriptions
    try:
        claude_subscriptions.apply_cli_home(env)
    except claude_subscriptions.SubscriptionError:
        pass  # Updating the CLI does not run a model or require sign-in.
    path_key = next((k for k in env if fold(k) == "PATH"), "PATH")
    env[path_key] = str(_launcher().parent) + os.pathsep + env.get(path_key, "")
    return env


def _run_command(command):
    env = _installer_env()
    # A file bounds memory even if an installer is noisy. Only the final lines
    # are exposed. No CLI auth commands or credential files are read here.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=sys.platform != "win32")
        timed_out = False
        try:
            try:
                _record_job(proc.pid)
            except Exception:  # noqa: BLE001 — the in-process lock still holds
                pass
            proc.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15, env=scrub_child_env())
            else:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=15)
        finally:
            _forget_job()
        output.seek(0, 2)
        output.seek(max(0, output.tell() - 8192))
        tail = "\n".join(output.read().decode("utf-8", errors="replace").splitlines()[-40:])
    return proc.returncode, tail, timed_out


def _execute(kind):
    state, message, tail = "failed", None, None
    try:
        before = claude_cli_status.get_status(force=True)
        if kind == "remove":
            if not _owned(before):
                raise ValueError("This Claude Code install was not installed by AI Shuffle.")
            if _in_use():
                raise ValueError(IN_USE_MESSAGE)
            # Anthropic's native uninstall: launcher and ~/.local/share/claude.
            # Deliberately never touch ~/.claude or ~/.claude.json.
            _launcher().unlink(missing_ok=True)
            versions = Path.home() / ".local" / "share" / "claude"
            if versions.is_symlink():
                versions.unlink()
            elif versions.exists():
                shutil.rmtree(versions)
            database.set_system_setting(_key("claude_cli_installed_by_app"), "0")
            state, message = "succeeded", "Claude Code removed. Your sign-in and settings were kept."
        else:
            if kind == "install":
                command = (["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                            "irm https://claude.ai/install.ps1 | iex"] if sys.platform == "win32" else
                           ["bash", "-c", "set -o pipefail; curl -fsSL https://claude.ai/install.sh | bash"])
            else:
                if not before.installed:
                    raise ValueError("Claude Code is not installed. Install it in Settings > Providers > Claude.")
                command = [before.binary_path, "update"]
            code, tail, timed_out = _run_command(command)
            if timed_out:
                message = "Claude Code installation or update timed out after 10 minutes. Check your network and try again."
            elif code:
                message = "Claude Code installation or update failed. Check your network connection and try again."
            else:
                claude_cli_status.invalidate()
                after = claude_cli_status.get_status(force=True)
                if not after.installed:
                    message = "The installer finished but Claude Code was not found. Check the installer output and try again."
                else:
                    if kind == "install":
                        database.set_system_setting(_key("claude_cli_installed_by_app"), "1")
                    state = "succeeded"
                    message = (f"Claude Code updated from {before.cli_version or 'unknown'} to {after.cli_version or 'unknown'}."
                               if kind == "update" else "Claude Code installed.")
    except Exception as exc:
        message = str(exc)
    finally:
        claude_cli_status.invalidate()
        if state == "succeeded":
            # What the last check found no longer describes this install.
            cli_update_check.forget(cli_update_check.CLAUDE)
        with _lock:
            _job.update(state=state, finished_at=_now(), message=message, output_tail=tail or None)
        logger.log(logging.INFO if state == "succeeded" else logging.WARNING,
                   "Claude Code %s %s: %s", kind, state, message)


def start(kind):
    global _job
    if kind not in ("install", "update", "remove"):
        raise ValueError("Unknown Claude Code action.")
    with _lock:
        if _job and _job["state"] == "running":
            return payload()
        if kind == "remove" and not _owned(claude_cli_status.get_status()):
            raise ValueError("This Claude Code install was not installed by AI Shuffle. Remove it manually instead.")
        if kind == "remove" and _in_use():
            raise ValueError(IN_USE_MESSAGE)
        _clear_orphan()
        _job = {"kind": kind, "state": "running", "started_at": _now(), "finished_at": None,
                "message": None, "output_tail": None}
        threading.Thread(target=_execute, args=(kind,), daemon=True, name="claude-cli-install").start()
    return payload()


def _release_channel(env):
    """The channel the CLI's own updater follows: ``stable`` when the owner
    chose it in Claude Code's settings, otherwise ``latest``."""
    home = env.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    try:
        chosen = json.loads((Path(home) / "settings.json").read_text(encoding="utf-8")).get("autoUpdatesChannel")
    except Exception:  # noqa: BLE001 — no settings file, or not JSON
        chosen = None
    return "stable" if chosen == "stable" else "latest"


def latest_release():
    """The version Anthropic currently publishes on this install's channel.

    One small download of a version number. Nothing is installed.
    """
    import httpx
    env = _installer_env()
    response = httpx.get(RELEASES_URL + _release_channel(env), timeout=CHECK_TIMEOUT_SECONDS,
                         follow_redirects=True)
    version = response.raise_for_status().text.strip()
    if not cli_update_check.version_key(version):
        raise ValueError("Anthropic's release channel did not name a version.")
    return version


def _check(installed):
    try:
        latest = latest_release()
    except Exception as exc:  # noqa: BLE001 — offline is normal; the worker runs on
        logger.info("Claude Code CLI: could not check for an update (%s)", exc)
        return
    if cli_update_check.record(cli_update_check.CLAUDE, installed, latest):
        logger.info("Claude Code CLI: %s is available (installed: %s). Update it in "
                    "Settings > Providers > Claude.", latest, installed)
    else:
        logger.info("Claude Code CLI: %s is up to date", installed)


def check_on_startup(cli):
    """Ask whether a newer Claude Code exists when the worker starts. True when asked.

    Only a check: the answer becomes a notice with an Update button, and
    nothing is downloaded until the owner presses it. Off when the owner turned
    the check off for this worker, and never from the test suite.
    """
    if not cli.installed or not cli.cli_version or cli_update_check.suppressed():
        return False
    if not cli_update_check.enabled(cli_update_check.CLAUDE):
        return False
    threading.Thread(target=_check, args=(cli.cli_version,), daemon=True,
                     name="claude-cli-update-check").start()
    return True

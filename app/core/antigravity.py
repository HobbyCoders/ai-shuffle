"""Google Antigravity as a delegated leg: the app-side half.

The agent is Google's own, unmodified ``agy`` binary, driven over the Agent
Client Protocol (``agy acp``) by fluid's generic ACP engine
(``fluid.engine.acp``). This module owns what the app adds around it:

* where the binary lives and how it is installed (Google's own update
  manifest, sha512-checked);
* one private home directory per provider row, so each row's sign-in stays the
  binary's own business on the worker that signed in;
* sign-in through the binary's own terminal flow (``agy acp --login``);
* the interactive-only guard for personal Google sign-ins;
* the on-demand quota read through the binary's own ``/usage`` command.

Terms (Google Antigravity terms section 6, FAQ): only Google's binary talks to
Google. This module never opens, copies, syncs or refreshes the sign-in the
binary stores in its home; it only starts the binary there and reads its
answers. ``tests/core/test_antigravity_no_token_access.py`` enforces that no
credential file name appears in app/ or fluid/.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from fluid.engine.acp import antigravity as agy_spec
from fluid.engine.acp.client import AcpConnection, AcpError

logger = logging.getLogger(__name__)

PROVIDER_TYPE = agy_spec.PROVIDER
#: Presence capability a worker advertises once it can run Antigravity rows.
CAPABILITY = "antigravity_acp_v1"

#: ACP auth method ids a row may hold in ``auth_mode``.
AUTH_MODES = (agy_spec.OAUTH_PERSONAL, agy_spec.OAUTH_BUSINESS, agy_spec.GEMINI_API_KEY)
#: Sign-ins the binary keeps in its own home (no key on the row).
STORED_SIGN_IN = (agy_spec.OAUTH_PERSONAL, agy_spec.OAUTH_BUSINESS)
#: Auth modes under the interactive-only guard.
GUARDED_AUTH_MODES = (agy_spec.OAUTH_PERSONAL, agy_spec.TERMINAL_LOGIN)

#: Reserved ``extra_env`` key on an Antigravity row: "1" lifts the guard.
#: Never passed to the agent.
GUARD_OVERRIDE_KEY = "AI_SHUFFLE_ANTIGRAVITY_ALLOW_UNATTENDED"

#: Test and development hook: run this executable instead of the installed one.
BINARY_ENV = "AI_SHUFFLE_ANTIGRAVITY_BIN"

#: Google's update manifests, one per ``<os>_<arch>`` key (``{version, url, sha512}``).
MANIFEST_BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/"
#: ``sys.platform`` → Google's OS name, ``platform.machine()`` → its arch name.
_MANIFEST_OS = {"linux": "linux", "win32": "windows", "darwin": "darwin"}
_MANIFEST_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
_ALLOWED_DOWNLOAD_HOSTS = ("storage.googleapis.com",)
DASHBOARD_URL = "https://antigravity.google/docs/faq"
TERMS_URL = "https://antigravity.google/terms"

#: The status and sign-in answer: both are read on the provider's own screen.
NOT_INSTALLED_HERE = ("Antigravity is not installed on this worker. "
                      "Install it under Antigravity agent below.")

WARNING_TEXT = (
    "Google's Antigravity terms (section 6) and FAQ restrict using a personal "
    "Google sign-in (consumer plans) through third-party software, and Google "
    "has suspended accounts for it. AI Shuffle runs Google's own unmodified "
    "Antigravity agent and never touches its sign-in, but the account is yours "
    "to risk. A Gemini API key or a business sign-in is the supported route."
)

GUARD_REFUSAL = (
    "This Antigravity provider uses a personal Google sign-in, which runs only in "
    "chats you type into yourself. It was refused for {reason}. Pick another "
    "provider for this run, or allow unattended use on the provider in "
    "Settings → Providers → Google Antigravity."
)

# What the sending agent reads when its MessageSession is refused up front:
# nothing reaches the chat, so it is not left failed by a refused turn.
AGENT_MESSAGE_REFUSAL = (
    "Not sent: chat \"{title}\" runs on a personal Google sign-in (Google "
    "Antigravity), which takes turns only from its owner typing in it. Tell the "
    "owner instead; they can allow unattended use on that provider in Settings → "
    "Providers → Google Antigravity."
)


def _thinking(levels: List[str]) -> Dict[str, Any]:
    # No default: the agent keeps its own level until the owner picks one.
    return {"version": 1, "effort_levels": levels, "default_effort": None}


#: Models ``agy`` 1.5.6 offered a non-first-party client (spike, 2026-11-11),
#: with the thinking levels (``thought_level``) the picker offers. 1.5.6
#: listed low/high for Gemini 3.1 Pro; the others use Google's documented
#: Gemini thinking levels. The agent's own list wins at session start; these
#: seed the picker and model curation (``POST /model-providers/{id}/fetch-models``).
DEFAULT_MODELS: List[Dict[str, Any]] = [
    {"id": "gemini-3.1-pro", "label": "Gemini 3.1 Pro", "reasoning_capabilities": _thinking(["low", "high"])},
    {"id": "gemini-4-argon", "label": "Gemini 4 Argon",
     "reasoning_capabilities": _thinking(["low", "medium", "high"])},
    {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash",
     "reasoning_capabilities": _thinking(["low", "medium", "high"])},
    {"id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash",
     "reasoning_capabilities": _thinking(["low", "medium", "high"])},
    {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash",
     "reasoning_capabilities": _thinking(["low", "medium", "high"])},
]

_URL_RE = re.compile(r"https://accounts\.google\.com/\S+")
_LOGIN_PROMPT_RE = re.compile(r"please visit the following url[^:]*:|enter the authorization code\s*:?", re.I)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# A line saying how the code went; the binary may keep running after it.
_LOGIN_RESULT_RE = re.compile(r"succe|signed in|logged in|authenticated|fail|invalid|error", re.I)
# The agent's own verdict that Google refused the code (e.g. "invalid_grant").
_LOGIN_REJECTED_RE = re.compile(r"invalid_grant|fail|invalid|denied|error", re.I)
CODE_REJECTED = ("Google did not accept that code. A code works once and only for a few "
                 "minutes: start the sign-in again, open the new link and paste the code it shows.")
ALREADY_SIGNED_IN = ("Already signed in on this worker. To use a different Google account, "
                     "sign out first.")
_LOGIN_IDLE_SECONDS = 15 * 60
_LOGIN_COMPLETE_SECONDS = 90
_PROBE_SECONDS = 20
_USAGE_SECONDS = 25


# ---------------------------------------------------------------------------
# Paths and binary
# ---------------------------------------------------------------------------

def _root() -> Path:
    from app.core.config import settings
    return Path(settings.effective_data_dir) / "antigravity"


def home_for(row_id: str) -> str:
    """The private home the binary runs in for one provider row."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(row_id or "default"))
    path = _root() / "homes" / safe
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return str(path)


def _current_record() -> Path:
    return _root() / "cli" / "current"


def _recorded_binary() -> Optional[str]:
    """The app's own install (``<data>/antigravity/cli/<version>/agy[.exe]``)."""
    try:
        recorded = _current_record().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if recorded and os.path.isfile(recorded) and os.access(recorded, os.X_OK):
        return recorded
    return None


def locate_binary() -> tuple[Optional[str], Optional[str]]:
    """``(path, source)``: source is ``override``, ``app`` or ``path`` (None when absent)."""
    override = os.environ.get(BINARY_ENV, "").strip()
    if override:
        return (override, "override") if os.path.isfile(override) else (None, None)
    recorded = _recorded_binary()
    if recorded:
        return recorded, "app"
    # PATHEXT makes this find agy.exe on Windows.
    found = shutil.which("agy")
    return (found, "path") if found else (None, None)


def find_binary() -> Optional[str]:
    """The executable to run: the override, the app's install, then PATH."""
    return locate_binary()[0]


async def binary_version(binary: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "--version", stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=agy_spec.agent_env(tempfile.gettempdir()),
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return None
    text = _ANSI_RE.sub("", out.decode("utf-8", "replace")).strip()
    match = re.search(r"\d+\.\d+(?:\.\d+)?(?:[-+.][\w.]+)?", text)
    return match.group(0) if match else (text.splitlines()[0][:80] if text else None)


# ---------------------------------------------------------------------------
# Install (Google's own manifest; one background job per worker)
# ---------------------------------------------------------------------------

def _platform() -> tuple[str, str]:
    """``(sys.platform, platform.machine())`` of this worker."""
    import platform
    return sys.platform, platform.machine()


def manifest_key(system: Optional[str] = None, machine: Optional[str] = None) -> Optional[str]:
    """Google's manifest key for this worker (``windows_amd64``…), or None when
    Google publishes no build for it. Defaults to this worker's platform."""
    if system is None or machine is None:
        here = _platform()
        system, machine = (here[0] if system is None else system), (here[1] if machine is None else machine)
    os_name = next((v for k, v in _MANIFEST_OS.items() if (system or "").startswith(k)), None)
    arch = _MANIFEST_ARCH.get((machine or "").strip().lower())
    return f"{os_name}_{arch}" if os_name and arch else None


def manifest_url(key: str) -> str:
    return f"{MANIFEST_BASE}{key}.json"


def unsupported_message() -> str:
    system, machine = _platform()
    return (f"Google publishes no Antigravity build for this worker ({system or 'unknown'} "
            f"{machine or 'unknown'}). Antigravity runs on Windows, macOS and Linux, on x64 or arm64.")


@dataclass
class InstallJob:
    kind: str = "install"  # install | update
    state: str = "idle"  # idle | running | succeeded | failed
    version: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None


_install = InstallJob()
_install_task: Optional[asyncio.Task] = None
JOB_KINDS = ("install", "update")
#: A worker checks for a newer Antigravity once per start; one restarting in a
#: loop asks Google at most this often.
STARTUP_CHECK_SECONDS = 600
UP_TO_DATE = "Antigravity is up to date ({version})."


def _version_key(text: Optional[str]) -> Optional[tuple]:
    """``1.5.7`` → ``(1, 5, 7)``; None when the text is not a plain dotted version
    (``1.5.7-rc1`` included: such versions compare by equality only)."""
    match = re.fullmatch(r"\d+(?:\.\d+)*", (text or "").strip())
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def _is_current(installed: Optional[str], latest: str) -> bool:
    """Whether ``installed`` is ``latest`` or newer: a job never downgrades."""
    if not installed:
        return False
    if installed == latest:
        return True
    have, want = _version_key(installed), _version_key(latest)
    return have is not None and want is not None and have >= want


def install_state() -> Dict[str, Any]:
    return {"kind": _install.kind, "state": _install.state, "version": _install.version,
            "error": _install.error, "message": _install.message}


def start_install(kind: str = "install") -> Dict[str, Any]:
    """Start (or join) the install or update check of Google's latest release.

    One job per worker: a second start while one runs returns the running one.
    """
    global _install_task, _install
    if kind not in JOB_KINDS:
        raise ValueError("Unknown Antigravity action.")
    if _install_task is not None and not _install_task.done():
        return install_state()
    key = manifest_key()
    if key is None:
        message = unsupported_message() + " Install Google's Antigravity CLI yourself so `agy` is on PATH."
        _install = InstallJob(kind=kind, state="failed", error=message, message=message,
                              finished_at=time.time())
        return install_state()
    _install = InstallJob(kind=kind, state="running", started_at=time.time())
    _install_task = asyncio.get_running_loop().create_task(_run_install(_install, key))
    return install_state()


def start_update() -> Dict[str, Any]:
    """Check for update: the same job; it downloads only when Google has a newer release."""
    return start_install("update")


def _download_kind(url: str) -> tuple[str, str]:
    """``(format, file name)`` from the download's own suffix."""
    from urllib.parse import urlsplit
    path = urlsplit(url).path.lower()
    if path.endswith(".exe"):
        return "exe", "agy.exe"
    if path.endswith((".tar.gz", ".tgz")):
        return "tar", "agy"
    name = path.rsplit("/", 1)[-1] or url
    raise RuntimeError(f"Google's update manifest named a download this app cannot install ({name}).")


async def _run_install(job: InstallJob, key: str) -> None:
    from urllib.parse import urlsplit

    import httpx
    label = "Update check" if job.kind == "update" else "Install"
    try:
        before_path, source = locate_binary()
        before = await binary_version(before_path) if before_path else None
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            manifest = (await client.get(manifest_url(key))).raise_for_status().json()
            version, url, digest = (str(manifest.get(k) or "") for k in ("version", "url", "sha512"))
            if (not (version and url and digest) or not re.fullmatch(r"\d[\w.+-]{0,63}", version)
                    or urlsplit(url).hostname not in _ALLOWED_DOWNLOAD_HOSTS):
                raise RuntimeError("Google's update manifest did not name a download.")
            job.version = version
            if _is_current(before, version):
                # Already this version or newer (a check with nothing new, a
                # stale Install button, a second worker's screen, a release
                # Google rolled back): nothing is downloaded.
                job.state, job.message = "succeeded", UP_TO_DATE.format(version=before)
                return
            fmt, name = _download_kind(url)
            target = _root() / "cli" / re.sub(r"[^\w.-]", "_", version)
            target.mkdir(parents=True, exist_ok=True)
            if fmt == "exe":
                # Windows: the release is the executable itself.
                partial = target / (name + ".part")
                await _download(client, url, digest, partial)
                partial.chmod(partial.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                os.replace(partial, target / name)
            else:
                with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
                    archive = Path(tmp) / "cli.tar.gz"
                    await _download(client, url, digest, archive)
                    await asyncio.to_thread(_extract_binary, archive, target / name)
        _write_record(target / name)
        # The copy just replaced stays until the next update: a chat that
        # resolved it a moment ago may still be starting it. So does a copy
        # the override names.
        spared = [Path(p) for p in (before_path, os.environ.get(BINARY_ENV, "").strip()) if p]
        await asyncio.to_thread(prune_versions, target, spared)
        if before:
            job.message = f"Antigravity updated from {before} to {version}."
        else:
            job.message = f"Antigravity {version} installed."
        if source == "path":
            job.message += " AI Shuffle's own copy now runs instead of the agy on PATH."
        elif source == "override":
            job.message += f" {BINARY_ENV} still selects the binary this worker runs."
        job.state = "succeeded"
    except Exception as exc:  # noqa: BLE001 — reported to the owner as text
        logger.warning("[antigravity] %s failed", label.lower(), exc_info=True)
        job.state, job.error = "failed", f"{label} failed: {exc}"
        job.message = job.error
    finally:
        job.finished_at = time.time()
        # The startup check has nobody watching Settings: the log is its result.
        if job.state == "succeeded":
            logger.info("[antigravity] %s: %s", job.kind, job.message)


async def _download(client: Any, url: str, digest: str, dest: Path) -> None:
    """Stream ``url`` to ``dest``, checking Google's sha512; nothing is left on a mismatch."""
    sha = hashlib.sha512()
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as fh:
                async for chunk in response.aiter_bytes(1 << 20):
                    sha.update(chunk)
                    fh.write(chunk)
        if sha.hexdigest().lower() != digest.lower():
            raise RuntimeError("The download did not match Google's checksum.")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def _write_record(binary: Path) -> None:
    record = _current_record()
    partial = record.with_name(record.name + ".part")
    partial.write_text(str(binary), encoding="utf-8")
    os.replace(partial, record)


def prune_versions(keep: Path, spared: Optional[List[Path]] = None) -> List[str]:
    """Best effort: remove app-installed versions other than ``keep`` and the
    folders holding any ``spared`` executable.

    A folder whose executable cannot be removed (an ``agy.exe`` still running
    on Windows) is left whole for a later update; nothing is ever stopped.
    """
    removed: List[str] = []
    cli = _root() / "cli"
    kept = {keep.name}
    roots = {os.path.abspath(cli), os.path.realpath(cli)}
    for exe in spared or ():
        # The path as named and, behind a symlink, the file it points to.
        for path in {os.path.abspath(exe), os.path.realpath(exe)}:
            for root in roots:
                try:
                    rel = Path(path).relative_to(root)
                except ValueError:
                    continue
                if rel.parts:
                    kept.add(rel.parts[0])
    try:
        folders = [p for p in cli.iterdir() if p.is_dir() and p.name not in kept]
    except OSError:
        return removed
    for folder in folders:
        try:
            for exe in ("agy.exe", "agy"):
                (folder / exe).unlink(missing_ok=True)
        except OSError:
            logger.debug("[antigravity] kept %s: its executable is in use", folder, exc_info=True)
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed.append(folder.name)
    return removed


def _setting_key() -> str:
    from app.core.config import settings
    return "antigravity_cli_startup_check_at:" + settings.effective_instance_id


def update_on_startup() -> bool:
    """Check for a newer Antigravity when the worker starts. True when it started.

    The same job as "Check for update". Only for the app's own install, never
    one on PATH or behind the override, and never from the test suite or a
    branch preview. Throttled so a worker restarting in a loop does not keep
    asking Google; the job runs in the background and logs what it did.
    """
    if os.environ.get("TESTING") == "1" or os.environ.get("AI_SHUFFLE_PREVIEW") == "1":
        return False
    if locate_binary()[1] != "app" or manifest_key() is None:
        return False
    from app.db import database
    key = _setting_key()
    try:
        last = float(database.get_system_setting(key) or 0)
    except ValueError:
        last = 0.0
    now = time.time()
    if 0 <= now - last < STARTUP_CHECK_SECONDS:
        return False
    database.set_system_setting(key, str(now))
    start_update()
    return True


def _extract_binary(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        member = next((m for m in tar.getmembers()
                       if m.isreg() and os.path.basename(m.name) in ("antigravity", "agy")), None)
        if member is None:
            raise RuntimeError("The download held no Antigravity executable.")
        source = tar.extractfile(member)
        assert source is not None
        partial = dest.with_suffix(".part")
        with source, partial.open("wb") as out:
            shutil.copyfileobj(source, out)
    partial.chmod(partial.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(partial, dest)


# ---------------------------------------------------------------------------
# Rows, environment and status
# ---------------------------------------------------------------------------

def _row_extra_env(row: Mapping[str, Any]) -> Dict[str, str]:
    raw = row.get("extra_env_json")
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {str(k): str(v) for k, v in (raw or {}).items() if isinstance(k, str)}


def workers_requiring_update() -> List[str]:
    """Members that cannot run an Antigravity row yet (rows sync to every member).

    Same rule as ``native_providers.workers_requiring_update``: membership,
    not online presence, and support read from signed presence.
    """
    from app.db import network as store
    from app.network import engine

    ctx = engine.context()
    if ctx is None:
        return []
    presence = engine.all_presence()
    return [member.get("name") or member["device_id"]
            for member in store.list_members(include_revoked=False)
            if member["device_id"] != ctx.device_id
            and (presence.get(member["device_id"], {}).get("capabilities") or {}).get(CAPABILITY) != 1]


def guard_lifted(row: Mapping[str, Any]) -> bool:
    return _row_extra_env(row).get(GUARD_OVERRIDE_KEY) == "1"


def guarded(row: Mapping[str, Any]) -> bool:
    """Whether the interactive-only guard applies to this row now."""
    return (row.get("auth_mode") in GUARDED_AUTH_MODES) and not guard_lifted(row)


def guarded_now(provider_id: Optional[str]) -> bool:
    """Whether the guard applies to row ``provider_id`` right now, read fresh.

    For a decision only a guarded row restricts (steering text no person
    entered into its live turn). A row that cannot be read or is gone counts
    as guarded: the text then waits for its own turn, where the turn-start
    guard decides it. False for any other provider's row. Blocking DB read.
    """
    if not provider_id:
        return False
    try:
        from app.db import model_providers
        row = model_providers.get_model_provider(str(provider_id))
    except Exception:  # noqa: BLE001 — cannot tell: the restrictive answer
        logger.debug("[antigravity] could not read %s for a steer", provider_id, exc_info=True)
        return True
    if not row:
        return True
    return row.get("provider_type") == PROVIDER_TYPE and guarded(row)


def agent_extra_env(row: Mapping[str, Any]) -> Dict[str, str]:
    """The row's extra environment for the agent, without app-reserved keys."""
    return {k: v for k, v in _row_extra_env(row).items() if k != GUARD_OVERRIDE_KEY}


def row_api_key(row: Mapping[str, Any]) -> Optional[str]:
    """The row's Gemini API key, decrypted; only for the agent's environment."""
    if row.get("auth_mode") != agy_spec.GEMINI_API_KEY:
        return None
    from app.core.provider.fluid_credentials import _decrypt_auth_token
    return _decrypt_auth_token(dict(row))


def build_spec(row: Mapping[str, Any], *, add_dirs: Optional[List[str]] = None,
               binary: Optional[str] = None) -> agy_spec.AntigravitySpec:
    binary = binary or find_binary()
    if not binary:
        raise FileNotFoundError(
            "Antigravity is not installed on this worker. Install it from "
            "Settings → Providers → Google Antigravity.")
    from app import __version__ as app_version  # type: ignore[attr-defined]
    return agy_spec.AntigravitySpec.build(
        binary, auth_mode=str(row.get("auth_mode") or agy_spec.OAUTH_PERSONAL),
        home=home_for(str(row.get("id") or "default")), api_key=row_api_key(row),
        add_dirs=list(add_dirs or ()), extra_env=agent_extra_env(row), version=str(app_version),
    )


def _signed_in_marker(row_id: str) -> Path:
    # The app's own file, outside the binary's home: it records what the
    # binary last answered, never anything read from the sign-in.
    return _root() / "signed-in" / re.sub(r"[^A-Za-z0-9_.-]", "_", str(row_id))


def signed_in_here(row_id: str) -> bool:
    """Whether the binary last said this sign-in row can open sessions on
    this worker (a status check or a completed sign-in; cleared by a
    signed-out answer, sign-out and delete). Cheap: one stat, no process."""
    try:
        return bool(row_id) and _signed_in_marker(str(row_id)).is_file()
    except OSError:
        return False


def _note_signin(row_id: str, state: str) -> None:
    if not row_id or state not in ("signed_in", "signed_out"):
        return  # not installed or an error says nothing about the sign-in
    marker = _signed_in_marker(row_id)
    try:
        if state == "signed_in":
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        else:
            marker.unlink(missing_ok=True)
    except OSError:
        logger.debug("[antigravity] could not record the sign-in state of %s", row_id, exc_info=True)


async def probe(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Ask the binary whether this row can open sessions (see ``_probe``);
    a sign-in row records the answer for :func:`signed_in_here`."""
    epoch = signin_epoch(str(row.get("id") or ""))
    result = await _probe(row)
    # A sign-out or delete while the binary answered: its answer is stale.
    if row.get("auth_mode") in STORED_SIGN_IN and epoch == signin_epoch(str(row.get("id") or "")):
        _note_signin(str(row.get("id") or ""), str(result.get("state") or ""))
    return result


async def _probe(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Ask the binary itself whether this row can open sessions.

    ``initialize`` (+ ``authenticate`` for key rows), then ``session/list``:
    -32000 means signed out. The sign-in itself is never looked at.
    """
    try:
        spec = build_spec(row)
    except FileNotFoundError:
        # Read on the provider's own screen, which offers the install below.
        return {"state": "not_installed", "message": NOT_INSTALLED_HERE}
    conn = AcpConnection(spec.argv, cwd=home_for(str(row.get("id") or "default")), env=spec.env)
    try:
        await conn.start()
        info = await asyncio.wait_for(conn.initialize(spec.client_capabilities(), spec.client_info),
                                      timeout=_PROBE_SECONDS)
        if spec.auth_method:
            await conn.request("authenticate", spec.authenticate_params(), timeout=_PROBE_SECONDS)
        await conn.request("session/list", {}, timeout=_PROBE_SECONDS)
        return {"state": "signed_in", "agent": agy_spec.initialize_summary(info.raw)}
    except AcpError as exc:
        if exc.auth_required:
            return {"state": "signed_out", "message": spec.auth_message(exc)}
        return {"state": "error", "message": exc.message}
    except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
        return {"state": "error", "message": f"Antigravity did not answer: {exc or type(exc).__name__}"}
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Sign-in through the binary's own terminal flow
# ---------------------------------------------------------------------------

@dataclass
class LoginSession:
    id: str
    row_id: str
    proc: Any = None
    url: Optional[str] = None
    state: str = "starting"  # starting | awaiting_code | completing | signed_in | failed
    message: Optional[str] = None
    output: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    timer: Any = None

    def public(self) -> Dict[str, Any]:
        return {"login_id": self.id, "provider_id": self.row_id, "state": self.state,
                "url": self.url, "message": self.message}


_logins: Dict[str, LoginSession] = {}
_login_locks: Dict[str, asyncio.Lock] = {}


def _login_lock(row_id: str) -> asyncio.Lock:
    """Serializes one row's sign-in flow: start, cancel, sign-out, delete.

    Separate from the row's ``operation_lock`` because a start waits up to
    30 s for the link; a row writer that also needs this lock takes
    ``operation_lock`` first (delete), never the other way round.
    """
    return _login_locks.setdefault(str(row_id), asyncio.Lock())


def _kill(login: LoginSession) -> None:
    timer = getattr(login, "timer", None)
    if timer is not None:
        timer.cancel()
    proc = login.proc
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _drop(login: LoginSession) -> None:
    """Forget and kill one sign-in (sync: timers and shutdown call it)."""
    if _logins.get(login.id) is login:
        _logins.pop(login.id, None)
    _kill(login)


async def _stop_logins(row_id: str) -> None:
    """End every sign-in of the row and wait for its process to exit, so
    nothing it writes lands after the caller goes on (a sign-out's removal)."""
    for login in [x for x in _logins.values() if x.row_id == str(row_id)]:
        _drop(login)
        proc = login.proc
        if proc is not None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass


def _expire(login_id: str) -> None:
    login = _logins.get(login_id)
    if login is not None:
        _drop(login)


def shutdown_logins() -> None:
    """App shutdown: no sign-in process outlives the app."""
    for login in list(_logins.values()):
        _drop(login)


async def start_login(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Run ``agy acp --login`` in the row's home and return its sign-in link.

    At most one sign-in per row: a new start ends the previous one first,
    under the row's sign-in lock, so two starts cannot both leave a process.
    An abandoned sign-in is ended after ``_LOGIN_IDLE_SECONDS``.
    """
    if row.get("auth_mode") not in STORED_SIGN_IN:
        return {"state": "failed", "message": "This provider signs in with an API key."}
    if row.get("auth_mode") == agy_spec.OAUTH_BUSINESS:
        # Spike, agy 1.5.6: business sign-in needs a browser on the worker
        # ("No browser is available here"); there is no terminal flow for it.
        return {"state": "failed", "message": (
            "Antigravity offers business sign-in only through a browser on the worker. "
            "Use a Gemini API key here, or sign in from a desktop worker.")}
    row_id = str(row["id"])
    if signed_in_here(row_id) and (await probe(row)).get("state") == "signed_in":
        # Said plainly: a new sign-in would not replace the account, and any
        # code would then read as "signed in".
        return {"login_id": None, "provider_id": row_id, "state": "signed_in",
                "url": None, "message": ALREADY_SIGNED_IN}
    async with _login_lock(row_id):
        await _stop_logins(row_id)
        binary = find_binary()
        if not binary:
            return {"state": "not_installed", "message": NOT_INSTALLED_HERE}
        home = home_for(row_id)
        login = LoginSession(id=uuid.uuid4().hex, row_id=row_id)
        login.proc = await asyncio.create_subprocess_exec(
            binary, "acp", "--login", cwd=home, env=agy_spec.agent_env(home),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, start_new_session=os.name == "posix",
        )
        _logins[login.id] = login
        login.timer = asyncio.get_running_loop().call_later(_LOGIN_IDLE_SECONDS, _expire, login.id)
        try:
            await asyncio.wait_for(_read_until_url(login), timeout=30)
        except asyncio.TimeoutError:
            pass
        except BaseException:
            _drop(login)  # cancelled (shutdown, client gone): no process left behind
            raise
        if not login.url:
            _drop(login)
            login.state = "failed"
            login.message = "Antigravity did not print a sign-in link." + _tail(login)
        else:
            login.state = "awaiting_code"
        return login.public()


async def _read_until_url(login: LoginSession) -> None:
    assert login.proc and login.proc.stdout
    while login.url is None:
        line = await login.proc.stdout.readline()
        if not line:
            return
        text = _ANSI_RE.sub("", line.decode("utf-8", "replace")).rstrip()
        login.output.append(text)
        match = _URL_RE.search(text)
        if match:
            login.url = match.group(0)


def _tail(login: LoginSession) -> str:
    # Keep the agent's verdict, not its own prompts ("Please visit the
    # following URL…", "Enter the authorization code:"), which share a line
    # with the verdict because the code prompt ends without a newline.
    lines = [_LOGIN_PROMPT_RE.sub("", line).strip() for line in login.output if line and not _URL_RE.search(line)]
    lines = [line for line in lines if line][-3:]
    return (" " + " ".join(lines))[:400] if lines else ""


def get_login(login_id: str) -> Optional[LoginSession]:
    return _logins.get(login_id)


def pending_login(row_id: str) -> Optional[Dict[str, Any]]:
    """The row's sign-in waiting for its code, wherever it was started
    (another device or tab), so the screen offers its code entry and a
    cancel instead of a start that would replace it."""
    waiting = [x for x in _logins.values()
               if x.row_id == str(row_id) and x.state == "awaiting_code" and x.url]
    return max(waiting, key=lambda x: x.created_at).public() if waiting else None


async def complete_login(login: LoginSession, code: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    """Hand the owner's authorization code to the binary and check the result."""
    code = (code or "").strip()
    if not code or len(code) > 4096 or "\n" in code:
        return {**login.public(), "state": "failed", "message": "Paste the code or the full URL."}
    proc = login.proc
    if (_logins.get(login.id) is not login or login.state != "awaiting_code"
            or proc is None or proc.returncode is not None or proc.stdin is None):
        login.state, login.message = "failed", "The sign-in expired. Start it again."
        return login.public()
    login.state = "completing"  # no await since the check: one completion per sign-in
    mark = len(login.output)
    proc.stdin.write(code.encode() + b"\n")
    await proc.stdin.drain()

    async def drain() -> None:
        assert proc.stdout
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = _ANSI_RE.sub("", line.decode("utf-8", "replace")).rstrip()
            login.output.append(text)
            if _LOGIN_RESULT_RE.search(text):
                return

    try:
        await asyncio.wait_for(drain(), timeout=_LOGIN_COMPLETE_SECONDS)
    except asyncio.TimeoutError:
        pass
    # After the code, ``--login`` may go on to serve ACP on the same pipes:
    # end it either way, then ask a fresh process whether the sign-in holds.
    try:
        proc.stdin.close()
    except (OSError, RuntimeError):
        pass
    _kill(login)
    # A sign-out, delete or newer start may end this sign-in meanwhile.
    result = await probe(row) if _logins.get(login.id) is login else {}
    # The agent's own verdict on the code decides, not only the check after
    # it: a worker still holding an earlier sign-in answers "signed in"
    # whatever code was sent.
    rejected = any(_LOGIN_REJECTED_RE.search(line) for line in login.output[mark:])
    if _logins.get(login.id) is not login:
        login.state, login.message = "failed", "The sign-in was cancelled."
    elif rejected:
        login.state, login.message = "failed", CODE_REJECTED
        if result.get("state") == "signed_in":
            login.message += " This worker is still signed in with the earlier account."
    elif result.get("state") == "signed_in":
        login.state, login.message = "signed_in", None
    else:
        login.state = "failed"
        login.message = ("Antigravity did not accept the sign-in. Start the sign-in again; "
                         "if it fails again, check the worker's connection to Google." + _tail(login))
    _drop(login)
    return login.public()


async def cancel_login(login_id: str) -> bool:
    login = _logins.get(login_id)
    if login is None:
        return False
    async with _login_lock(login.row_id):
        _drop(login)
    return True


_signin_epochs: Dict[str, int] = {}


def signin_epoch(row_id: str) -> int:
    """Bumped each time this worker forgets the row's sign-in (sign-out,
    delete): a usage reading taken across a bump is not written."""
    return _signin_epochs.get(str(row_id), 0)


def _home_path(row_id: str) -> Path:
    return _root() / "homes" / re.sub(r"[^A-Za-z0-9_.-]", "_", str(row_id))


async def forget_row(row_id: str, *, reason: Optional[str] = None) -> None:
    """End the row's sign-in on this worker: its running agents, pending
    sign-ins, then its home.

    The directory is the binary's; nothing in it is read. Removing it is the
    only way the app ends a stored sign-in without touching its contents.
    Missing or partly removed is fine; a file another process holds open
    (Windows) stays until ``prune_orphan_homes`` runs again.
    """
    if not row_id:
        return
    _signin_epochs[str(row_id)] = signin_epoch(row_id) + 1
    # A warm agent keeps the sign-in it read at start: end it, so the next
    # turn cannot run on a credential that is gone.
    await stop_agents(row_id, reason or STOPPED_REMOVED)
    async with _login_lock(str(row_id)):
        await _stop_logins(str(row_id))
        _note_signin(str(row_id), "signed_out")
        path = _home_path(str(row_id))
        if path.is_dir():
            await asyncio.to_thread(shutil.rmtree, path, True)


async def sign_out(row: Mapping[str, Any]) -> None:
    """Forget a row's sign-in on this worker and its last usage reading."""
    row_id = str(row.get("id") or "")
    if not row_id:
        return
    await forget_row(row_id, reason=STOPPED_SIGNED_OUT)
    from app.db.provider_usage_snapshots import delete_provider_usage_snapshot
    await asyncio.to_thread(delete_provider_usage_snapshot, row_id)


def _antigravity_row_ids() -> set:
    from app.db.database import get_db

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM model_providers WHERE provider_type = %s", (PROVIDER_TYPE,))
        return {str(r["id"]) for r in cursor.fetchall()}


async def prune_orphan_homes() -> int:
    """Remove the home and sign-ins of every row that no longer exists here.

    Runs at startup and after a sync delete: covers a delete from another
    worker, one applied by a snapshot, a removal a previous process did not
    finish (killed, or a file held open on Windows). Lists the folders before
    reading the rows, so a row created meanwhile is never pruned.
    """
    homes = _root() / "homes"
    try:
        names = [p.name for p in homes.iterdir() if p.is_dir()] if homes.is_dir() else []
    except OSError:
        names = []
    pending = {x.row_id for x in _logins.values()}
    if not names and not pending:
        return 0
    try:
        live = await asyncio.to_thread(_antigravity_row_ids)
    except Exception:  # noqa: BLE001 — try again on the next delete or start
        logger.debug("[antigravity] could not list rows to prune homes", exc_info=True)
        return 0
    gone = (set(names) | pending) - {re.sub(r"[^A-Za-z0-9_.-]", "_", r) for r in live} - live
    for row_id in gone:
        await forget_row(row_id)
    return len(gone)


# ---------------------------------------------------------------------------
# Guard: personal sign-ins are interactive-only by default
# ---------------------------------------------------------------------------

_CONNECT_SOURCE: Any = object()

# The guard's own rule: only a turn the owner typed or spoke in the app's own
# chat UI passes. It reads the turn row's own provenance, two facts set by
# the host from that row (``turn_scheduler.turn_provenance``): the source it
# was admitted with, and whether a person entered it (``turns.entry_provenance``:
# only the positive ``turns.PERSON_ENTERED_KEY`` the host sets where the
# owner's own input arrives; Away's answers, voice-assistant requests, parked
# turns' automatic resumes and rows without the key are not). The
# source must be one of these (deliberately NOT
# ``turn_sources.PERSON_PRESENT_TURN_SOURCES``, which also admits "api", any
# public-API caller, and "connector", a chat connector message) AND a person
# must have entered it. "resume" (a Retry) takes both facts from the exact
# row it re-runs. Any other source, no source, or no person: refused.
OWNER_UI_TURN_SOURCES = frozenset(("user", "queue", "worktree"))  # == turns.OWNER_ENTRY_SOURCES
RETRY_TURN_SOURCE = "resume"

# Why a turn the owner did not type was started, by app ``turns.source``.
_SOURCE_REASONS = {
    "wakeup": "a scheduled wake-up",
    "auto_continue": "an automatic continuation",
    "automation": "an automation run",
    "automation_step": "an automation run",
    "unpark": "a background turn picked up again",
    "unpark_pump": "a background turn picked up again",
    "api": "a request through the API",
    "api_stream": "a request through the API",
    "connector": "a message from a chat connector",
    "provider_agent": "a Task or Swarm worker",
    "failover": "an automatic retry on another account",
    "watch": "a watch",
}
NO_SOURCE_REASON = "a run with no known origin"
#: An owner source no person entered (Away mode's answer, an automatic resume).
NOT_ENTERED_REASON = "a reply sent for you automatically, such as an answer Away mode gave"
#: A prompt the voice assistant wrote and sent itself (``voice_delegation``).
VOICE_REASON = "a request written by the voice assistant"
#: Forwarded by a worker too old to say whether a person entered it.
OUTDATED_WORKER_REASON = ("a message sent through a worker that needs updating (update that "
                          "worker, then send it again)")
#: Sent through the agent browser's preview link (an agent driving the app),
#: not by the owner (``session_modes.person_entry``).
PREVIEW_REASON = "a message sent through an agent's preview link"
_NOT_ENTERED_REASONS = {"voice": VOICE_REASON, "unstated": OUTDATED_WORKER_REASON,
                        "preview": PREVIEW_REASON}
#: A Retry the owner did not ask for (no key on the Retry's own row).
RETRY_NOT_OWNER_REASON = "a retry you did not ask for yourself"
#: A run another agent starts (Task, Swarm, a subagent, StartSession).
AGENT_RUN_REASON = "a run another agent starts"
API_REQUEST_REASON = _SOURCE_REASONS["api"]
#: ``X-Error-Code`` of a route that refuses a guarded row up front.
GUARD_REFUSED_CODE = "antigravity_guarded"


def _agent_message_reason(session_id: Optional[str]) -> str:
    """Names the agent whose message started this chat's live turn."""
    label = ""
    if session_id:
        try:
            from app.db import turns
            payload = (turns.get_live_turn(session_id) or {}).get("payload") or {}
            label = str(payload.get("peer_sender_label") or "").strip()
        except Exception:  # noqa: BLE001 — the refusal stands without the name
            logger.debug("[antigravity] could not name the sender for %s", session_id, exc_info=True)
    label = " ".join(label.split())[:80]
    return f'a message from another agent ("{label}")' if label else "a message from another agent"


def agent_message_refusal(session: Optional[Mapping[str, Any]], *,
                          fail_closed: bool = False) -> Optional[str]:
    """The MessageSession refusal for a chat on a guarded row, or None.

    Checked when a message is sent (and again when a queued one is
    delivered), so a refused message never starts a turn that fails the
    chat. At send time a failed read fails open to the turn-start guard,
    which refuses an ``agent_message`` turn on its own. ``fail_closed``
    (held messages that would ride the owner's turn, where that guard
    cannot tell them apart) re-raises the failed read instead.
    """
    provider_id = (session or {}).get("provider_id")
    if not provider_id:
        return None
    try:
        from app.db import model_providers
        row = model_providers.get_model_provider(str(provider_id))
    except Exception:  # noqa: BLE001 — the turn-start guard still applies
        if fail_closed:
            raise
        logger.debug("[antigravity] could not read %s for a message", provider_id, exc_info=True)
        return None
    if not row or row.get("provider_type") != PROVIDER_TYPE or not guarded(row):
        return None
    title = " ".join(str((session or {}).get("title") or (session or {}).get("id") or "").split())[:80]
    return AGENT_MESSAGE_REFUSAL.format(title=title)


def unattended_run_refusal(provider_id: Optional[str], reason: str) -> Optional[str]:
    """The refusal for an unattended run (``reason`` names it) on this row.

    Lets an automation fail before it mints a session for a turn the guard
    would refuse. None for any other row; fails open to the turn-start guard.
    """
    if not provider_id:
        return None
    try:
        from app.db import model_providers
        row = model_providers.get_model_provider(str(provider_id))
    except Exception:  # noqa: BLE001 — the turn-start guard still applies
        logger.debug("[antigravity] could not read %s for a run", provider_id, exc_info=True)
        return None
    if not row or row.get("provider_type") != PROVIDER_TYPE or not guarded(row):
        return None
    return GUARD_REFUSAL.format(reason=reason)


def _retried_provenance(session_id: Optional[str]) -> tuple[Optional[str], bool, Optional[str]]:
    """``(source, person_entered, refusal)`` for a Retry: the turn it re-runs.

    A Retry re-runs the request the chat's newest started turn left
    unanswered, so that exact row decides, by its own admission
    (``turn_scheduler.turn_provenance``): its real source, never the
    accounting origin of a parked turn's automatic resume, and whether a
    person entered it. The Retry itself must be the owner's (its row's own
    key): one asked for through a preview link or by nobody known is refused
    whatever row it re-runs. A Retry sent with an API key is a request through the
    API; a Retry of a Retry walks back to the row it re-ran; a failover retry
    follows its parent. Unknown is a refusal.
    """
    unknown = (None, False, "a retry whose original turn could not be checked")
    if not session_id:
        return unknown
    try:
        from app.core.turn_scheduler import turn_provenance
        from app.db import turns

        live = turns.get_live_turn(session_id) or {}
        payload = live.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        if payload.get("api_user_id"):
            return None, False, _SOURCE_REASONS["api"]
        # Who asked for this Retry: only the owner (the key its own admission
        # wrote, ``session_modes.person_entry``) may re-run the owner's turn.
        if payload.get(turns.PERSON_ENTERED_KEY) is not True:
            if payload.get(turns.PREVIEW_ENTRY_KEY):
                return None, False, PREVIEW_REASON
            return None, False, RETRY_NOT_OWNER_REASON
        with turns.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM turns WHERE session_id=%s AND id<>%s AND started_at IS NOT NULL "
                "ORDER BY started_at DESC, insert_order DESC LIMIT 20",
                (session_id, str(live.get("id") or "")),
            )
            rows = cursor.fetchall()
        for row in turns._rows(rows):
            if row.get("source") == RETRY_TURN_SOURCE:
                if row["payload"].get("api_user_id"):
                    return None, False, _SOURCE_REASONS["api"]
                continue
            source, entered = turn_provenance(row)
            return (source, entered, None) if source else unknown
    except Exception:  # noqa: BLE001 — fail closed
        logger.debug("[antigravity] could not read the retried turn of %s", session_id, exc_info=True)
    return unknown


def unattended_reason(options: Any, *, turn_source: Any = _CONNECT_SOURCE,
                      person_entered: Any = _CONNECT_SOURCE) -> Optional[str]:
    """Why this run is not the owner typing in their own chat, or None.

    Refused: automation runs, scheduled and other unattended turns, Task /
    Swarm workers, subagent children, sessions another agent started,
    answers given for the owner, and internal calls with no app session.
    Reads the session row; never raises (a failed read counts as
    unattended). ``turn_source`` and ``person_entered`` describe the turn
    being started (its row's own admission); by default what the session
    connected with. Only ``person_entered is True`` counts.
    """
    extra = getattr(options, "extra", None) or {}
    if extra.get("one_shot") or extra.get("assistant_proactive") is not None:
        return "a background call"
    if turn_source is _CONNECT_SOURCE:
        turn_source = getattr(options, "aih_turn_source", None)
    if person_entered is _CONNECT_SOURCE:
        person_entered = getattr(options, "aih_turn_person_entered", None)
    session_id = extra.get("app_session_id")
    if turn_source == RETRY_TURN_SOURCE:
        # A Retry runs with the provenance of the exact turn it re-runs.
        turn_source, person_entered, why = _retried_provenance(session_id)
        if why:
            return why
    if not turn_source:
        # Fail closed: the stateless REST queries and any future caller that
        # names no source are not the owner typing in the app.
        return NO_SOURCE_REASON
    if turn_source not in OWNER_UI_TURN_SOURCES:
        if turn_source == "agent_message":
            return _agent_message_reason(session_id)
        return _SOURCE_REASONS.get(str(turn_source), "a scheduled or automatic turn")
    if person_entered is not True:
        return _NOT_ENTERED_REASONS.get(str(person_entered), NOT_ENTERED_REASON)
    if not session_id:
        return "a background call"
    try:
        from app.db import sessions
        row = sessions.get_session(session_id) or {}
    except Exception:  # noqa: BLE001
        logger.debug("[antigravity] guard could not read %s", session_id, exc_info=True)
        return "a run it could not check"
    if not row:
        return "a background call"
    if row.get("automation_id"):
        return "an automation run"
    if row.get("is_worker"):
        return "a Task or Swarm worker"
    if row.get("parent_session_id"):
        return "a subagent"
    if row.get("hidden"):
        return "a background session"
    try:
        from app.core.session_orchestration import _start_turn
        if _start_turn(session_id) is not None:
            return "a session another agent started"
    except Exception:  # noqa: BLE001
        return "a run it could not check"
    return None


def guard_refusal(row: Mapping[str, Any], options: Any, *,
                  turn_source: Any = _CONNECT_SOURCE,
                  person_entered: Any = _CONNECT_SOURCE) -> Optional[str]:
    """The refusal text for this run on this row, or None to proceed."""
    if not guarded(row):
        return None
    reason = unattended_reason(options, turn_source=turn_source, person_entered=person_entered)
    return GUARD_REFUSAL.format(reason=reason) if reason else None


MISSING_ROW = "This chat's Antigravity provider no longer exists."


def turn_row(provider_id: Optional[str], options: Any, turn_source: Optional[str],
             person_entered: bool = False) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Re-read the row as a turn starts: ``(refusal, row)``.

    Every turn decides from the stored row, never from the copy a connected
    chat took: a guard restored, a row disabled or deleted applies to the
    very next turn. One primary-key read (with the token, for the spawn
    binding below), plus the session row only while the guard applies.
    """
    from app.db import database as db

    try:
        # The reader connect uses (a disabled account follows its slot sibling;
        # Antigravity rows have no slot today, so it is the exact row).
        row = db.resolve_model_provider(provider_id, include_token=True) if provider_id else None
    except Exception:  # noqa: BLE001 — fail closed, like the guard itself
        logger.debug("[antigravity] turn check could not read %s", provider_id, exc_info=True)
        return "Could not check this chat's Antigravity provider. Try again.", None
    if not row or row.get("provider_type") != PROVIDER_TYPE:
        return MISSING_ROW, None
    if not row.get("enabled"):
        return (f"Model provider '{row.get('name') or provider_id}' is disabled. "
                "Re-enable it in Settings or pick a different model."), None
    return guard_refusal(row, options, turn_source=turn_source, person_entered=person_entered), row


def spawn_binding(row: Mapping[str, Any], *, binary: Optional[str] = None) -> str:
    """Digest of everything fixed into the agent process when it starts.

    The row (its private home), the key, the agent's extra environment and
    the installed binary go into
    its environment and argv at spawn; when any of them changes, the next
    turn starts a fresh process (resuming the agent's saved session). Pass the
    ``binary`` the spec was built with, so an update between the two cannot
    make the binding name a different executable than the spec runs.
    """
    import json

    material = json.dumps({
        "id": row.get("id"), "auth_mode": row.get("auth_mode"), "key": row_api_key(row) or "",
        "env": agent_extra_env(row), "binary": binary or find_binary() or "",
        # A sign-out or delete here: the next turn never reuses the process.
        "signin": signin_epoch(str(row.get("id") or "")),
    }, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Running agents: ended when the row's credential is taken away
# ---------------------------------------------------------------------------

STOPPED_SIGNED_OUT = ("Antigravity was signed out on this worker, so this turn was stopped. "
                      "Sign in again on the provider's page to continue.")
STOPPED_REMOVED = "This chat's Antigravity provider was removed, so this turn was stopped."
STOPPED_DISABLED = ("This chat's Antigravity provider was turned off, so this turn was stopped. "
                    "Turn it back on or pick a different model.")
STOPPED_KEY_CHANGED = ("This provider's Gemini API key was replaced, so this turn was stopped. "
                       "Send the message again to continue with the new key.")

# Row id -> live ACP engines and the spawn binding each runs with. Weak: an
# engine that is gone drops out by itself; nothing here keeps one alive.
_agents: Dict[str, "weakref.WeakKeyDictionary[Any, str]"] = {}


def register_agent(row_id: str, engine: Any, binding: str) -> None:
    """Record (or re-record after a respawn) an engine running for the row."""
    _agents.setdefault(str(row_id), weakref.WeakKeyDictionary())[engine] = binding


def agent_row_ids() -> List[str]:
    """Rows with an engine on this worker (cheap: no database read)."""
    return [row_id for row_id in list(_agents) if _engines(row_id)]


def _engines(row_id: str) -> List[Any]:
    return list((_agents.get(str(row_id)) or {}).keys())


async def stop_agents(row_id: str, reason: str, *, keep_binding: Optional[str] = None) -> int:
    """End the row's agent processes now (a running turn ends with ``reason``).

    ``keep_binding`` spares the engines already running with that binding
    (an edit that changed nothing the agent holds).
    """
    stopped = 0
    entries = _agents.get(str(row_id)) or {}
    for engine in list(entries.keys()):
        if keep_binding is not None and entries.get(engine) == keep_binding:
            continue
        try:
            await engine.stop_agent(reason)
            stopped += 1
        except Exception:  # noqa: BLE001 — one engine never blocks the others
            logger.debug("[antigravity] could not stop an agent of %s", row_id, exc_info=True)
    return stopped


async def reconcile_agents(row_ids: Any) -> None:
    """After a row changed (here or through sync): end every agent whose
    row is gone or off, or that holds a key or setting the row no longer has."""
    from app.db import database as db

    for row_id in {str(r) for r in row_ids or () if r}:
        if not _engines(row_id):
            continue
        try:
            row = await asyncio.to_thread(db.get_model_provider, row_id, include_token=True)
        except Exception:  # noqa: BLE001 — the next turn's own check still applies
            logger.debug("[antigravity] could not re-read %s", row_id, exc_info=True)
            continue
        if not row or row.get("provider_type") != PROVIDER_TYPE:
            await stop_agents(row_id, STOPPED_REMOVED)
        elif not row.get("enabled"):
            await stop_agents(row_id, STOPPED_DISABLED)
        else:
            await stop_agents(row_id, STOPPED_KEY_CHANGED, keep_binding=spawn_binding(row))


# ---------------------------------------------------------------------------
# Quota through the binary's own /usage
# ---------------------------------------------------------------------------

async def read_usage(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Run ``agy -p /usage --output-format json`` for the row; raw JSON or None."""
    import json
    try:
        spec = build_spec(row)
    except FileNotFoundError:
        return None
    home = home_for(str(row.get("id") or "default"))
    try:
        proc = await asyncio.create_subprocess_exec(
            spec.argv[0], "-p", "/usage", "--output-format", "json", cwd=home, env=spec.env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=os.name == "posix",
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_USAGE_SECONDS)
    except asyncio.TimeoutError:
        # Signed out, it prints a sign-in link and waits: never left waiting.
        return None
    finally:
        # Also when the read is cancelled (shutdown, the caller gone): the
        # process ends with the read, never left until the timeout.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    text = out.decode("utf-8", "replace").strip()
    start = text.find("{")
    try:
        payload = json.loads(text[start:]) if start >= 0 else None
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


_FRACTION_KEYS = ("remaining_fraction", "remainingFraction", "remaining")
_RESET_KEYS = ("reset_time", "resetTime", "resets_at", "resetsAt", "reset_at")
_NAME_KEYS = ("window", "name", "label", "model", "type", "id")


def normalize_usage(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """Windows from a ``/usage`` JSON reply, or None when it is not understood.

    The shape is unverified against a signed-in binary (community reports
    describe a remaining fraction and a reset time per window). Anything that
    does not look like that answers None, which the UI shows as unavailable.
    """
    if not isinstance(payload, Mapping) or payload.get("status") == "ERROR":
        return None
    candidates: List[Mapping[str, Any]] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(value, Mapping):
            if any(k in value for k in _FRACTION_KEYS):
                candidates.append(value)
                return
            for child in value.values():
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:50]:
                walk(child, depth + 1)

    walk({k: v for k, v in payload.items() if k != "usage"})
    windows = []
    for index, item in enumerate(candidates):
        fraction = next((item.get(k) for k in _FRACTION_KEYS if k in item), None)
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            continue
        if not 0 <= float(fraction) <= 1:
            continue
        name = next((str(item[k]) for k in _NAME_KEYS if isinstance(item.get(k), str) and item[k]), "")
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or str(index)
        reset = next((item.get(k) for k in _RESET_KEYS if item.get(k) not in (None, "")), None)
        windows.append({
            "type": f"antigravity_{slug}",
            "label": name or None,
            "utilization": round((1 - float(fraction)) * 100, 2),
            "resets_at": str(int(reset)) if isinstance(reset, (int, float)) and not isinstance(reset, bool)
            else (str(reset) if reset is not None else None),
        })
    return windows or None

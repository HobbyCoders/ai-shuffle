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
import tarfile
import tempfile
import time
import uuid
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

MANIFEST_URL = (
    "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app"
    "/manifests/linux_amd64.json"
)
_ALLOWED_DOWNLOAD_HOSTS = ("storage.googleapis.com",)
DASHBOARD_URL = "https://antigravity.google/docs/faq"
TERMS_URL = "https://antigravity.google/terms"

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
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# A line saying how the code went; the binary may keep running after it.
_LOGIN_RESULT_RE = re.compile(r"succe|signed in|logged in|authenticated|fail|invalid|error", re.I)
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


def find_binary() -> Optional[str]:
    """The executable to run: the override, the app's install, then PATH."""
    override = os.environ.get(BINARY_ENV, "").strip()
    if override:
        return override if os.path.isfile(override) else None
    try:
        recorded = _current_record().read_text(encoding="utf-8").strip()
    except OSError:
        recorded = ""
    if recorded and os.path.isfile(recorded) and os.access(recorded, os.X_OK):
        return recorded
    return shutil.which("agy")


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

@dataclass
class InstallJob:
    state: str = "idle"  # idle | running | done | failed
    version: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[float] = None


_install = InstallJob()
_install_task: Optional[asyncio.Task] = None


def install_state() -> Dict[str, Any]:
    return {"state": _install.state, "version": _install.version, "error": _install.error}


def start_install() -> Dict[str, Any]:
    """Start (or join) the install of the latest Linux x64 release."""
    global _install_task, _install
    if _install_task is not None and not _install_task.done():
        return install_state()
    import platform
    if os.name != "posix" or platform.machine().lower() not in ("x86_64", "amd64"):
        _install = InstallJob(state="failed", error="The app installs Antigravity on Linux x64 only. "
                              "Install Google's Antigravity CLI yourself so `agy` is on PATH.")
        return install_state()
    _install = InstallJob(state="running", started_at=time.time())
    _install_task = asyncio.get_running_loop().create_task(_run_install())
    return install_state()


async def _run_install() -> None:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            manifest = (await client.get(MANIFEST_URL)).raise_for_status().json()
            version, url, digest = (str(manifest.get(k) or "") for k in ("version", "url", "sha512"))
            from urllib.parse import urlsplit
            if not (version and url and digest) or urlsplit(url).hostname not in _ALLOWED_DOWNLOAD_HOSTS:
                raise RuntimeError("Google's update manifest did not name a download.")
            _install.version = version
            target = _root() / "cli" / re.sub(r"[^\w.-]", "_", version)
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
                archive = Path(tmp) / "cli.tar.gz"
                sha = hashlib.sha512()
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with archive.open("wb") as fh:
                        async for chunk in response.aiter_bytes(1 << 20):
                            sha.update(chunk)
                            fh.write(chunk)
                if sha.hexdigest().lower() != digest.lower():
                    raise RuntimeError("The download did not match Google's checksum.")
                await asyncio.to_thread(_extract_binary, archive, target / "agy")
        _current_record().write_text(str(target / "agy"), encoding="utf-8")
        _install.state = "done"
    except Exception as exc:  # noqa: BLE001 — reported to the owner as text
        logger.warning("[antigravity] install failed", exc_info=True)
        _install.state, _install.error = "failed", f"Install failed: {exc}"


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


async def probe(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Ask the binary itself whether this row can open sessions.

    ``initialize`` (+ ``authenticate`` for key rows), then ``session/list``:
    -32000 means signed out. The sign-in itself is never looked at.
    """
    try:
        spec = build_spec(row)
    except FileNotFoundError as exc:
        return {"state": "not_installed", "message": str(exc)}
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

    def public(self) -> Dict[str, Any]:
        return {"login_id": self.id, "provider_id": self.row_id, "state": self.state,
                "url": self.url, "message": self.message}


_logins: Dict[str, LoginSession] = {}


def _prune_logins() -> None:
    cutoff = time.time() - _LOGIN_IDLE_SECONDS
    for key, login in list(_logins.items()):
        if login.created_at < cutoff:
            _kill(login)
            _logins.pop(key, None)


def _kill(login: LoginSession) -> None:
    proc = login.proc
    if proc is not None and proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def start_login(row: Mapping[str, Any]) -> Dict[str, Any]:
    """Run ``agy acp --login`` in the row's home and return its sign-in link."""
    _prune_logins()
    for login in list(_logins.values()):
        if login.row_id == row.get("id"):
            _kill(login)
            _logins.pop(login.id, None)
    if row.get("auth_mode") not in STORED_SIGN_IN:
        return {"state": "failed", "message": "This provider signs in with an API key."}
    if row.get("auth_mode") == agy_spec.OAUTH_BUSINESS:
        # Spike, agy 1.5.6: business sign-in needs a browser on the worker
        # ("No browser is available here"); there is no terminal flow for it.
        return {"state": "failed", "message": (
            "Antigravity offers business sign-in only through a browser on the worker. "
            "Use a Gemini API key here, or sign in from a desktop worker.")}
    binary = find_binary()
    if not binary:
        return {"state": "not_installed", "message": "Antigravity is not installed on this worker."}
    home = home_for(str(row["id"]))
    login = LoginSession(id=uuid.uuid4().hex, row_id=str(row["id"]))
    login.proc = await asyncio.create_subprocess_exec(
        binary, "acp", "--login", cwd=home, env=agy_spec.agent_env(home),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=os.name == "posix",
    )
    _logins[login.id] = login
    try:
        await asyncio.wait_for(_read_until_url(login), timeout=30)
    except asyncio.TimeoutError:
        pass
    if not login.url:
        _kill(login)
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
    lines = [line for line in login.output if line and not _URL_RE.search(line)][-3:]
    return (" " + " ".join(lines))[:400] if lines else ""


def get_login(login_id: str) -> Optional[LoginSession]:
    return _logins.get(login_id)


async def complete_login(login: LoginSession, code: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    """Hand the owner's authorization code to the binary and check the result."""
    code = (code or "").strip()
    if not code or len(code) > 4096 or "\n" in code:
        return {**login.public(), "state": "failed", "message": "Paste the code or the full URL."}
    proc = login.proc
    if proc is None or proc.returncode is not None or proc.stdin is None:
        login.state, login.message = "failed", "The sign-in expired. Start it again."
        return login.public()
    login.state = "completing"
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
    result = await probe(row)
    if result.get("state") == "signed_in":
        login.state, login.message = "signed_in", None
    else:
        login.state = "failed"
        login.message = "Antigravity did not accept the sign-in." + _tail(login)
    _logins.pop(login.id, None)
    return login.public()


def cancel_login(login_id: str) -> bool:
    login = _logins.pop(login_id, None)
    if login is None:
        return False
    _kill(login)
    return True


async def sign_out(row: Mapping[str, Any]) -> None:
    """Forget a row's sign-in by deleting its whole private home.

    The directory is the binary's; nothing in it is read. Removing it is the
    only way the app ends a stored sign-in without touching its contents.
    """
    path = _root() / "homes" / re.sub(r"[^A-Za-z0-9_.-]", "_", str(row.get("id") or ""))
    if row.get("id") and path.is_dir():
        await asyncio.to_thread(shutil.rmtree, path, True)


# ---------------------------------------------------------------------------
# Guard: personal sign-ins are interactive-only by default
# ---------------------------------------------------------------------------

def unattended_reason(options: Any) -> Optional[str]:
    """Why this run is not the owner typing in their own chat, or None.

    Refused: automation runs, scheduled and other unattended turns, Task /
    Swarm workers, subagent children, sessions another agent started, and
    internal calls with no app session. Reads the session row; never raises
    (a failed read counts as unattended).
    """
    extra = getattr(options, "extra", None) or {}
    if extra.get("one_shot") or extra.get("assistant_proactive") is not None:
        return "a background call"
    turn_source = getattr(options, "aih_turn_source", None)
    from app.core.turn_sources import PERSON_PRESENT_TURN_SOURCES
    if turn_source and turn_source not in PERSON_PRESENT_TURN_SOURCES:
        return "a scheduled or automatic turn"
    session_id = extra.get("app_session_id")
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


def guard_refusal(row: Mapping[str, Any], options: Any) -> Optional[str]:
    """The refusal text for this run on this row, or None to proceed."""
    if not guarded(row):
        return None
    reason = unattended_reason(options)
    return GUARD_REFUSAL.format(reason=reason) if reason else None


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
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
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

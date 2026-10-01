"""What the stock Claude Code CLI says about its own sign-in.

AI Shuffle never reads the CLI's credential files. To learn whether the CLI is
signed in, and as whom, it asks the CLI: ``claude auth status``. That command
prints JSON by default (``--json`` is accepted and is the default) and exits 0
when signed in, 1 when not. The fields read here are ``loggedIn``,
``authMethod``, ``apiProvider``, ``email``, ``orgName`` and
``subscriptionType``; every one is optional, unknown fields are ignored, and a
reply that is not JSON falls back to the plain-text form and then to the exit
code alone. A missing binary, a CLI without the ``auth`` command, a timeout or
garbage output all produce a status with ``error`` set instead of an exception.

The result is cached briefly: a positive answer for a minute, a negative one
for a few seconds so a login done in another terminal shows up quickly.
``invalidate()`` drops the cache after a login or logout the app ran itself.

When a probe finds that the sign-in or the plan changed since the previous
readable probe, ``get_status`` publishes a ``settings`` resource change. Every
open client re-reads ``/api/models`` on that event, and the model picker lists
Claude models only while the registry reports a Claude sign-in, so a sign-in
done on one device (or in a terminal) shows up in every composer without a
reload. The first probe of the process publishes nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

POSITIVE_TTL_SECONDS = 60.0
NEGATIVE_TTL_SECONDS = 5.0
_COMMAND_TIMEOUT_SECONDS = 15.0

# Environment variables that would make the CLI report a key instead of its own
# login. The API key is shown separately in Settings and is injected only into
# agent runs, so the probe asks about the CLI's sign-in alone.
_STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

# authMethod values that mean "the user signed in to the CLI". The CLI also
# reports ``api_key`` (a key in the environment) and ``none``.
_SIGN_IN_METHODS = {"claude.ai", "oauth_token", "api_key_helper", "third_party"}

_METHOD_LABELS = {
    "claude.ai": "Claude account",
    "oauth_token": "Claude account token",
    "api_key_helper": "API key helper",
    "third_party": "Third-party provider",
    "api_key": "API key",
    "none": "Not signed in",
}

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z.]+)?")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@dataclass
class ClaudeCliStatus:
    """One probe result. Every field is what the CLI printed, or None."""

    installed: bool = False
    signed_in: Optional[bool] = None
    auth_method: Optional[str] = None
    auth_method_label: Optional[str] = None
    email: Optional[str] = None
    org_name: Optional[str] = None
    plan: Optional[str] = None
    api_provider: Optional[str] = None
    cli_version: Optional[str] = None
    binary_path: Optional[str] = None
    error: Optional[str] = None
    checked_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def _probe_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
    env.setdefault("NO_COLOR", "1")
    return env


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
        stdin=subprocess.DEVNULL,
        env=_probe_env(),
    )


def _default_resolver() -> str:
    """The same PATH-only resolution the delegated loop uses."""
    from app.core.provider.harness_adapter import _resolve_claude_cmd

    return _resolve_claude_cmd()[0]


def _string(value) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_json(text: str) -> Optional[dict]:
    """The first JSON object in *text*, tolerating banners around it."""
    start = text.find("{")
    while start != -1:
        end = text.rfind("}")
        while end > start:
            try:
                value = json.loads(text[start:end + 1])
            except ValueError:
                end = text.rfind("}", start, end)
                continue
            return value if isinstance(value, dict) else None
        start = text.find("{", start + 1)
    return None


def parse_json_status(data: dict, status: ClaudeCliStatus) -> None:
    """Fill *status* from the ``claude auth status`` JSON object."""
    logged_in = data.get("loggedIn", data.get("logged_in"))
    method = _string(data.get("authMethod", data.get("auth_method")))
    status.auth_method = method
    status.api_provider = _string(data.get("apiProvider", data.get("api_provider")))
    status.email = _string(data.get("email", data.get("emailAddress")))
    status.org_name = _string(data.get("orgName", data.get("organizationName")))
    status.plan = _string(data.get("subscriptionType", data.get("plan")))
    if isinstance(logged_in, bool):
        status.signed_in = logged_in
    elif method is not None:
        status.signed_in = method != "none"
    # A key in the environment is not a CLI sign-in; the probe strips the
    # usual variable, but a key from elsewhere must not read as one either.
    if method == "api_key":
        status.signed_in = False


def parse_text_status(text: str, status: ClaudeCliStatus) -> None:
    """Best effort for the human-readable form (``claude auth status --text``)."""
    lowered = text.lower()
    if "not logged in" in lowered or "not signed in" in lowered:
        status.signed_in = False
    email = _EMAIL_RE.search(text)
    if email:
        status.email = email.group(0)
    for line in text.splitlines():
        label, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = label.strip().lower(), value.strip()
        if key in ("login method", "auth method") and value:
            status.auth_method_label = value
        elif key in ("organization", "org") and value:
            status.org_name = value
        elif key in ("plan", "subscription") and value:
            status.plan = value


def _supports_auth_status(binary: str) -> bool:
    try:
        result = _run([binary, "auth", "--help"])
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "status" in (result.stdout + result.stderr).lower()


_auth_support: dict[tuple[str, Optional[str]], bool] = {}


def probe(resolver: Optional[Callable[[], str]] = None) -> ClaudeCliStatus:
    """Run the CLI's own status command once, uncached. Never raises."""
    status = ClaudeCliStatus()
    try:
        binary = (resolver or _default_resolver)()
    except Exception as exc:  # ClaudeCliMissingError and anything unexpected
        status.error = str(exc) or "The Claude Code CLI is not installed."
        return status
    status.installed = True
    status.binary_path = binary

    try:
        version = _run([binary, "--version"])
        match = _VERSION_RE.search(version.stdout or "")
        status.cli_version = match.group(0) if match else None
    except (OSError, subprocess.SubprocessError) as exc:
        status.installed = False
        status.error = f"The Claude Code CLI at {binary} could not be started: {type(exc).__name__}"
        return status

    key = (binary, status.cli_version)
    if key not in _auth_support:
        _auth_support[key] = _supports_auth_status(binary)
    if not _auth_support[key]:
        status.error = ("This Claude Code CLI has no `claude auth status` command. "
                        "Update the CLI (`claude update`) and check again.")
        return status

    try:
        result = _run([binary, "auth", "status", "--json"])
        if result.returncode not in (0, 1) or "unknown option" in (result.stderr or "").lower():
            result = _run([binary, "auth", "status"])
    except subprocess.TimeoutExpired:
        status.error = "`claude auth status` did not answer in time."
        return status
    except (OSError, subprocess.SubprocessError) as exc:
        status.error = f"`claude auth status` could not run: {type(exc).__name__}"
        return status

    output = result.stdout or ""
    data = _extract_json(output)
    if data is not None:
        parse_json_status(data, status)
    else:
        parse_text_status(output + "\n" + (result.stderr or ""), status)
    if status.signed_in is None and result.returncode in (0, 1):
        status.signed_in = result.returncode == 0
    if status.signed_in is None:
        status.error = "`claude auth status` gave an answer AI Shuffle could not read."
    if status.auth_method and not status.auth_method_label:
        status.auth_method_label = _METHOD_LABELS.get(status.auth_method, status.auth_method)
    if not status.signed_in:
        # Nothing about an account is shown for a CLI that is not signed in.
        status.email = status.org_name = status.plan = None
    return status


# ─── Cache ──────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_cached: Optional[ClaudeCliStatus] = None
_cached_at = 0.0
# ``(signed_in, plan)`` from the last probe that could read the sign-in. Kept
# apart from the cache because ``invalidate()`` drops the cache on the very
# login this is meant to notice.
_last_account: Optional[tuple] = None


def _announce_account_change() -> None:
    """Tell open clients to re-read settings-derived state (the model registry)."""
    from app.core.resource_sync import mutation

    @mutation("settings", database=False)
    def claude_account_changed() -> bool:
        return True

    try:
        claude_account_changed()
    except Exception:  # noqa: BLE001 — a lost refresh hint must not fail the probe
        logger.warning("Could not announce the Claude Code sign-in change", exc_info=True)


def get_status(force: bool = False, resolver: Optional[Callable[[], str]] = None) -> ClaudeCliStatus:
    """The cached probe result, re-running the probe when it is stale.

    Blocking (spawns the CLI on a miss); async callers use ``get_status_async``.
    Concurrent callers share one probe.
    """
    global _cached, _cached_at, _last_account
    with _lock:
        now = time.monotonic()
        if not force and _cached is not None:
            ttl = POSITIVE_TTL_SECONDS if _cached.signed_in else NEGATIVE_TTL_SECONDS
            if now - _cached_at < ttl:
                return _cached
        result = probe(resolver)
        _cached, _cached_at = result, time.monotonic()
        if result.error:
            logger.info("Claude Code CLI status: %s", result.error)
        changed = False
        # A probe that could not read the sign-in says nothing about the
        # account, so it neither counts as a change nor resets the baseline.
        if result.signed_in is not None:
            account = (bool(result.signed_in), result.plan)
            changed = _last_account is not None and account != _last_account
            _last_account = account
    if changed:
        logger.info(
            "Claude Code CLI sign-in changed (signed in: %s, plan: %s)",
            result.signed_in, result.plan,
        )
        _announce_account_change()
    return result


async def get_status_async(force: bool = False) -> ClaudeCliStatus:
    import asyncio

    return await asyncio.to_thread(get_status, force)


def peek() -> Optional[ClaudeCliStatus]:
    """The last result without probing (None before the first probe)."""
    return _cached


def is_signed_in_cached() -> bool:
    """Whether the CLI reports a login, from the cache or a fresh probe."""
    return bool(get_status().signed_in)


def invalidate() -> None:
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0

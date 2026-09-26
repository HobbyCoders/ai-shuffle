"""One answer to "may a new agent run start on this worker?".

``agent_credentials_ready()`` combines the two things a Claude agent run needs:

1. The license allows new agent runs (``license.license_allows_agent_runs``;
   unlicensed blocks only new runs, never data).
2. The active Claude Code auth mode has its credential
   (``app.core.claude_auth_mode``): in ``subscription`` mode the stock CLI
   reports a sign-in (``claude auth status``, via ``claude_cli_status``; the
   CLI's credential files are never read); in ``api_key`` mode an Anthropic API
   key is stored on this worker.

There is no fallback: a worker with neither credential does not borrow any
other, it refuses the run with a setup message. The same function drives three
places: startup (a logged setup state), ``GET /auth/status`` and turn start in
``query_engine``, so a turn with no credential is refused whatever its source:
chat, connector, automation, wake-up, background worker or a turn handed over
from another device.

Turns that run on another provider (an OpenAI, xAI, OpenRouter or custom
gateway row) do not need a Claude credential; for those only the license part
applies (see :func:`turn_refusal`). Their own missing key is reported by the
provider path itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SETUP_CODE = "agent_setup_required"

NO_CLI_MESSAGE = (
    "Agents cannot start: the Claude Code CLI is not installed on this worker. "
    "See Settings > Providers > Claude Code CLI for how to install it, or choose "
    "API key mode there and add your Anthropic API key."
)
NOT_SIGNED_IN_MESSAGE = (
    "Agents cannot start: Claude Code is set to subscription mode and the Claude "
    "Code CLI is not signed in. Sign in with your Claude account in Settings > "
    "Providers > Claude Code CLI (it runs `claude auth login`), or add an "
    "Anthropic API key and switch Claude Code mode to API key. AI Shuffle never "
    "falls back to another credential."
)
NO_KEY_MESSAGE = (
    "Agents cannot start: Claude Code is set to API key mode and no Anthropic API "
    "key is stored on this worker. Add your key in Settings > Providers, or switch "
    "Claude Code mode to subscription and sign in to the Claude Code CLI. AI "
    "Shuffle never falls back to another credential."
)
UNKNOWN_CLI_MESSAGE = (
    "Agents cannot start: AI Shuffle could not read the Claude Code CLI's sign-in "
    "status ({detail}). Check Settings > Providers > Claude Code CLI."
)


class AgentSetupRequired(ValueError):
    """A run was refused because this worker is not set up to start agents."""


# What startup found, for logs and the status endpoint: "ready" or
# "setup_required". None before startup ran.
startup_state: Optional[str] = None
startup_reason: Optional[str] = None


def claude_credential_ready(cli=None) -> Tuple[bool, Optional[str]]:
    """Whether the active Claude Code mode has its credential, and why not.

    ``cli`` is a ``ClaudeCliStatus`` (or its dict); when omitted the cached
    probe is used, running ``claude auth status`` on a miss (blocking).
    """
    from app.core import claude_auth_mode

    mode = claude_auth_mode.current_mode()
    if mode == claude_auth_mode.MODE_API_KEY:
        if claude_auth_mode.has_stored_api_key():
            return True, None
        return False, NO_KEY_MESSAGE

    if cli is None:
        from app.core import claude_cli_status

        cli = claude_cli_status.get_status()
    if not isinstance(cli, dict):
        cli = cli.to_dict()
    if cli.get("signed_in"):
        return True, None
    if not cli.get("installed"):
        return False, NO_CLI_MESSAGE
    if cli.get("signed_in") is None and cli.get("error"):
        return False, UNKNOWN_CLI_MESSAGE.format(detail=cli["error"])
    return False, NOT_SIGNED_IN_MESSAGE


def agent_credentials_ready(cli=None) -> Tuple[bool, Optional[str]]:
    """``(ready, reason)`` for a new Claude agent run on this worker.

    Blocking (may run the CLI probe on a cache miss); async callers use
    :func:`agent_credentials_ready_async`.
    """
    from app.core.license import license_allows_agent_runs

    allowed, reason = license_allows_agent_runs()
    if not allowed:
        return False, reason
    try:
        return claude_credential_ready(cli)
    except Exception:  # noqa: BLE001 — a broken check must not look like "ready"
        logger.exception("Credential check failed")
        return False, "Agents cannot start: AI Shuffle could not check the Claude Code credential. See the server log."


async def agent_credentials_ready_async(cli=None) -> Tuple[bool, Optional[str]]:
    return await asyncio.to_thread(agent_credentials_ready, cli)


def turn_refusal(leg: Optional[str]) -> Optional[str]:
    """The setup message that refuses a turn, or None to let it start.

    ``leg`` is the harness leg the turn will run on (``"delegated"`` is the
    Claude Code CLI; anything else is a provider row with its own credential).
    """
    if leg in (None, "delegated"):
        ready, reason = agent_credentials_ready()
        return None if ready else reason
    from app.core.license import license_allows_agent_runs

    allowed, reason = license_allows_agent_runs()
    return None if allowed else reason


async def turn_refusal_async(leg: Optional[str]) -> Optional[str]:
    return await asyncio.to_thread(turn_refusal, leg)


def leg_for_options(options) -> str:
    """The leg a built options object runs on; unknown reads as the CLI."""
    try:
        from app.core.provider.harness_adapter import provider_kind_for_sdk_options

        return provider_kind_for_sdk_options(options) or "delegated"
    except Exception:  # noqa: BLE001
        logger.debug("Could not decide the leg for the credential gate", exc_info=True)
        return "delegated"


def refusal_for_options(options) -> Optional[str]:
    """:func:`turn_refusal` for a built options object (may read the DB)."""
    return turn_refusal(leg_for_options(options))


async def refusal_for_options_async(options) -> Optional[str]:
    return await asyncio.to_thread(refusal_for_options, options)


def refusal_event(reason: str) -> dict:
    """The stream's error frame for a refused turn."""
    return {"type": "error", "message": reason, "code": SETUP_CODE}


def record_startup(cli=None) -> Tuple[bool, Optional[str]]:
    """Startup's check: log the blocking setup state (no fallback is tried)."""
    global startup_state, startup_reason
    ready, reason = agent_credentials_ready(cli)
    startup_state = "ready" if ready else "setup_required"
    startup_reason = reason
    if ready:
        logger.info("Agent runs: ready")
    else:
        logger.warning("Agent runs are blocked until setup is finished: %s", reason)
    return ready, reason


def status_payload(cli=None) -> dict:
    """The gate as ``/auth/status`` and Settings report it."""
    from app.core import claude_auth_mode
    from app.core.license import license_allows_agent_runs

    license_ok, license_reason = license_allows_agent_runs()
    try:
        claude_ok, claude_reason = claude_credential_ready(cli)
    except Exception:  # noqa: BLE001
        logger.warning("Credential check failed for the status payload", exc_info=True)
        claude_ok, claude_reason = False, None
    return {
        "ready": bool(license_ok and claude_ok),
        "reason": license_reason if not license_ok else claude_reason,
        "mode": claude_auth_mode.current_mode(),
        "claude_credential_ready": claude_ok,
        "license_allows_runs": license_ok,
        "startup_state": startup_state,
    }

"""
Built-in (managed) MCP servers / integrations.

These are browser-automation (and similar) capabilities that AI Shuffle ships
and manages itself, rather than ones the user adds via .mcp.json / ~/.claude.json.
They are surfaced through the normal McpService paths so they appear in the admin
MCP server list. The ``browser`` built-in is ALWAYS ON — there is no setting;
stale ``disabledMcpServers`` entries for it are stripped on read.

Currently this provides a single built-in:

- ``browser`` — AI Shuffle's own browser-automation tool suite. This IS a normal
  spawned STDIO MCP server: the SDK launches ``node /opt/browser-mcp/dist/index.js``
  once per session and tears it down on exit (session isolation + cleanup for free).
  It exposes tools named ``mcp__browser__<tool>`` (``browser_navigate``,
  ``browser_snapshot``, ``browser_act``, ``browser_screenshot``; more land in later
  PRs).

Architecture (the decision record):
  Each spawned server is a thin CDP client built on ``playwright-core``. It calls
  ``connectOverCDP('http://127.0.0.1:9221')`` to attach to the *existing supervised
  Chromium* (port from ``CHROME_CDP_PORT``, default 9221) and drives a single
  ``Page`` in the persistent default context. That persistent context preserves the
  logged-in profile (``browser-profile`` volume); the chrome-supervisor keeps
  ownership of the process (SingletonLock) so the MCP server never launches or owns a
  browser itself. No Claude account, no browser extension, no native-messaging host,
  and no ``--chrome`` CLI flag are involved — this replaces the previous "Claude in
  Chrome" integration entirely.

  Because it is an ordinary STDIO server, ``get_browser_server_config()`` returns a
  spawnable ``McpServerConfig`` (``command="node"``, ``args=[BROWSER_MCP_ENTRY]``);
  the mcp_servers builder adds it like any other server, and its per-session env
  (``ANTHROPIC_*`` for the future ``browser_find`` sub-call plus ``CHROME_CDP_PORT``)
  is injected by mcp_config for this server only. The old ``cli_flag`` path is gone.

  The entry point is installed at ``/opt/browser-mcp/dist/index.js`` in the Docker
  image (PR A3); ``BROWSER_MCP_ENTRY`` overrides it for local/dev checkouts.

SECURITY NOTE (SSRF / local-network reach): the spawned server drives a real browser
that can currently reach loopback (127.0.0.1/localhost), RFC1918 private hosts on the
same network, and cloud metadata endpoints (e.g. 169.254.169.254, which on some
clouds exposes credentials). Egress filtering is NOT implemented yet — the SSRF guard
(Playwright request interception blocking metadata + loopback, RFC1918 configurable)
lands in PR A6. Until then, operators who run untrusted prompts should isolate the
host at the network layer.
"""

import logging
import os
from pathlib import Path
from typing import List

from app.core.mcp_service import McpServerConfig, McpServerType

logger = logging.getLogger(__name__)


# Name the browser integration is registered under. Tools are exposed to the
# agent as ``mcp__browser__<tool>`` (e.g. ``mcp__browser__browser_navigate``);
# the allow rule uses the server-level wildcard ``mcp__browser`` which permits
# every tool from it.
BROWSER_SERVER_NAME = "browser"

# Default path to the spawned STDIO server's entry point inside the Docker image
# (installed by PR A3). Overridable via the ``BROWSER_MCP_ENTRY`` env var for
# local/dev checkouts where the built package lives elsewhere.
DEFAULT_BROWSER_MCP_ENTRY = "/opt/browser-mcp/dist/index.js"


def _browser_mcp_entry() -> str:
    """Resolve the browser MCP entry point.

    Resolution order:
      1. ``BROWSER_MCP_ENTRY`` env override, if set.
      2. The packaged Docker path ``/opt/browser-mcp/dist/index.js`` — but only
         when it actually exists (i.e. we are in the container image).
      3. Repo-relative fallback ``<repo_root>/browser-mcp/dist/index.js`` for
         native/local checkouts, where ``repo_root`` is derived from this file's
         location (``<repo_root>/app/core/builtin_mcp_servers.py``). Returned
         regardless of existence — a missing build is diagnosed elsewhere
         (startup prereq log + the browser status ``detail`` field).
    """
    env_override = os.environ.get("BROWSER_MCP_ENTRY")
    if env_override:
        return env_override

    if os.path.exists(DEFAULT_BROWSER_MCP_ENTRY):
        return DEFAULT_BROWSER_MCP_ENTRY

    # app/core/builtin_mcp_servers.py -> repo root is two parents up from app/.
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root / "browser-mcp" / "dist" / "index.js")


def get_browser_server_config() -> McpServerConfig:
    """
    Build the McpServerConfig for the ``browser`` built-in.

    Returns a spawnable STDIO server (``command="node"``,
    ``args=[<entry>]``) — an ordinary MCP server the SDK launches per session,
    NOT a CLI-flag integration. It is surfaced in the admin MCP list and is
    always enabled (stale disable entries are ignored).
    """
    return McpServerConfig(
        name=BROWSER_SERVER_NAME,
        type=McpServerType.STDIO,
        command="node",
        args=[_browser_mcp_entry()],
        scope="builtin",
        enabled=True,
        builtin=True,
        description=(
            "Browser automation — drive the supervised Chromium over CDP with a "
            "provider-agnostic Playwright tool suite. Exposes mcp__browser__* tools "
            "(navigate, snapshot, act, screenshot). Uses the persistent logged-in "
            "profile; no Claude account or browser extension required."
        ),
    )


def get_builtin_servers() -> List[McpServerConfig]:
    """
    Return all built-in/managed MCP server configs.

    Always returned enabled: the browser suite has no per-server setting, and
    stale ``disabledMcpServers`` entries are stripped by the read path
    (``_normalize_disabled_servers``). The one switch is START_CHROME=0, which
    means no Chromium exists to drive, so the suite is not offered at all.
    """
    from app.core.config import settings

    if not settings.start_chrome:
        return []
    return [get_browser_server_config()]

"""
Built-in (managed) MCP servers.

These are MCP servers that AI Shuffle ships and manages itself, rather than
ones the user adds via .mcp.json / ~/.claude.json. They are surfaced through
the normal McpService paths so they appear in the admin MCP server list and
respect the same enable/disable mechanism (settings.json -> disabledMcpServers).

Currently this provides a single built-in server:

- ``playwright`` — a headless, isolated browser-automation MCP server
  (Microsoft Playwright MCP, https://github.com/microsoft/playwright-mcp).
  It lets the agent navigate, read, and interact with web pages entirely
  server-side ("Claude in Chrome"-style). The end user never sees a browser
  window — it runs ``--headless --isolated``.

Why Playwright MCP (vs chrome-devtools-mcp):
  Playwright MCP self-manages its Chromium binary (installed at build time via
  ``playwright install``), supports a first-class ``--headless --isolated``
  mode, and is the most reliable headless option on Debian-slim. chrome-devtools-mcp
  requires an already-running Chrome and is geared toward attaching to a visible
  browser, which is a poorer fit for this headless server-side use case.
"""

import logging
from pathlib import Path
from typing import List

from app.core.config import settings
from app.core.mcp_service import McpServerConfig, McpServerType

logger = logging.getLogger(__name__)


# Name the browser server is registered under. Individual tools are exposed to
# the agent as ``mcp__playwright__<tool>`` (e.g. ``mcp__playwright__navigate``);
# the allow rule uses the server-level wildcard ``mcp__playwright`` which
# permits every tool from this server.
BROWSER_SERVER_NAME = "playwright"

# Pinned @playwright/mcp version. MUST stay in sync with the Dockerfile, which
# installs this exact version globally AND bakes the matching Chromium build
# (see Dockerfile: @playwright/mcp@0.0.76 -> playwright-core 1.61.0-alpha
# -> Chromium build v1226 / Chrome-for-Testing 149.x). Pinning avoids the
# @latest server resolving to a Playwright build whose Chromium revision is not
# present under PLAYWRIGHT_BROWSERS_PATH (which would fail at runtime offline).
PLAYWRIGHT_MCP_VERSION = "0.0.76"

# SECURITY NOTE (SSRF / local-network reach): this is a real headless browser
# running inside the server with no egress filtering. The agent can therefore
# drive it to reach loopback (127.0.0.1/localhost), RFC1918 private hosts on the
# same network, and cloud-provider metadata endpoints (e.g. 169.254.169.254,
# which on some clouds exposes credentials). Operators who run untrusted prompts
# should isolate this container at the network layer (egress firewall / netns /
# blocked metadata route). We do NOT implement egress filtering here.

# Where Playwright's bundled Chromium lives. Set at Docker build time
# (see Dockerfile) and threaded into the agent subprocess env. When unset
# (e.g. local mode where the user ran ``npx playwright install`` into the
# default cache), Playwright falls back to its own default location.
PLAYWRIGHT_BROWSERS_PATH = "/ms-playwright"


def _browser_output_dir() -> Path:
    """
    Writable output/temp directory for the browser MCP server.

    Playwright MCP writes traces, downloads, and screenshots here. It must be
    a writable path (NOT a read-only mount), so we place it under the app's
    data directory rather than the project/workspace.
    """
    out = settings.effective_data_dir / "playwright-mcp"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # pragma: no cover - best effort
        logger.warning(f"Could not create Playwright MCP output dir {out}: {e}")
    return out


def get_browser_server_config() -> McpServerConfig:
    """
    Build the McpServerConfig for the headless browser-automation server.

    Returns a STDIO server invoked via ``npx`` that runs Playwright MCP in
    headless + isolated mode. ``isolated`` uses an ephemeral in-memory browser
    profile (no persisted cookies/sessions between runs).
    """
    # We invoke the PINNED version via npx WITHOUT ``-y``. In Docker the package
    # is pre-installed globally (npm install -g @playwright/mcp@<pinned>), so
    # ``npx @playwright/mcp@<pinned>`` resolves to that cached global copy with
    # no network fetch — it runs fully offline/air-gapped. (We intentionally drop
    # the old ``-y`` flag, which only suppresses npx's install prompt and is
    # unnecessary once the package is already present.)
    args: List[str] = [
        f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}",
        "--headless",
        "--isolated",
        "--output-dir",
        str(_browser_output_dir()),
    ]

    # Point Playwright at the build-time browser cache when present, so the
    # npx-launched server finds Chromium without re-downloading at runtime.
    env = {}
    if Path(PLAYWRIGHT_BROWSERS_PATH).exists():
        env["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_PATH

    return McpServerConfig(
        name=BROWSER_SERVER_NAME,
        type=McpServerType.STDIO,
        command="npx",
        args=args,
        env=env or None,
        scope="builtin",
        enabled=True,
        builtin=True,
        description=(
            "Headless browser automation (Playwright MCP). Lets the agent "
            "navigate, read, and interact with web pages server-side. Runs "
            "headless and isolated; no visible browser window."
        ),
    )


def get_builtin_servers() -> List[McpServerConfig]:
    """
    Return all built-in/managed MCP server configs.

    The ``enabled`` flag reflects the global disable list from settings.json
    (``disabledMcpServers``), so admins can toggle these the same way as other
    servers.
    """
    return [get_browser_server_config()]

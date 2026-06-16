"""
Built-in (managed) MCP servers / integrations.

These are browser-automation (and similar) capabilities that AI Shuffle ships
and manages itself, rather than ones the user adds via .mcp.json / ~/.claude.json.
They are surfaced through the normal McpService paths so they appear in the admin
MCP server list and respect the same enable/disable mechanism
(settings.json -> disabledMcpServers).

Currently this provides a single built-in:

- ``claude-in-chrome`` — Anthropic's official "Claude in Chrome" integration.
  This is NOT a separately spawned MCP server; it is a Claude Code CLI feature
  enabled by passing ``--chrome`` to the bundled CLI. When enabled, the CLI
  starts its own internal browser MCP server and exposes the browser as tools
  named ``mcp__claude-in-chrome__<tool>`` (navigate, computer, get_page_text,
  read_console_messages, …). The agent drives a REAL Chrome/Chromium running
  the Claude in Chrome extension (id ``fcoeoabgfenejglbffodgkkbkcdhcgfn``).

Why Claude in Chrome (vs Playwright MCP, which this replaces):
  Playwright MCP drives an ephemeral, isolated, throwaway Chromium with no
  account and no extension — fine for scraping but unusable for tasks that need
  the user's real, signed-in browser. Claude in Chrome connects to a persistent
  browser the user controls: real cookies/sessions, the Claude in Chrome
  extension, and Anthropic's first-party browser tools.

How it is wired here (the key difference from a normal MCP server):
  ``get_browser_server_config()`` returns an McpServerConfig with ``cli_flag``
  set (and no command/url). The mcp_servers builder SKIPS such entries, and
  ``build_options_from_profile`` instead:
    1. adds ``mcp__claude-in-chrome`` to allowed_tools, and
    2. sets ``extra_args["chrome"] = None`` (→ the CLI flag ``--chrome``)
  whenever this built-in is enabled (i.e. not in disabledMcpServers).

Browser-side requirements (the part the CLI cannot do on its own):
  The CLI auto-installs the native-messaging host on first ``--chrome`` run
  (host ``com.anthropic.claude_code_browser_extension``; Windows registers it
  under ``HKCU\\Software\\<Browser>\\NativeMessagingHosts``, Linux writes a
  manifest under ``~/.config/<browser>/NativeMessagingHosts``). But a real
  Chrome/Chromium must be running with the Claude in Chrome extension installed,
  signed in to the same Claude account, and paired. On the user's Windows
  machine this is the existing desktop Chrome; in Docker a headless Chromium
  must be provisioned and paired once (see LOCAL_SETUP / Dockerfile notes).

SECURITY NOTE (SSRF / local-network reach): the agent drives a real browser
with no egress filtering. It can reach loopback (127.0.0.1/localhost), RFC1918
private hosts on the same network, and cloud metadata endpoints (e.g.
169.254.169.254, which on some clouds exposes credentials). Operators who run
untrusted prompts should isolate the host at the network layer. We do NOT
implement egress filtering here.
"""

import logging
from typing import List

from app.core.mcp_service import McpServerConfig, McpServerType

logger = logging.getLogger(__name__)


# Name the browser integration is registered under. Tools are exposed to the
# agent as ``mcp__claude-in-chrome__<tool>`` (e.g.
# ``mcp__claude-in-chrome__navigate``); the allow rule uses the server-level
# wildcard ``mcp__claude-in-chrome`` which permits every tool from it.
BROWSER_SERVER_NAME = "claude-in-chrome"

# The Claude Code CLI flag that enables this integration. ``extra_args`` maps a
# None value to a bare ``--<flag>`` (see SubprocessCLITransport), so this becomes
# ``--chrome`` on the bundled CLI command line.
BROWSER_CLI_FLAG = "chrome"

# Chrome extension id for the Claude in Chrome extension. Referenced for docs /
# Docker enterprise-policy force-install.
CHROME_EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


def get_browser_server_config() -> McpServerConfig:
    """
    Build the McpServerConfig for the Claude in Chrome integration.

    Returns a CLI-flag built-in (``cli_flag="chrome"``) with no spawn command.
    It is surfaced in the admin MCP list and toggled like any other server, but
    is enabled by adding ``--chrome`` to the CLI rather than by spawning a
    subprocess.
    """
    return McpServerConfig(
        name=BROWSER_SERVER_NAME,
        type=McpServerType.STDIO,
        command=None,
        cli_flag=BROWSER_CLI_FLAG,
        scope="builtin",
        enabled=True,
        builtin=True,
        description=(
            "Claude in Chrome — drive a real, signed-in Chrome/Chromium browser "
            "via Anthropic's browser extension. Exposes mcp__claude-in-chrome__* "
            "tools (navigate, read pages, click/type, screenshots, console/network "
            "logs). Requires Chrome with the Claude in Chrome extension installed, "
            "signed in, and paired."
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

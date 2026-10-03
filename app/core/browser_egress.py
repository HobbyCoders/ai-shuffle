"""Launch wiring for the browser's egress proxy.

The supervised browser runs behind ``browser-mcp/dist/egress-launch.js``, a
loopback-only proxy that sees every TCP connection the browser makes
(WebSockets and loopback included), resolves each host once and connects only
to addresses the SSRF policy allows. The CDP request guard inside browser-mcp
cannot close those two gaps on its own.

The proxy enforces the same base policy the app hands every browser MCP
server, so this module derives it from ``mcp_config`` instead of restating the
defaults. ``python -m app.core.browser_egress`` prints it as ``KEY=VALUE``
lines for ``docker/start-chrome.sh``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

# The policy knobs the proxy reads (see browser-mcp/src/ssrf.ts compilePolicy).
EGRESS_ENV_KEYS = (
    "BROWSER_SSRF_ALLOW",
    "BROWSER_SSRF_BLOCK",
    "BROWSER_SSRF_DENY_PORTS",
    "CHROME_CDP_PORT",
)

LAUNCHER_NAME = "egress-launch.js"


class EgressUnavailable(RuntimeError):
    """The browser cannot be started behind its egress proxy."""


def egress_env() -> dict[str, str]:
    """The SSRF policy env for the proxy: the app's base browser policy.

    Session-level overrides do not apply here: the browser, and so its proxy,
    is shared by every session.
    """
    from app.core.mcp_config import _build_browser_server_env

    env = _build_browser_server_env(None)
    return {key: env[key] for key in EGRESS_ENV_KEYS if env.get(key)}


def launcher_path() -> Path:
    """``egress-launch.js`` beside the browser MCP entry point."""
    from app.core.builtin_mcp_servers import _browser_mcp_entry

    return Path(_browser_mcp_entry()).with_name(LAUNCHER_NAME)


def launch_command(exe: str, args: list[str], node: str | None = None) -> list[str]:
    """The argv that starts ``exe`` behind the egress proxy.

    Raises :class:`EgressUnavailable` when node or the launcher is missing: the
    browser is never started without its proxy.
    """
    node = node or shutil.which("node")
    if not node:
        raise EgressUnavailable("node is not on PATH; the browser egress proxy cannot run")
    launcher = launcher_path()
    if not launcher.is_file():
        raise EgressUnavailable(
            f"browser egress proxy missing: {launcher} (rebuild browser-mcp: "
            "npm --prefix browser-mcp ci && npm --prefix browser-mcp run build)"
        )
    return [node, str(launcher), "--watch-stdin", "--", exe, *args]


def main() -> None:
    for key, value in egress_env().items():
        if "\n" not in value and "\r" not in value:
            print(f"{key}={value}")


if __name__ == "__main__":
    main()

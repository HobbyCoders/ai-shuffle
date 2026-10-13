"""
MCP server and plugin configuration for Claude queries.

Contains functions that build MCP server dictionaries and plugin lists
for ``AgentOptions``.
"""

import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

from app.core.config import settings
from app.core.mcp_service import get_mcp_service, McpServerType

logger = logging.getLogger(__name__)


# Env vars injected into the spawned ``browser`` MCP server subprocess, sourced
# from the session's full_env (built by query_options). Keyed on server name so
# no other STDIO server is affected. The ANTHROPIC_* subset feeds the
# ``browser_find`` cheap-model sub-call: a direct API key plus the small-fast
# model id. Claude cloud/gateway auth is never handed to this server.
# CHROME_CDP_PORT tells the server which supervised
# Chromium CDP port to attach to (default 9221); BROWSER_MCP_ENTRY is passed
# through for dev overrides. BROWSER_UPLOAD_DIR gates browser_upload — the
# server rejects all uploads unless it is set to an allowlisted directory.
# Values that are unset/empty are omitted.
_BROWSER_ENV_KEYS = (
    # Claude gateway credentials/routing are CLI-only, never browser-find auth.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CHROME_CDP_PORT",
    "BROWSER_MCP_ENTRY",
    "BROWSER_UPLOAD_DIR",
    "BROWSER_SSRF_BLOCK",
    "BROWSER_SSRF_ALLOW",
    "BROWSER_LOOPBACK_HOST",
)

# Keys that may also be sourced from the process environment when absent from
# full_env (Docker sets these on the container, not per-session). The SSRF knobs
# belong here for the same reason: an operator sets them in .env / compose, and
# full_env is built from the session profile, so without this fallback the
# documented override would silently never reach the spawned server.
_BROWSER_ENV_OS_FALLBACK_KEYS = (
    "CHROME_CDP_PORT",
    "BROWSER_MCP_ENTRY",
    "BROWSER_UPLOAD_DIR",
    "BROWSER_SSRF_BLOCK",
    "BROWSER_SSRF_ALLOW",
    "BROWSER_LOOPBACK_HOST",
)

# Process-env override for the loopback panel-register URL (same fallback shape
# as _BROWSER_ENV_OS_FALLBACK_KEYS: an operator may point the spawned server at
# a non-default host/port when the backend is not on 127.0.0.1:{settings.port}).
_BROWSER_PANEL_REGISTER_URL_OS_KEY = "BROWSER_PANEL_REGISTER_URL"

# Loopback CIDRs handed to browser-mcp's BROWSER_SSRF_ALLOW unless the operator
# has disabled it. browser-mcp blocks these by default; without them an agent
# cannot view a dev server it started itself, which is the single most common
# thing it wants a browser for on a self-hosted box.
_LOOPBACK_ALLOW = "127.0.0.0/8,::1/128"


def _browser_ssrf_allow(explicit: Optional[str]) -> Optional[str]:
    """
    Resolve the BROWSER_SSRF_ALLOW value for the spawned browser MCP server.

    Resolution order, most specific first:

    1. An explicitly-configured ``BROWSER_SSRF_ALLOW`` (session env or process
       env) is passed through untouched — the operator has stated the policy
       and we do not append to it.
    2. ``BROWSER_SSRF_ALLOW_LOOPBACK`` true/false, when set, decides.
    3. Unset: loopback is ALLOWED in every runtime (Docker, native and
       desktop). AI Shuffle has no multi-tenant mode — one owner, and the agent
       already has a shell on the same host — so a self-hosted install is
       single-user whether it runs in Docker or natively. ``is_local_mode()``
       is a Docker-vs-native probe, not a tenancy signal, and gating on it
       refused a single-user Unraid agent checking a UI on 127.0.0.1 (commit
       e55cfa74). An install fronted for other people sets
       ``BROWSER_SSRF_ALLOW_LOOPBACK=false``.

    The tradeoff: with loopback allowed, a page the agent visits can reach
    anything listening on the worker's loopback that is not on the deny list.
    The worker's own ports and the DevTools port are always denied (see
    ``_worker_deny_ports`` and browser-mcp ``compilePolicy``).

    Returns None only when loopback is explicitly disabled, leaving browser-mcp
    on its own default-block policy (loopback, private LAN and metadata refused).
    """
    if explicit:
        return explicit
    opt_in = settings.browser_ssrf_allow_loopback
    if opt_in is None:
        opt_in = True
    return _LOOPBACK_ALLOW if opt_in else None


def _own_routable_address() -> Optional[str]:
    """This host's address on the interface it would use to reach the network.

    A UDP socket that is only "connected" sends nothing; the kernel still picks
    the outbound interface, so ``getsockname`` names the address other machines
    (and other containers) can reach this one on. ``gethostname`` resolution is
    not used: on Windows it happily returns a VPN or Hyper-V adapter, and inside
    a container it often resolves to loopback.
    """
    import socket

    for family, probe in (
        (socket.AF_INET, ("10.255.255.255", 1)),
        (socket.AF_INET6, ("2001:4860:4860::8888", 1)),
    ):
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.connect(probe)
            address = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            sock.close()
        if address and not address.startswith(("127.", "::1", "fe80")):
            return address
    return None


def _browser_loopback_host(explicit: Optional[str]) -> Optional[str]:
    """Where the browser should look when a loopback URL is really this host.

    The shell an agent runs commands in and the Chrome this server drives are
    not always the same network namespace: on a shared runner the shell is a
    container and the browser lives elsewhere, so a dev server the agent just
    started on ``localhost:5179`` is unreachable by that name. browser-mcp
    retries such a URL against this address (only after the direct attempt
    fails) and, failing that, names it in the error instead of leaving the model
    to discover ``hostname -I`` on its own.
    """
    if explicit:
        return explicit
    try:
        return _own_routable_address()
    except Exception:  # pragma: no cover - socket policy varies by platform
        logger.debug("Could not resolve a routable address for the browser", exc_info=True)
        return None


def _host_cidr(address: str) -> str:
    """``address`` as a single-host CIDR (``/32`` or ``/128``)."""
    return f"{address}/128" if ":" in address else f"{address}/32"


def _browser_behind_egress_proxy() -> bool:
    """Whether the browser the spawned server drives was started by egress-launch.

    Only then may browser-mcp read an https ``ERR_TUNNEL_CONNECTION_FAILED`` as
    a refusal by the egress policy (``BROWSER_EGRESS_PROXY``, see
    browser-mcp/src/loopback.ts); any other proxy's failure must not be
    reported as one. In the Docker image the supervised Chromium is always
    launched through the proxy (start-chrome.sh refuses otherwise) unless
    ``START_CHROME`` turned it off. On a native install it is so only while
    the native supervisor runs; an external browser on the CDP port is not.
    Unknown is "no": a missed flag only makes the message less specific.
    """
    try:
        from app.core import native_chrome

        if native_chrome._in_container():
            return os.environ.get("START_CHROME", "1") == "1"
        task = native_chrome._supervisor_task
        return task is not None and not task.done()
    except Exception:  # pragma: no cover - defensive: never break the env build
        logger.debug("Could not tell whether the browser runs behind its egress proxy", exc_info=True)
        return False


def _worker_deny_ports(extra: Optional[str]) -> str:
    """The worker's local and protected listener ports, plus any extras."""
    ports = {str(settings.port)}
    try:
        from app.serve import configured_remote_port

        ports.add(str(configured_remote_port(os.environ)))
    except Exception:  # pragma: no cover - misconfigured protected port
        ports.add("8001")
    desktop_port = os.environ.get("SHUFFLE_DESKTOP_PROTECTED_PORT", "")
    for token in f"{extra or ''} {desktop_port}".replace(",", " ").split():
        if token.isdigit():
            ports.add(token)
    return ",".join(sorted(ports, key=int))


def _build_browser_server_env(
    tool_env: Optional[Dict[str, str]],
    session_id: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build the curated env subset for the spawned ``browser`` MCP server.

    Pulls the whitelisted keys from the session's full_env (``tool_env``),
    falling back to the process environment for the container-level knobs
    (``CHROME_CDP_PORT``/``BROWSER_MCP_ENTRY``). Only non-empty values are
    included so we never inject blank overrides.

    When ``session_id`` is provided, also inject the live-panel wiring so the
    spawned server can publish its current CDP target back to the backend:
    ``AIS_SESSION_ID``, ``AIS_PANEL_TOKEN`` (idempotent per session) and
    ``AIS_PANEL_REGISTER_URL`` (loopback register endpoint, overridable via the
    ``BROWSER_PANEL_REGISTER_URL`` process env).
    """
    from fluid.tools.child_env import scrub_claude_auth_env

    src = scrub_claude_auth_env(tool_env or {})
    env: Dict[str, str] = {}
    for key in _BROWSER_ENV_KEYS:
        value = src.get(key)
        if not value and key in _BROWSER_ENV_OS_FALLBACK_KEYS:
            value = os.environ.get(key)
        if value:
            env[key] = value

    # Agent uploads stay confined to the workspace unless an operator provides
    # a narrower/different allowlist. Panel files use a separate, app-owned root.
    env.setdefault("BROWSER_UPLOAD_DIR", str(settings.effective_workspace_dir.resolve()))
    env["AIS_BROWSER_PANEL_UPLOAD_DIR"] = str(
        (settings.effective_workspace_dir / ".browser-uploads").resolve()
    )

    # Loopback egress: injected only when nothing explicit was configured above.
    explicit_allow = env.get("BROWSER_SSRF_ALLOW")
    ssrf_allow = _browser_ssrf_allow(explicit_allow)

    loopback_host = _browser_loopback_host(env.get("BROWSER_LOOPBACK_HOST"))
    if loopback_host:
        env["BROWSER_LOOPBACK_HOST"] = loopback_host
        # browser-mcp refuses private addresses by default. The loopback
        # stand-in is this host, so it rides on the automatic loopback allow;
        # an operator's explicit allow-list is left exactly as written.
        if ssrf_allow and not explicit_allow:
            ssrf_allow = f"{ssrf_allow},{_host_cidr(loopback_host)}"
    if ssrf_allow:
        env["BROWSER_SSRF_ALLOW"] = ssrf_allow

    # The worker's own listeners stay closed to the agent's browser on every
    # internal address, whatever the allow-list says: a request from inside
    # the container to them is treated as the owner.
    env["BROWSER_SSRF_DENY_PORTS"] = _worker_deny_ports(os.environ.get("BROWSER_SSRF_DENY_PORTS"))

    if _browser_behind_egress_proxy():
        env["BROWSER_EGRESS_PROXY"] = "1"

    if session_id:
        from app.core import browser_panel_registry

        env["AIS_SESSION_ID"] = session_id
        env["AIS_BROWSER_MODE"] = "proxy"
        env["AIS_PANEL_TOKEN"] = browser_panel_registry.issue_token(session_id)
        register_url = os.environ.get(_BROWSER_PANEL_REGISTER_URL_OS_KEY)
        if not register_url:
            register_url = (
                f"http://127.0.0.1:{settings.port}/api/v1/browser/panel/register"
            )
        env["AIS_PANEL_REGISTER_URL"] = register_url

    return env


def _build_mcp_servers_dict(
    project_dir: Optional[Path] = None,
    tool_env: Optional[Dict[str, str]] = None,
    session_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Build the mcp_servers dict for ``AgentOptions`` from registered MCP servers.

    Reads MCP servers from two sources:
    1. Scope-based .mcp.json files (user, local, project) via McpService
    2. Installed+enabled plugin directories (each plugin can have its own .mcp.json)

    Args:
        project_dir: Optional project directory for project-scoped servers.
        tool_env: The session's full_env; a curated subset is injected into the
            spawned ``browser`` built-in server only (see _build_browser_server_env).

    Returns:
        Dictionary mapping server names to SDK MCP server configs, or None if empty.
    """

    from app.core.builtin_mcp_servers import BROWSER_SERVER_NAME

    mcp_servers: dict = {}

    # --- Source 1: Scope-based .mcp.json files ---
    service = None
    try:
        service = get_mcp_service(project_dir)
        all_servers = service.get_all_servers()
    except Exception as e:
        logger.warning(f"Failed to load MCP servers from scopes: {e}")
        all_servers = {}

    for scope_servers in all_servers.values():
        for server in scope_servers:
            if not server.enabled:
                continue
            if server.name in mcp_servers:
                continue
            # Legacy CLI-flag built-ins carried no command/url and were skipped
            # here; the ``browser`` built-in is now an ordinary spawnable STDIO
            # server and passes through the normal path below. Any stray config
            # without a command is still skipped defensively.
            if getattr(server, "cli_flag", None):
                continue
            if server.type == McpServerType.STDIO or server.type == "stdio":
                if not server.command:
                    continue
                config: dict = {"command": server.command}
                if server.args:
                    config["args"] = server.args
                # Merge the server's own env with the curated per-session subset
                # for the spawned ``browser`` built-in (ANTHROPIC_* + CDP port).
                merged_env: dict = dict(server.env) if server.env else {}
                if server.name == BROWSER_SERVER_NAME:
                    merged_env.update(
                        _build_browser_server_env(tool_env, session_id=session_id)
                    )
                if merged_env:
                    config["env"] = merged_env
                mcp_servers[server.name] = config
            elif server.type == McpServerType.SSE or server.type == "sse":
                config = {"type": "sse", "url": server.url}
                if server.headers:
                    config["headers"] = server.headers
                mcp_servers[server.name] = config
            elif server.type == McpServerType.HTTP or server.type == "http":
                config = {"type": "http", "url": server.url}
                if server.headers:
                    config["headers"] = server.headers
                mcp_servers[server.name] = config

    # NOTE: Plugin MCP servers (e.g. Context7) are NOT added here.
    # Native legs load enabled plugins from ~/.fluid/plugins. Manually adding
    # them here would create duplicates
    # (e.g. mcp__context7__* AND mcp__plugin_context7_context7__*).

    if mcp_servers:
        logger.info(f"Passing {len(mcp_servers)} MCP server(s) to SDK: {list(mcp_servers.keys())}")
        return mcp_servers

    return None


def _build_mcp_servers_dict_with_ai_tools(
    project_dir: Optional[Path] = None,
    ai_tools_config: Optional[Dict[str, Any]] = None,
    tool_env: Optional[Dict[str, str]] = None,
    session_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Build MCP servers dict including AI content generation tools.

    Wraps _build_mcp_servers_dict() and adds the AI tools SDK MCP server
    when any AI tools are enabled in the profile.

    Args:
        project_dir: Optional project directory for project-scoped servers.
        ai_tools_config: Optional dict with individual AI tool toggles.
        tool_env: The session's full_env (API keys, provider settings). Injected
            into AI tool Node.js subprocesses, and a curated subset is injected
            into the spawned ``browser`` built-in server (see _build_mcp_servers_dict).

    Returns:
        Dictionary mapping server names to SDK MCP server configs, or None if empty.
    """
    mcp_servers = _build_mcp_servers_dict(
        project_dir=project_dir,
        tool_env=tool_env,
        session_id=session_id,
    ) or {}

    # Add AI tools SDK MCP server if any AI tools are enabled
    if ai_tools_config and any(ai_tools_config.values()):
        try:
            from app.mcp.ai_tools_sdk import create_ai_tools_server
            tools_dir = settings.effective_tools_dir
            if tools_dir:
                ai_server = create_ai_tools_server(
                    ai_tools_config, str(tools_dir),
                    tool_env=tool_env,
                    tool_cwd=str(project_dir) if project_dir else str(settings.effective_workspace_dir),
                )
                if ai_server:
                    mcp_servers["ai-tools"] = ai_server
                    logger.debug("Added in-process SDK MCP server for ai-tools")
        except Exception as e:
            logger.warning(f"Failed to create AI tools SDK MCP server: {e}")

    if mcp_servers:
        return mcp_servers

    return None


def _build_plugins_list(
    profile_enabled_plugins: Optional[list],
    setting_sources: Optional[list] = None
) -> Optional[list]:
    """
    Build the plugins list for ``AgentOptions``.

    IMPORTANT: When setting_sources includes "user", the Claude Code CLI auto-loads plugins from
    ~/.claude/settings.json. We must NOT pass those same plugins explicitly, or they
    will be loaded twice (causing duplicate skills, agents, etc.).

    This function only adds plugins from profile config when:
    1. setting_sources does NOT include "user" (we need to load all plugins explicitly)
    2. OR the plugin is specified in profile config but NOT in user settings

    Entries use the SDK plugin shape:
    [{"type": "local", "path": "./my-plugin"}, ...]

    Supports two formats for plugin entries:
    1. Plugin IDs: "plugin-name@marketplace" - resolved via installed_plugins.json
    2. Direct paths: "/path/to/plugin" or "relative/path" - used directly

    Args:
        profile_enabled_plugins: List of plugin IDs or paths from profile config
        setting_sources: List of setting sources (e.g., ["user", "project"])

    Returns:
        List of plugin dicts for SDK, or None if no plugins enabled/needed
    """
    # Import here to avoid circular imports
    from app.core.plugin_service import get_plugin_service
    service = get_plugin_service()

    # When "user" is in setting_sources, the CLI loads plugins from ~/.claude/settings.json
    sdk_loads_user_plugins = setting_sources and "user" in setting_sources

    # Load installed plugins registry to resolve plugin IDs
    installed_plugins = {}
    installed_plugins_file = service.INSTALLED_FILE
    try:
        if installed_plugins_file.exists():
            import json
            with open(installed_plugins_file, 'r') as f:
                data = json.load(f)
                installed_plugins = data.get("plugins", {})
    except Exception as e:
        logger.warning(f"Failed to load installed_plugins.json: {e}")

    # Get plugins enabled in Claude's settings.json (for reference/logging)
    settings_enabled = service.get_enabled_plugins()  # Returns Dict[str, bool]
    settings_plugin_ids = {pid for pid, enabled in settings_enabled.items() if enabled}
    logger.debug(f"Plugins in user settings.json: {list(settings_plugin_ids)}")

    # Build list of plugins to pass explicitly
    all_plugin_entries = []

    if sdk_loads_user_plugins:
        # The CLI auto-loads user settings plugins - only add profile-specific plugins
        # that are NOT already in user settings (to avoid duplicates)
        logger.debug("SDK will auto-load user plugins (setting_sources includes 'user')")
        if profile_enabled_plugins:
            for entry in profile_enabled_plugins:
                if entry and entry not in settings_plugin_ids:
                    all_plugin_entries.append(entry)
                    logger.debug(f"Adding profile-only plugin (not in user settings): {entry}")
    else:
        # The CLI won't auto-load - we need to pass all plugins explicitly
        logger.debug("SDK won't auto-load plugins - adding all enabled plugins explicitly")
        for plugin_id, is_enabled in settings_enabled.items():
            if is_enabled:
                all_plugin_entries.append(plugin_id)

        # Add plugins from profile config (may be paths or IDs)
        if profile_enabled_plugins:
            for entry in profile_enabled_plugins:
                if entry and entry not in all_plugin_entries:
                    all_plugin_entries.append(entry)

    if not all_plugin_entries:
        logger.debug("No plugins enabled in settings.json or profile config")
        return None

    plugins = []
    seen_paths = set()  # Avoid duplicates

    for plugin_entry in all_plugin_entries:
        if not plugin_entry:
            continue

        resolved_path = None

        # Check if this is a plugin ID format (name@marketplace)
        if "@" in plugin_entry and not plugin_entry.startswith("/") and not plugin_entry.startswith("."):
            # Look up in installed_plugins registry
            if plugin_entry in installed_plugins:
                installations = installed_plugins[plugin_entry]
                if installations:
                    resolved_path = Path(installations[0].get("installPath", ""))
                    if not resolved_path.exists():
                        logger.warning(f"Plugin install path not found: {resolved_path}")
                        resolved_path = None
            else:
                logger.warning(f"Plugin not found in installed_plugins.json: {plugin_entry}")
        else:
            # Direct path format
            path = Path(plugin_entry)
            if path.is_absolute():
                resolved_path = path
            else:
                resolved_path = settings.effective_workspace_dir / plugin_entry

        if resolved_path and str(resolved_path) not in seen_paths:
            # Verify the plugin directory exists (manifest path checked for existence)
            _plugin_manifest = resolved_path / ".claude-plugin" / "plugin.json"
            if resolved_path.exists():
                plugins.append({
                    "type": "local",
                    "path": str(resolved_path)
                })
                seen_paths.add(str(resolved_path))
                logger.debug(f"Adding plugin: {resolved_path}")
            else:
                logger.warning(f"Plugin directory not found: {resolved_path}")

    if not plugins:
        return None

    logger.debug(f"Built plugins list with {len(plugins)} plugin(s)")
    return plugins

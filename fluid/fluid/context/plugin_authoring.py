"""AI-authored plugins: the ``authored`` plugin source and its approvals.

The ``save_plugin`` tool writes a plugin an agent drafted into
``~/.fluid/plugins/cache/authored/<name>/<version>/`` in the Agent Plugins
layout (root ``plugin.json``, ``skills/``, ``mcp.json``; commands and agents
declared under ``extensions["ai.shuffle"]``), so the normal loader and the
app's plugin list find it at once.

Writing a plugin never turns it on. It loads only after the owner enabled it,
which records an approval in ``~/.fluid/plugins/authored_approvals.json``
pinned to the content hash of the exact version they reviewed. Any later edit
or new version changes the hash, and the plugin stops loading until it is
approved again. The tool never writes that file.

What the tool refuses, so a draft cannot do more than a reviewer can see:
hooks (they run automatically), MCP server ``env`` values other than
``${VAR}`` references (no literal secrets on disk), text that looks like a
credential, paths outside the plugin folder, oversized drafts, and rewriting
a version that already exists with different content.

A plugin that needs an outside service declares it under
``extensions["ai.shuffle"].services`` by name, provider, API address and
read/write access, and optionally an OAuth sign-in (``auth``: public
endpoints and scope names only). That is a declaration for the reviewer and
the agent: the credential itself (a key, or the OAuth client and its tokens)
is entered by the owner privately and never appears in the plugin.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fluid.context.plugin_formats import (
    _DATA_VARS,
    _ROOT_VARS,
    _VAR_RE,
    SHUFFLE_EXTENSION,
    content_hash,
    mcp_server_detail,
    read_plugin,
)
from fluid.tools.child_env import is_app_secret

log = logging.getLogger(__name__)

AUTHORED_SOURCE = "authored"
APPROVALS_FILE = "authored_approvals.json"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION_RE = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}$")
_ENV_REF_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

MAX_COMPONENTS = 20
MAX_SERVERS = 10
MAX_SERVICES = 10
SERVICE_PROVIDERS = ("cloudflare", "custom")
SERVICE_ACCESS = ("read", "write")

# Text that is almost certainly a credential. A plugin never holds one.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an API key (sk-...)", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("a GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{24,}")),
)
MAX_TEXT = 50_000
MAX_DESCRIPTION = 500
MAX_TOTAL_BYTES = 256 * 1024
# The review screen shows each MCP server's whole command line, so a draft
# may not hide part of it past the display limit.
MAX_COMMAND_LINE = 2000
_PLUGIN_VARS = _ROOT_VARS | _DATA_VARS | {"CLAUDE_PROJECT_DIR"}


class PluginDraftError(ValueError):
    """The draft was refused; the message says why, for the model to fix."""


@dataclass(frozen=True, kw_only=True)
class SavedPlugin:
    id: str
    name: str
    version: str
    path: Path
    content_hash: str
    unchanged: bool = False
    components: list[str] = field(default_factory=list)


def plugins_home() -> Path:
    return Path.home() / ".fluid" / "plugins"


def authored_dir(home: Path | None = None) -> Path:
    return (plugins_home() if home is None else home) / "cache" / AUTHORED_SOURCE


def approvals_path(home: Path | None = None) -> Path:
    return (plugins_home() if home is None else home) / APPROVALS_FILE


# ─── approvals ───────────────────────────────────────────────────────────────


def read_approvals(home: Path | None = None) -> dict[str, dict[str, Any]]:
    """``{"<name>@authored": {"version", "hash", ...}}``; ``{}`` when unreadable."""
    path = approvals_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("unreadable authored plugin approvals %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    approvals = data.get("approvals", {})
    return {k: v for k, v in approvals.items() if isinstance(v, dict)} if isinstance(approvals, dict) else {}


def is_approved(name: str, root: Path, *, home: Path | None = None) -> bool:
    """Whether the owner approved exactly the content now at ``root``.

    Fails closed: no record, a different version, an unreadable tree or a
    content hash that moved since approval all read as not approved.
    """
    record = read_approvals(home).get(f"{name}@{AUTHORED_SOURCE}")
    if not record:
        return False
    if str(record.get("version") or "") != root.name:
        return False
    current = content_hash(root)
    return current is not None and current == record.get("hash")


# ─── validation ──────────────────────────────────────────────────────────────


def _text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise PluginDraftError(f"{what} is required")
        return ""
    if not isinstance(value, str):
        raise PluginDraftError(f"{what} must be a string")
    if len(value) > limit:
        raise PluginDraftError(f"{what} is longer than {limit} characters")
    if "\0" in value:
        raise PluginDraftError(f"{what} contains a NUL character")
    return value


def _name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise PluginDraftError(
            f"{what} must be lowercase letters, digits and hyphens (1-63 characters), "
            f"starting with a letter or digit"
        )
    return value


def _oneline(value: str) -> str:
    return " ".join(value.split())


def _entries(value: Any, what: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise PluginDraftError(f"{what} must be a list of objects")
    if len(value) > MAX_COMPONENTS:
        raise PluginDraftError(f"at most {MAX_COMPONENTS} {what} per plugin")
    names = [v.get("name") for v in value]
    if len(set(map(str, names))) != len(names):
        raise PluginDraftError(f"{what} names must be unique")
    return value


def _server(name: str, cfg: Any) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise PluginDraftError(f"MCP server {name!r} must be an object")
    allowed = {"command", "args", "env", "url", "type"}
    extra = sorted(set(cfg) - allowed)
    if extra:
        raise PluginDraftError(f"MCP server {name!r}: unsupported keys {extra}")
    if "url" in cfg:
        url = _text(cfg.get("url"), f"MCP server {name!r} url", 2000, required=True)
        if not url.startswith(("https://", "http://")):
            raise PluginDraftError(f"MCP server {name!r}: url must start with https:// or http://")
        if cfg.get("command") or cfg.get("args") or cfg.get("env"):
            raise PluginDraftError(f"MCP server {name!r}: give a url or a command, not both")
        return {"type": "http", "url": url}
    command = _text(cfg.get("command"), f"MCP server {name!r} command", 500, required=True)
    args = cfg.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) and len(a) <= 2000 for a in args):
        raise PluginDraftError(f"MCP server {name!r}: args must be a list of strings")
    out: dict[str, Any] = {"command": command, "args": list(args)}
    env = cfg.get("env")
    if env:
        if not isinstance(env, dict):
            raise PluginDraftError(f"MCP server {name!r}: env must be an object")
        for key, val in env.items():
            if not isinstance(key, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise PluginDraftError(f"MCP server {name!r}: bad env name {key!r}")
            if not isinstance(val, str) or not _ENV_REF_RE.match(val):
                raise PluginDraftError(
                    f"MCP server {name!r}: env value for {key} must be a ${{VAR}} reference; "
                    "secrets are never written into a plugin"
                )
        out["env"] = dict(env)
    if len(mcp_server_detail(out, limit=MAX_COMMAND_LINE + 1)) > MAX_COMMAND_LINE:
        raise PluginDraftError(
            f"MCP server {name!r}: the command line is longer than {MAX_COMMAND_LINE} characters"
        )
    return out


def secret_findings(text: str) -> list[str]:
    """What kinds of credential ``text`` appears to contain (empty when none)."""
    return [label for label, pattern in _SECRET_PATTERNS if pattern.search(text)]


def _no_secrets(text: str, what: str) -> None:
    found = secret_findings(text)
    if found:
        raise PluginDraftError(
            f"{what} looks like it contains {found[0]}; a plugin never holds a credential. "
            "Ask for access with the private access card instead"
        )


_MCP_TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_*-]+$")


def _tools(value: Any, what: str) -> list[str]:
    """An agent's ``tools`` list: this app's tool names only."""
    from fluid.context.plugin_import import NATIVE_TOOL_NAMES, map_tool

    if value is None or value == [] or value == "":
        return []
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(t, str) for t in items):
        raise PluginDraftError(f"{what} must be a list of tool names")
    out: list[str] = []
    for raw in (t.strip() for t in items):
        if not raw:
            continue
        if raw in NATIVE_TOOL_NAMES or _MCP_TOOL_RE.match(raw):
            if raw not in out:
                out.append(raw)
            continue
        native, _ = map_tool(raw)
        hint = f"; use {native}" if native else ""
        raise PluginDraftError(f"{what}: {raw!r} is not a tool here{hint}")
    return out


_SERVICE_KEYS = {"name", "provider", "url", "access", "purpose", "auth"}
_AUTH_KEYS = {"type", "authorize_url", "token_url", "scopes", "pkce", "revoke_url"}
_AUTH_URL_KEYS = ("authorize_url", "token_url", "revoke_url")
MAX_SCOPES = 20
# RFC 6749 section 3.3 scope-token: printable ASCII without space, '"' or '\'.
_SCOPE_RE = re.compile(r'^[\x21\x23-\x5b\x5d-\x7e]{1,200}$')
# Second-level labels under a two-letter country code that are a public
# suffix of their own (example.co.uk, example.com.au). Without the Public
# Suffix List this keeps "same registrable domain" strict for the common
# cases: a country code with one of these takes three labels.
_CC_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "edu", "ac", "or", "ne", "go", "gob", "mil", "nic"}


def _secret_key_name(key: Any) -> bool:
    """A field name that would hold an OAuth client secret or another secret."""
    return isinstance(key, str) and "secret" in re.sub(r"[^a-z]", "", key.lower())


def registrable_domain(host: str) -> str:
    """The part of ``host`` an organisation registers: the last two labels,
    or three under a country-code second level such as ``co.uk``.

    IP addresses and single-label hosts are returned whole, so they only ever
    match themselves. A simple rule, deliberately strict: hosts that a full
    Public Suffix List would group differently are refused, not allowed.
    """
    host = host.lower().rstrip(".")
    labels = host.split(".")
    if len(labels) < 2 or all(part.isdigit() for part in labels) or ":" in host:
        return host
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _CC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _https_host(url: Any) -> str | None:
    """The host of a plain ``https://`` URL with no credentials, query or
    fragment, else ``None``."""
    from urllib.parse import urlsplit

    if not isinstance(url, str) or not url.startswith("https://") or any(c in url for c in "?#@ \\"):
        return None
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return None
    return parts.hostname


def check_service_auth(sname: str, service_url: str, auth: Any) -> dict[str, Any]:
    """Validate a service's ``auth`` block: how the owner signs in to it.

    Only ``{"type": "oauth2", "authorize_url", "token_url", "scopes": [...],
    "pkce": true}`` and an optional ``revoke_url``. Every URL is plain
    ``https://`` with no credentials, query or fragment, on the same
    registrable domain as the service ``url``. A client secret is refused by
    name: the owner enters the client on their own device, never in a plugin.

    Raises :class:`PluginDraftError`; returns the block in the exact shape
    ``build_files`` writes.
    """
    if not isinstance(auth, dict):
        raise PluginDraftError(f"service {sname}: auth must be an object")
    secret_keys = sorted(k for k in auth if _secret_key_name(k))
    if secret_keys:
        raise PluginDraftError(
            f"service {sname}: auth holds {secret_keys[0][:40]!r}; a plugin never carries an OAuth "
            "client secret. The owner enters the client privately on their own device"
        )
    extra = sorted(str(k)[:40] for k in set(auth) - _AUTH_KEYS)
    if extra:
        raise PluginDraftError(f"service {sname}: auth takes only {sorted(_AUTH_KEYS)}; remove {extra}")
    if auth.get("type") != "oauth2":
        raise PluginDraftError(f"service {sname}: auth type must be 'oauth2'")
    if auth.get("pkce") is not True:
        raise PluginDraftError(f"service {sname}: auth must set \"pkce\": true")
    service_host = _https_host(service_url) if service_url else None
    if not service_host:
        raise PluginDraftError(
            f"service {sname}: a service with auth needs its https:// API url, so the sign-in "
            "addresses can be checked against it"
        )
    domain = registrable_domain(service_host)
    out: dict[str, Any] = {"type": "oauth2"}
    for key in _AUTH_URL_KEYS:
        value = auth.get(key)
        if value is None and key == "revoke_url":
            continue
        _text(value, f"service {sname} auth {key}", 500, required=True)
        host = _https_host(value)
        if not host:
            raise PluginDraftError(
                f"service {sname}: auth {key} must be an https:// address with no query, "
                "fragment or credentials"
            )
        if registrable_domain(host) != domain:
            raise PluginDraftError(
                f"service {sname}: auth {key} must be on {domain}, the same domain as the service url"
            )
        out[key] = value
    scopes = auth.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise PluginDraftError(f"service {sname}: auth scopes must be a list of strings")
    if len(scopes) > MAX_SCOPES:
        raise PluginDraftError(f"service {sname}: at most {MAX_SCOPES} auth scopes")
    if not all(_SCOPE_RE.match(s) for s in scopes):
        raise PluginDraftError(
            f"service {sname}: each auth scope is one word of plain text (no spaces or quotes), "
            "at most 200 characters"
        )
    if len(set(scopes)) != len(scopes):
        raise PluginDraftError(f"service {sname}: auth scopes must be unique")
    out["scopes"] = list(scopes)
    out["pkce"] = True
    return {k: out[k] for k in ("type", "authorize_url", "token_url", "scopes", "pkce", "revoke_url") if k in out}


def describe_service_auth(auth: dict[str, Any]) -> str:
    """One plain line for a reviewer: where the owner signs in and what for."""
    host = _https_host(auth.get("authorize_url", "")) or "an unknown address"
    scopes = auth.get("scopes") or []
    return f"signs in with OAuth at {host}; scopes: {', '.join(scopes) if scopes else 'none'}"


def check_services(value: Any) -> list[dict[str, Any]]:
    """Validate ``extensions["ai.shuffle"].services``: which outside services
    the plugin needs access to and, optionally, how the owner signs in to
    them (``auth``, see :func:`check_service_auth`). Never a credential.

    Raises :class:`PluginDraftError`. Returns the entries in the exact shape
    ``build_files`` writes.
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise PluginDraftError("services must be a list of objects")
    if len(value) > MAX_SERVICES:
        raise PluginDraftError(f"at most {MAX_SERVICES} services per plugin")
    out: list[dict[str, Any]] = []
    for entry in value:
        secret_keys = sorted(k for k in entry if _secret_key_name(k))
        if secret_keys:
            raise PluginDraftError(
                f"service entries never hold a secret such as {secret_keys[0][:40]!r}; an OAuth "
                "client secret or key is entered by the owner in the private access card"
            )
        extra = sorted(set(entry) - _SERVICE_KEYS)
        if extra:
            raise PluginDraftError(
                f"service entries take only {sorted(_SERVICE_KEYS)}; remove {extra}. "
                "Credentials are entered by the owner in the private access card"
            )
        sname = _name(entry.get("name"), "service name")
        provider = entry.get("provider", "custom")
        if provider not in SERVICE_PROVIDERS:
            raise PluginDraftError(f"service {sname}: provider must be one of {list(SERVICE_PROVIDERS)}")
        url = _text(entry.get("url"), f"service {sname} url", 500, required=provider == "custom")
        if url and (not url.startswith("https://") or any(c in url for c in "?#@ ")):
            raise PluginDraftError(
                f"service {sname}: url must be a public https:// API address with no query, "
                "fragment or credentials"
            )
        access = entry.get("access", "read")
        if access not in SERVICE_ACCESS:
            raise PluginDraftError(f"service {sname}: access must be 'read' or 'write'")
        purpose = _oneline(_text(entry.get("purpose"), f"service {sname} purpose", 300, required=True))
        item: dict[str, Any] = {"name": sname, "provider": str(provider)}
        if url:
            item["url"] = url
        item["access"] = str(access)
        item["purpose"] = purpose
        if "auth" in entry:
            item["auth"] = check_service_auth(sname, url, entry["auth"])
        out.append(item)
    if len({v["name"] for v in out}) != len(out):
        raise PluginDraftError("service names must be unique")
    return out


def _host_secret_refs(value: Any) -> set[str]:
    """``${VAR}`` names in ``value`` that are the host app's own secrets."""
    if isinstance(value, str):
        return {
            m.group(1) for m in _VAR_RE.finditer(value)
            if m.group(1) not in _PLUGIN_VARS and is_app_secret(m.group(1))
        }
    if isinstance(value, dict):
        return set().union(*(_host_secret_refs(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_host_secret_refs(v) for v in value)) if value else set()
    return set()


def _frontmatter(fields: dict[str, str]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        value = _oneline(value)
        if not value:
            continue
        if value[0] in "[>|\"'":
            # Quote what fluid's frontmatter reader would otherwise take for a
            # list or a block scalar.
            value = '"' + value.replace('"', "'") + '"'
        lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def build_files(spec: dict[str, Any]) -> tuple[str, str, dict[str, str], list[str]]:
    """Validate a draft and lay it out: ``(name, version, {relpath: text}, components)``."""
    if not isinstance(spec, dict):
        raise PluginDraftError("the plugin must be an object")
    if spec.get("hooks"):
        raise PluginDraftError("hooks are not supported in authored plugins")
    unknown = sorted(set(spec) - {
        "name", "version", "description", "skills", "commands", "agents", "mcp_servers",
        "services",
    })
    if unknown:
        raise PluginDraftError(f"unsupported fields {unknown}")
    name = _name(spec.get("name"), "name")
    version = spec.get("version") or "0.1.0"
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise PluginDraftError("version must look like 1.2.3")
    description = _oneline(_text(spec.get("description"), "description", MAX_DESCRIPTION))

    files: dict[str, str] = {}
    components: list[str] = []
    for skill in _entries(spec.get("skills"), "skills"):
        sname = _name(skill.get("name"), "skill name")
        body = _text(skill.get("body"), f"skill {sname} body", MAX_TEXT, required=True)
        sdesc = _text(skill.get("description"), f"skill {sname} description", MAX_DESCRIPTION, required=True)
        files[f"skills/{sname}/SKILL.md"] = _frontmatter({"name": sname, "description": sdesc}) + body
        components.append(f"skill {sname}")
    for command in _entries(spec.get("commands"), "commands"):
        cname = _name(command.get("name"), "command name")
        body = _text(command.get("body"), f"command {cname} body", MAX_TEXT, required=True)
        cdesc = _text(command.get("description"), f"command {cname} description", MAX_DESCRIPTION)
        files[f"commands/{cname}.md"] = (_frontmatter({"description": cdesc}) if cdesc else "") + body
        components.append(f"command {cname}")
    for agent in _entries(spec.get("agents"), "agents"):
        aname = _name(agent.get("name"), "agent name")
        body = _text(agent.get("body"), f"agent {aname} body", MAX_TEXT, required=True)
        adesc = _text(agent.get("description"), f"agent {aname} description", MAX_DESCRIPTION, required=True)
        tools = _tools(agent.get("tools"), f"agent {aname} tools")
        front = {"name": aname, "description": adesc}
        if tools:
            front["tools"] = ", ".join(tools)
        files[f"agents/{aname}.md"] = _frontmatter(front) + body
        components.append(f"agent {aname}")
    services = check_services(spec.get("services"))

    raw_servers = spec.get("mcp_servers") or {}
    if not isinstance(raw_servers, dict):
        raise PluginDraftError("mcp_servers must be an object keyed by server name")
    if len(raw_servers) > MAX_SERVERS:
        raise PluginDraftError(f"at most {MAX_SERVERS} MCP servers per plugin")
    servers = {_name(k, "MCP server name"): _server(k, v) for k, v in raw_servers.items()}
    secret_refs = _host_secret_refs(servers)
    if secret_refs:
        raise PluginDraftError(
            f"MCP servers may not reference the app's own secrets {sorted(secret_refs)}"
        )
    for server_name in servers:
        components.append(f"MCP server {server_name}")
    if not components:
        raise PluginDraftError("a plugin needs at least one skill, command, agent or MCP server")

    manifest: dict[str, Any] = {"name": name, "version": version}
    if description:
        manifest["description"] = description
    manifest["author"] = {"name": "AI Shuffle agent"}
    if any(k.startswith("skills/") for k in files):
        manifest["skills"] = "./skills/"
    if servers:
        manifest["mcpServers"] = "./mcp.json"
        files["mcp.json"] = json.dumps({"mcpServers": servers}, indent=2) + "\n"
    shuffle: dict[str, Any] = {"authored": True}
    if any(k.startswith("commands/") for k in files):
        shuffle["commands"] = "./commands/"
    if any(k.startswith("agents/") for k in files):
        shuffle["agents"] = "./agents/"
    if services:
        shuffle["services"] = services
    manifest["extensions"] = {SHUFFLE_EXTENSION: shuffle}
    files["plugin.json"] = json.dumps(manifest, indent=2) + "\n"
    for rel, text in files.items():
        _no_secrets(text, rel)

    total = sum(len(v.encode("utf-8")) for v in files.values())
    if total > MAX_TOTAL_BYTES:
        raise PluginDraftError(f"the plugin is {total} bytes; the limit is {MAX_TOTAL_BYTES}")
    return name, version, files, components


# ─── writing ─────────────────────────────────────────────────────────────────


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def save_authored_plugin(spec: dict[str, Any], *, home: Path | None = None) -> SavedPlugin:
    """Write a validated draft as a new version of ``<name>@authored``.

    Raises :class:`PluginDraftError` for a refused draft. Re-saving identical
    content is a no-op (``unchanged=True``); different content under an
    existing version is refused so an approved version is never rewritten.
    """
    name, version, files, components = build_files(spec)
    base = authored_dir(home) / name
    target = base / version
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.resolve().is_relative_to(authored_dir(home).resolve()):
        raise PluginDraftError("the authored plugin folder is not a plain folder")

    # Staged outside the plugin's own folder, so a half-written draft is never
    # mistaken for a version by the loader, then moved into place in one step.
    staging_root = (plugins_home() if home is None else home) / "cache" / ".authored-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{name}-{version}-", dir=staging_root))
    try:
        _write_tree(staging, files)
        digest = content_hash(staging)
        if digest is None:
            raise PluginDraftError("the draft could not be hashed")
        if target.exists():
            if content_hash(target) == digest:
                return SavedPlugin(
                    id=f"{name}@{AUTHORED_SOURCE}", name=name, version=version, path=target,
                    content_hash=digest, unchanged=True, components=components,
                )
            raise PluginDraftError(
                f"version {version} of {name} already exists with different content; "
                "save it under a new version"
            )
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    # Read back through the shared parser so what was saved is what loads.
    record = read_plugin(target, name=name, source=AUTHORED_SOURCE, version=version)
    loaded = [f"{c.kind} {c.name}" for c in record.components if c.support == "loaded"]
    return SavedPlugin(
        id=f"{name}@{AUTHORED_SOURCE}", name=name, version=version, path=target,
        content_hash=digest, components=loaded or components,
    )


# ─── drafts copied between devices ───────────────────────────────────────────
#
# A session can run on a different device than the one whose Settings the
# owner reviews in, so the app copies drafts between the devices of a network
# (see ``app.network.authored_plugins``). A copied draft passes the same
# checks ``save_plugin`` applies and only ever has the files it writes.

_DRAFT_FILE_RE = re.compile(
    r"^(?:plugin\.json|mcp\.json"
    r"|skills/[a-z0-9][a-z0-9-]{0,62}/SKILL\.md"
    r"|(?:commands|agents)/[a-z0-9][a-z0-9-]{0,62}\.md)$"
)
MAX_DRAFT_FILES = 3 * MAX_COMPONENTS + 2
_MANIFEST_KEYS = {"name", "version", "description", "author", "skills", "mcpServers", "extensions"}
_SHUFFLE_KEYS = {"authored": True, "commands": "./commands/", "agents": "./agents/"}


def read_draft_files(root: Path) -> dict[str, str] | None:
    """A saved draft as ``{relpath: text}``.

    ``None`` when the folder holds anything ``save_plugin`` never writes (a
    symlink, another file, text that is not UTF-8), so only drafts that look
    exactly like the tool's output leave this device.
    """
    files: dict[str, str] = {}
    try:
        if not root.is_dir() or root.is_symlink():
            return None
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(dirpath)
            for entry in dirnames + filenames:
                if (base / entry).is_symlink():
                    return None
            for entry in filenames:
                rel = (base / entry).relative_to(root).as_posix()
                if not _DRAFT_FILE_RE.match(rel) or len(files) >= MAX_DRAFT_FILES:
                    return None
                files[rel] = (base / entry).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return files


def check_draft_files(name: str, version: str, files: Any) -> None:
    """Refuse a copied draft that ``save_plugin`` could not have written.

    Raises :class:`PluginDraftError`. Checks the file names, the size, the
    manifest (no hooks, nothing pointing outside the plugin) and every MCP
    server with the same rules as a new draft.
    """
    _name(name, "name")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise PluginDraftError("version must look like 1.2.3")
    if not isinstance(files, dict) or not files or len(files) > MAX_DRAFT_FILES:
        raise PluginDraftError("the draft has no files or too many")
    total = 0
    for rel, text in files.items():
        if not isinstance(rel, str) or not _DRAFT_FILE_RE.match(rel):
            raise PluginDraftError(f"unexpected file {rel!r}")
        if not isinstance(text, str) or "\0" in text:
            raise PluginDraftError(f"{rel} is not text")
        total += len(text.encode("utf-8"))
    if total > MAX_TOTAL_BYTES:
        raise PluginDraftError(f"the plugin is {total} bytes; the limit is {MAX_TOTAL_BYTES}")
    try:
        manifest = json.loads(files.get("plugin.json") or "")
    except json.JSONDecodeError as exc:
        raise PluginDraftError("plugin.json is not JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) - _MANIFEST_KEYS:
        raise PluginDraftError("plugin.json has fields save_plugin never writes")
    if manifest.get("name") != name or manifest.get("version") != version:
        raise PluginDraftError("plugin.json names a different plugin or version")
    if manifest.get("skills", "./skills/") != "./skills/":
        raise PluginDraftError("plugin.json points skills elsewhere")
    if manifest.get("mcpServers", "./mcp.json") != "./mcp.json":
        raise PluginDraftError("plugin.json points MCP servers elsewhere")
    extensions = manifest.get("extensions", {})
    if not isinstance(extensions, dict) or set(extensions) - {SHUFFLE_EXTENSION}:
        raise PluginDraftError("plugin.json has extensions save_plugin never writes")
    shuffle = extensions.get(SHUFFLE_EXTENSION, {})
    if not isinstance(shuffle, dict) or any(
        key != "services" and (key not in _SHUFFLE_KEYS or value != _SHUFFLE_KEYS[key])
        for key, value in shuffle.items()
    ):
        raise PluginDraftError("plugin.json has extension settings save_plugin never writes")
    if "services" in shuffle and check_services(shuffle["services"]) != shuffle["services"]:
        raise PluginDraftError("plugin.json lists services save_plugin would not write")
    for rel, text in files.items():
        _no_secrets(text, rel)
    if "mcp.json" in files:
        try:
            servers = json.loads(files["mcp.json"]).get("mcpServers")
        except (json.JSONDecodeError, AttributeError) as exc:
            raise PluginDraftError("mcp.json is not a server list") from exc
        if not isinstance(servers, dict) or len(servers) > MAX_SERVERS:
            raise PluginDraftError("mcp.json is not a server list")
        checked = {_name(k, "MCP server name"): _server(k, v) for k, v in servers.items()}
        refs = _host_secret_refs(checked) | _host_secret_refs(servers)
        if refs:
            raise PluginDraftError(f"MCP servers may not reference the app's own secrets {sorted(refs)}")


def install_draft_files(
    name: str, version: str, files: dict[str, str], expected_hash: str, *, home: Path | None = None,
) -> Path | None:
    """Write a draft copied from another device as ``<name>/<version>``.

    Returns the new folder, or ``None`` when that version already exists here
    (it is never overwritten). Raises :class:`PluginDraftError` when the draft
    fails :func:`check_draft_files` or its files do not hash to
    ``expected_hash``, the hash the owner's approval will be pinned to.
    """
    check_draft_files(name, version, files)
    base = authored_dir(home) / name
    target = base / version
    if target.exists():
        return None
    base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink() or not base.resolve().is_relative_to(authored_dir(home).resolve()):
        raise PluginDraftError("the authored plugin folder is not a plain folder")
    staging_root = (plugins_home() if home is None else home) / "cache" / ".authored-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{name}-{version}-", dir=staging_root))
    try:
        _write_tree(staging, files)
        if content_hash(staging) != expected_hash:
            raise PluginDraftError("the copied draft does not match its content hash")
        if target.exists():
            return None
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return target

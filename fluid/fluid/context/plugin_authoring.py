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
``${VAR}`` references (no literal secrets on disk), paths outside the plugin
folder, oversized drafts, and rewriting a version that already exists with
different content.
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
        files[f"agents/{aname}.md"] = _frontmatter({"name": aname, "description": adesc}) + body
        components.append(f"agent {aname}")

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
    manifest["extensions"] = {SHUFFLE_EXTENSION: shuffle}
    files["plugin.json"] = json.dumps(manifest, indent=2) + "\n"

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
        key not in _SHUFFLE_KEYS or value != _SHUFFLE_KEYS[key] for key, value in shuffle.items()
    ):
        raise PluginDraftError("plugin.json has extension settings save_plugin never writes")
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

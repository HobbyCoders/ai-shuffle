"""Safe, explicit import previews for Claude and Codex settings.

This module is deliberately not part of normal configuration discovery.  A host
chooses a source home, asks for a preview, and may apply selected ``ready``
items from that exact preview.  It never reads credentials, histories, or
session data, and it never overwrites an existing Fluid/native file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlsplit

try:  # pragma: no cover - Python 3.11+ in supported deployments
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


ImportStatus = Literal[
    "ready", "imported", "skipped", "duplicate", "conflict", "diagnostic"
]
_SECRET_WORDS = ("secret", "auth", "token", "password", "credential", "api_key", "apikey")


@dataclass(frozen=True, kw_only=True)
class SettingsImportItem:
    """One non-destructive import candidate or inventory diagnostic."""

    id: str
    source_system: str
    kind: str
    source_paths: tuple[str, ...]
    destination: str | None
    source_digest: str | None
    status: ImportStatus
    message: str = ""
    is_directory: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)
    # Kept out of ``to_dict``: this is the sanitized MCP document to write.
    _content: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def source(self) -> str | None:
        """Compatibility-friendly singular source for one-path items."""
        return self.source_paths[0] if len(self.source_paths) == 1 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_system": self.source_system,
            "kind": self.kind,
            "source_paths": list(self.source_paths),
            "destination": self.destination,
            "source_digest": self.source_digest,
            "status": self.status,
            "message": self.message,
            "is_directory": self.is_directory,
            "provenance": self.provenance,
        }


@dataclass(frozen=True, kw_only=True)
class SettingsImportPreview:
    """A JSON-serializable plan.  It performs no writes."""

    source_home: str
    destination_home: str
    sources: tuple[str, ...]
    items: tuple[SettingsImportItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_home": self.source_home,
            "destination_home": self.destination_home,
            "sources": list(self.sources),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, kw_only=True)
class SettingsImportResult:
    preview: SettingsImportPreview
    items: tuple[SettingsImportItem, ...]

    def to_dict(self) -> dict[str, Any]:
        data = self.preview.to_dict()
        data["items"] = [item.to_dict() for item in self.items]
        return data


def _home(value: str | Path | None) -> Path:
    return Path(value).expanduser() if value is not None else Path.home()


def _stable_id(
    system: str,
    kind: str,
    paths: Iterable[Path],
    destination: Path | None,
    provenance: dict[str, Any] | None = None,
) -> str:
    identity = json.dumps(provenance or {}, sort_keys=True, separators=(",", ":"))
    key = "\0".join((system, kind, *(str(p) for p in paths), str(destination or ""), identity))
    return f"{system}-{kind}-{hashlib.sha256(key.encode()).hexdigest()[:16]}"


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def _is_symlink(path: Path) -> bool:
    info = _lstat(path)
    return info is not None and stat.S_ISLNK(info.st_mode)


def _file_digest(path: Path) -> str | None:
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _normalized_text_digest(path: Path) -> str | None:
    if _path_has_symlink(path):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).rstrip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def _tree_digest(path: Path) -> str | None:
    """Digest a directory without following symlinks (and reject any found)."""
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        return None
    digest = hashlib.sha256()
    try:
        for root, dirs, files in os.walk(path, followlinks=False):
            root_path = Path(root)
            dirs.sort()
            files.sort()
            for name in [*dirs, *files]:
                child = root_path / name
                child_info = child.lstat()
                if stat.S_ISLNK(child_info.st_mode):
                    return None
                relative = child.relative_to(path).as_posix().encode()
                if stat.S_ISDIR(child_info.st_mode):
                    digest.update(b"D\0" + relative + b"\0")
                elif stat.S_ISREG(child_info.st_mode):
                    file_hash = _file_digest(child)
                    if file_hash is None:
                        return None
                    digest.update(b"F\0" + relative + b"\0" + file_hash.encode() + b"\0")
                else:
                    return None
    except OSError:
        return None
    return digest.hexdigest()


def _path_has_symlink(path: Path) -> bool:
    """Reject a final symlink and symlinked ancestors, which can escape a root."""
    candidate = path
    while True:
        if _is_symlink(candidate):
            return True
        parent = candidate.parent
        if parent == candidate:
            return False
        candidate = parent


def _digest(path: Path, is_directory: bool) -> str | None:
    if _path_has_symlink(path):
        return None
    return _tree_digest(path) if is_directory else _file_digest(path)


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if _path_has_symlink(path):
        return None, "source is a symlink; rejected"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"could not parse JSON: {exc}"
    return (raw, None) if isinstance(raw, dict) else (None, "JSON root must be an object")


def _read_toml(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if tomllib is None:
        return None, "TOML parsing is unavailable"
    if _path_has_symlink(path):
        return None, "source is a symlink; rejected"
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"could not parse TOML: {exc}"
    return (raw, None) if isinstance(raw, dict) else (None, "TOML root must be a table")


def _item(
    system: str, kind: str, paths: Iterable[Path], destination: Path | None,
    *, status: ImportStatus = "ready", message: str = "", is_directory: bool = False,
    provenance: dict[str, Any] | None = None, content: bytes | None = None,
) -> SettingsImportItem:
    path_tuple = tuple(paths)
    source_digest = hashlib.sha256(content).hexdigest() if content is not None else (
        _digest(path_tuple[0], is_directory) if len(path_tuple) == 1 else None
    )
    if status == "ready" and source_digest is None:
        status, message = "skipped", "source is unreadable, non-regular, or contains a symlink; rejected"
    source_digests = {
        str(path): (_digest(path, is_directory) if len(path_tuple) == 1 else _digest(path, path.is_dir()))
        for path in path_tuple
    }
    clean_provenance = dict(provenance or {})
    clean_provenance.setdefault("source_digests", source_digests)
    return SettingsImportItem(
        id=_stable_id(system, kind, path_tuple, destination, provenance), source_system=system, kind=kind,
        source_paths=tuple(str(p) for p in path_tuple), destination=str(destination) if destination else None,
        source_digest=source_digest, status=status, message=message, is_directory=is_directory,
        provenance=clean_provenance, _content=content,
    )


def _diagnostic(system: Literal["claude", "codex"], kind: str, path: Path, message: str,
                *, is_directory: bool = False) -> SettingsImportItem:
    provenance = {"source_digests": {}}
    return SettingsImportItem(
        id=_stable_id(system, kind, (path,), None),
        source_system=system,
        kind=kind,
        source_paths=(str(path),),
        destination=None,
        source_digest=None,
        status="diagnostic",
        message=message,
        is_directory=is_directory,
        provenance=provenance,
    )


def _add_file(items: list[SettingsImportItem], system: Literal["claude", "codex"], kind: str,
              source: Path, destination: Path) -> None:
    if source.exists() or _is_symlink(source):
        provenance = None
        if "instruction" in kind:
            provenance = {"normalized_text_digest": _normalized_text_digest(source)}
        items.append(_item(system, kind, (source,), destination, provenance=provenance))


def _add_skills(items: list[SettingsImportItem], system: Literal["claude", "codex"], root: Path,
                destination: Path, kind: str, *, native: bool = False) -> None:
    if not root.exists() and not _is_symlink(root):
        return
    if _is_symlink(root) or not root.is_dir():
        items.append(_item(system, kind, (root,), destination, is_directory=True))
        return
    for skill in sorted(root.glob("*/SKILL.md")):
        source = skill.parent
        if native:
            items.append(_item(system, kind, (source,), source, status="duplicate",
                               message="already a Fluid-native project skill; not copied", is_directory=True))
        else:
            items.append(_item(system, kind, (source,), destination / source.name, is_directory=True))


def _add_agents(items: list[SettingsImportItem], system: Literal["claude", "codex"], root: Path,
                destination: Path, kind: str, pattern: str = "*.md") -> None:
    if not root.exists() and not _is_symlink(root):
        return
    if _is_symlink(root) or not root.is_dir():
        items.append(_item(system, kind, (root,), destination, is_directory=True))
        return
    for source in sorted(root.rglob(pattern)):
        _add_file(items, system, kind, source, destination / source.relative_to(root))


def _instruction_destination(project: Path, source: Path, dot_claude: bool = False) -> Path:
    name = source.name
    native_name = "AGENTS" + name[len("CLAUDE"):]
    if name == "CLAUDE.local.md":
        native_name = "AGENTS.override.md"
    return (project / ".agents" / native_name) if dot_claude else project / native_name


def _add_claude_project(items: list[SettingsImportItem], project: Path, system_home: Path,
                        mcp_inputs: dict[Path, list[tuple[Path, dict[str, Any]]]]) -> None:
    for source in sorted(project.glob("CLAUDE*.md")):
        _add_file(items, "claude", "project_instruction", source, _instruction_destination(project, source))
    dot = project / ".claude"
    for source in sorted(dot.glob("CLAUDE*.md")) if dot.is_dir() else []:
        _add_file(items, "claude", "project_instruction", source, _instruction_destination(project, source, True))
    _add_skills(items, "claude", dot / "skills", project / ".agents" / "skills", "project_skill")
    _add_agents(items, "claude", dot / "agents", project / ".agents" / "agents", "project_agent")
    for setting in (dot / "settings.json", dot / "settings.local.json"):
        if setting.exists() or _is_symlink(setting):
            items.append(_diagnostic("claude", "project_settings", setting,
                                     "inventoried only; Fluid has no safe native translation"))
    mcp_file = project / ".mcp.json"
    if mcp_file.exists() or _is_symlink(mcp_file):
        raw, error = _read_json(mcp_file)
        if error:
            items.append(_diagnostic("claude", "project_mcp", mcp_file, error))
        else:
            servers = raw.get("mcpServers")
            if isinstance(servers, dict):
                mcp_inputs.setdefault(project, []).append((mcp_file, servers))
            else:
                items.append(_diagnostic("claude", "project_mcp", mcp_file, "mcpServers must be an object"))


def _project_registration(
    system: Literal["claude", "codex"], registry: Path, project: Path
) -> SettingsImportItem:
    item = _item(
        system,
        "project_registration",
        (registry,),
        None,
        message="configured project available for host registration; Fluid will not change a database",
        provenance={"project_path": str(project)},
    )
    suffix = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:12]
    return replace(item, id=f"{item.id}-{suffix}")


def _claude(items: list[SettingsImportItem], home: Path, fluid: Path,
            mcp_inputs: dict[Path | None, list[tuple[Path, dict[str, Any]]]]) -> None:
    root = home / ".claude"
    _add_file(items, "claude", "global_instruction", root / "CLAUDE.md", fluid / "AGENTS.md")
    _add_skills(items, "claude", root / "skills", fluid / "skills", "global_skill")
    _add_agents(items, "claude", root / "agents", fluid / "agents", "global_agent")
    setting = root / "settings.json"
    if setting.exists() or _is_symlink(setting):
        items.append(_diagnostic("claude", "settings", setting,
                                 "inventoried only; Fluid has no safe native translation"))
    plugins = root / "plugins"
    if plugins.exists() or _is_symlink(plugins):
        items.append(_diagnostic(
            "claude",
            "plugins",
            plugins,
            "inventoried only; Fluid does not activate Claude plugins",
            is_directory=True,
        ))
    config_path = home / ".claude.json"
    persisted_config = root / ".claude.json"
    if _is_symlink(config_path):
        try:
            managed_link = config_path.resolve(strict=False) == persisted_config.resolve(strict=False)
        except OSError:
            managed_link = False
        if managed_link and persisted_config.is_file() and not _path_has_symlink(persisted_config):
            config_path = persisted_config
    elif not config_path.exists() and persisted_config.is_file():
        # Older AI Shuffle containers persisted the root registry inside the
        # ~/.claude volume even when the ephemeral home link is absent.
        config_path = persisted_config
    if not (config_path.exists() or _is_symlink(config_path)):
        return
    raw, error = _read_json(config_path)
    if error:
        items.append(_diagnostic("claude", "configuration", config_path, error))
        return
    servers = raw.get("mcpServers")
    if isinstance(servers, dict):
        mcp_inputs.setdefault(None, []).append((config_path, servers))
    elif servers is not None:
        items.append(_diagnostic("claude", "mcp", config_path, "mcpServers must be an object"))
    projects = raw.get("projects")
    if projects is None:
        return
    if not isinstance(projects, dict):
        items.append(_diagnostic("claude", "configuration", config_path, "projects must be an object"))
        return
    for value, record in sorted(projects.items(), key=lambda pair: str(pair[0])):
        project = Path(str(value)).expanduser()
        if not project.is_dir() or _is_symlink(project):
            items.append(_diagnostic("claude", "project", project, "configured project is missing or a symlink"))
            continue
        items.append(_project_registration("claude", config_path, project))
        project_inputs: dict[Path, list[tuple[Path, dict[str, Any]]]] = {}
        _add_claude_project(items, project, home, project_inputs)
        if isinstance(record, dict) and isinstance(record.get("mcpServers"), dict):
            project_inputs.setdefault(project, []).append((config_path, record["mcpServers"]))
        for key, values in project_inputs.items():
            mcp_inputs.setdefault(key, []).extend(values)


def _codex_agent_item(
    items: list[SettingsImportItem], source: Path, destination_root: Path
) -> None:
    raw, error = _read_toml(source)
    if error:
        items.append(_diagnostic("codex", "global_agent", source, error))
        return
    assert raw is not None
    name = source.stem
    description = raw.get("description", "")
    body = raw.get("developer_instructions", raw.get("instructions", raw.get("prompt", "")))
    if not isinstance(description, str) or not isinstance(body, str):
        items.append(_diagnostic(
            "codex",
            "global_agent",
            source,
            "agent description and instructions must be strings",
        ))
        return
    fields = [f"name: {name}", f"description: {json.dumps(description)}"]
    model = raw.get("model")
    if isinstance(model, str) and model:
        fields.append(f"model: {model}")
    content = ("---\n" + "\n".join(fields) + "\n---\n\n" + body.strip() + "\n").encode()
    items.append(_item(
        "codex",
        "global_agent",
        (source,),
        destination_root / f"{name}.md",
        content=content,
        provenance={"translation": "codex TOML to Fluid Markdown agent"},
    ))


def _add_codex_agents(
    items: list[SettingsImportItem], root: Path, destination: Path
) -> None:
    if not root.exists() and not _is_symlink(root):
        return
    if _is_symlink(root) or not root.is_dir():
        items.append(_item(
            "codex", "global_agent", (root,), destination, is_directory=True
        ))
        return
    for source in sorted(root.rglob("*.toml")):
        _codex_agent_item(items, source, destination)


def _agent_references(value: Any, key: str = "", in_agent_section: bool = False) -> set[str]:
    """Find TOML agent paths without treating arbitrary config paths as agents."""
    found: set[str] = set()
    active = in_agent_section or "agent" in key.lower()
    if isinstance(value, dict):
        for child_key, child in value.items():
            found.update(_agent_references(child, str(child_key), active))
    elif isinstance(value, list):
        for child in value:
            found.update(_agent_references(child, key, active))
    elif isinstance(value, str) and active and value.endswith(".toml"):
        found.add(value)
    return found


def _codex(items: list[SettingsImportItem], home: Path, fluid: Path,
           mcp_inputs: dict[Path | None, list[tuple[Path, dict[str, Any]]]]) -> None:
    codex, native = home / ".codex", home / ".agents"
    instruction = codex / "AGENTS.override.md"
    if not (instruction.exists() or _is_symlink(instruction)):
        instruction = codex / "AGENTS.md"
    _add_file(items, "codex", "global_instruction", instruction, fluid / "AGENTS.md")
    _add_skills(items, "codex", native / "skills", fluid / "skills", "global_skill")
    _add_skills(items, "codex", codex / "skills", fluid / "skills", "legacy_global_skill")
    _add_codex_agents(items, codex / "agents", fluid / "agents")
    config = codex / "config.toml"
    if not (config.exists() or _is_symlink(config)):
        return
    raw, error = _read_toml(config)
    if error:
        items.append(_diagnostic("codex", "configuration", config, error))
        return
    items.append(_diagnostic("codex", "configuration", config,
                             "inventoried for MCP, agent references, and projects; other settings are not imported"))
    for reference in sorted(_agent_references(raw)):
        source = (codex / reference).resolve(strict=False)
        # resolve is only used to construct a path; containment prevents config escapes.
        try:
            source.relative_to(codex.resolve())
        except ValueError:
            items.append(_diagnostic("codex", "agent", codex / reference, "agent reference escapes .codex; rejected"))
            continue
        _codex_agent_item(items, source, fluid / "agents")
    servers = raw.get("mcp_servers")
    if isinstance(servers, dict):
        mcp_inputs.setdefault(None, []).append((config, servers))
    elif servers is not None:
        items.append(_diagnostic("codex", "mcp", config, "mcp_servers must be a table"))
    projects = raw.get("projects")
    if projects is None:
        return
    if not isinstance(projects, dict):
        items.append(_diagnostic("codex", "configuration", config, "projects must be a table"))
        return
    for project_name, record in sorted(projects.items(), key=lambda pair: str(pair[0])):
        project = Path(str(project_name)).expanduser()
        if not project.is_dir() or _is_symlink(project):
            items.append(_diagnostic("codex", "project", project, "configured project is missing or a symlink"))
            continue
        items.append(_project_registration("codex", config, project))
        for name in ("AGENTS.md", "AGENTS.override.md"):
            source = project / name
            if source.exists() or _is_symlink(source):
                items.append(_item("codex", "project_native_instruction", (source,), source,
                                   status="duplicate", message="already a Fluid-native project instruction; not copied"))
        _add_skills(items, "codex", project / ".agents" / "skills", project / ".agents" / "skills",
                    "project_native_skill", native=True)
        if isinstance(record, dict):
            project_servers = record.get("mcp_servers", record.get("mcpServers"))
            if isinstance(project_servers, dict):
                mcp_inputs.setdefault(project, []).append((config, project_servers))
            elif project_servers is not None:
                items.append(_diagnostic("codex", "project_mcp", config, "project MCP servers must be a table"))


def _has_secret_key(value: str) -> bool:
    lower = value.lower().replace("-", "_")
    return any(word in lower for word in _SECRET_WORDS)


def _contains_embedded_secret(value: str) -> bool:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return True
    if any(_has_secret_key(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        return True
    return bool(
        re.search(
            r"(?i)(?:^|[^a-z])(?:api[-_]?key|authorization|credential|password|secret|token)"
            r"(?:\s|[-_])*[:=]",
            value,
        )
    )


def _sanitize_server(name: str, raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(name, str) or not name or not isinstance(raw, dict):
        return None, "server name and definition must be non-empty string/object"
    command, url = raw.get("command"), raw.get("url")
    if not isinstance(command, str) and not isinstance(url, str):
        return None, "server requires a string command or url"
    if isinstance(command, str) and isinstance(url, str):
        return None, "server cannot specify both command and url"
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return None, "server args must be a list of strings"
    values = [value for value in (command, url, *args) if isinstance(value, str)]
    secret_flag = any(
        arg.startswith("-") and _has_secret_key(arg.lstrip("-").split("=", 1)[0])
        for arg in args
    )
    if secret_flag or any(_contains_embedded_secret(value) for value in values):
        return None, "server contains credential-bearing command, URL, or argument values"
    clean: dict[str, Any] = {"command": command} if isinstance(command, str) else {"url": url}
    if args:
        clean["args"] = args
    if isinstance(raw.get("type"), str):
        clean["type"] = raw["type"]
    # env and secret-shaped fields are deliberately not represented, even in diagnostics.
    unsupported = [key for key in raw if key not in {"command", "url", "args", "type", "env"} and not _has_secret_key(str(key))]
    if unsupported:
        return None, "server contains unsupported fields"
    return clean, "environment and secret fields omitted" if ("env" in raw or any(_has_secret_key(str(k)) for k in raw)) else None


def _mcp_items(items: list[SettingsImportItem], inputs: dict[Path | None, list[tuple[Path, dict[str, Any]]]],
               home: Path) -> None:
    for project, groups in sorted(inputs.items(), key=lambda pair: str(pair[0])):
        servers: dict[str, dict[str, Any]] = {}
        paths: list[Path] = []
        notes: list[str] = []
        conflict_names: set[str] = set()
        for path, supplied in groups:
            paths.append(path)
            for name, raw in sorted(supplied.items()):
                clean, note = _sanitize_server(str(name), raw)
                if clean is None:
                    items.append(_diagnostic("claude" if path.name.endswith("json") else "codex", "mcp_server", path,
                                             f"MCP server {name!r} rejected: {note}"))
                    continue
                if note:
                    notes.append(f"{name}: {note}")
                previous = servers.get(str(name))
                if previous is not None and previous != clean:
                    conflict_names.add(str(name))
                else:
                    servers[str(name)] = clean
        destination = (home / ".fluid" / "mcp.json") if project is None else project / ".fluid" / "mcp.json"
        if not servers and not conflict_names:
            continue
        # A collision among sources is never silently resolved.  Valid unrelated names remain importable.
        for name in conflict_names:
            servers.pop(name, None)
        payload = json.dumps({"mcpServers": servers}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        source_systems = {"claude" if path.suffix == ".json" else "codex" for path in paths}
        system = next(iter(source_systems)) if len(source_systems) == 1 else "combined"
        item = _item(system, "project_mcp" if project else "global_mcp", tuple(paths), destination,
                     content=payload, provenance={"scope": str(project) if project else "global"})
        if conflict_names:
            item = replace(item, status="conflict", message="MCP server-name collision between sources: " + ", ".join(sorted(conflict_names)))
        elif notes:
            item = replace(item, message="; ".join(sorted(set(notes))))
        items.append(item)


def _comparison_digest(item: SettingsImportItem) -> str | None:
    normalized = item.provenance.get("normalized_text_digest")
    return normalized if isinstance(normalized, str) and normalized else item.source_digest


def _instruction_sidecar(item: SettingsImportItem, destination: Path) -> Path | None:
    if "instruction" not in item.kind or not item.source_digest:
        return None
    name = f"{destination.stem}-{item.source_digest[:12]}{destination.suffix or '.md'}"
    if destination.parent.name == ".fluid":
        return destination.parent / "imports" / item.source_system / name
    if destination.parent.name == ".agents":
        return destination.parent / "imports" / item.source_system / name
    return destination.parent / ".agents" / "imports" / item.source_system / name


def _as_review_sidecar(item: SettingsImportItem, destination: Path) -> SettingsImportItem:
    sidecar = _instruction_sidecar(item, destination)
    if sidecar is None:
        return replace(item, status="conflict", message="native destination already exists; it wins")
    if _path_has_symlink(sidecar):
        return replace(item, status="skipped", destination=str(sidecar), message="review sidecar path has a symlink ancestor; rejected")
    if sidecar.exists():
        existing = _normalized_text_digest(sidecar)
        if existing == _comparison_digest(item):
            return replace(item, status="duplicate", destination=str(sidecar), message="exact review sidecar already exists")
        return replace(item, status="conflict", destination=str(sidecar), message="review sidecar destination already exists; no overwrite")
    return replace(
        item,
        destination=str(sidecar),
        status="ready",
        message="native instructions win; import will remain an inert provenance sidecar until reviewed",
        provenance={**item.provenance, "proposed_native_destination": str(destination), "activation": "review_required"},
    )


def _resolve_destinations(items: list[SettingsImportItem]) -> list[SettingsImportItem]:
    """Apply destination/native and source-to-source collision rules."""
    result = list(items)
    indexed: dict[str, list[int]] = {}
    for index, item in enumerate(result):
        if item.status == "ready" and item.destination:
            indexed.setdefault(item.destination, []).append(index)
    for destination, indices in indexed.items():
        dest = Path(destination)
        if _path_has_symlink(dest):
            for index in indices:
                result[index] = replace(result[index], status="skipped",
                                        message="destination path has a symlink ancestor; rejected")
            continue
        if dest.exists() or _is_symlink(dest):
            first_item = result[indices[0]]
            existing = (
                _normalized_text_digest(dest)
                if "instruction" in first_item.kind
                else _digest(dest, first_item.is_directory)
            )
            for index in indices:
                item = result[index]
                if existing is not None and existing == _comparison_digest(item):
                    result[index] = replace(item, status="duplicate", message="exact native destination already exists; not copied")
                else:
                    result[index] = _as_review_sidecar(item, dest)
            continue
        by_digest: dict[str | None, list[int]] = {}
        for index in indices:
            by_digest.setdefault(_comparison_digest(result[index]), []).append(index)
        if len(by_digest) == 1:
            first = indices[0]
            for index in indices[1:]:
                result[index] = replace(result[index], status="duplicate",
                                        message=f"exact duplicate of {result[first].id}; one copy will be used")
        else:
            for index in indices:
                result[index] = _as_review_sidecar(result[index], dest)
    return result


def preview_settings_import(*, sources: Iterable[str] = ("codex", "claude"),
                            source_home: str | Path | None = None,
                            destination_home: str | Path | None = None) -> SettingsImportPreview:
    """Preview selected Claude/Codex imports without changing either home.

    ``source_home`` and ``destination_home`` are independent, so callers can
    import from mounted Linux/macOS/container homes (including paths with spaces).
    """
    raw_sources = (sources,) if isinstance(sources, str) else sources
    selected = tuple(dict.fromkeys(str(source).lower() for source in raw_sources))
    invalid = set(selected) - {"claude", "codex"}
    if invalid:
        raise ValueError(f"unsupported settings source(s): {', '.join(sorted(invalid))}")
    source, destination = _home(source_home), _home(destination_home)
    fluid = destination / ".fluid"
    items: list[SettingsImportItem] = []
    mcp_inputs: dict[Path | None, list[tuple[Path, dict[str, Any]]]] = {}
    if "claude" in selected:
        _claude(items, source, fluid, mcp_inputs)
    if "codex" in selected:
        _codex(items, source, fluid, mcp_inputs)
    _mcp_items(items, mcp_inputs, destination)
    return SettingsImportPreview(source_home=str(source), destination_home=str(destination), sources=selected,
                                 items=tuple(_resolve_destinations(items)))


def _source_unchanged(item: SettingsImportItem) -> bool:
    expected = item.provenance.get("source_digests", {})
    if not isinstance(expected, dict):
        return False
    for raw_path, digest in expected.items():
        path = Path(str(raw_path))
        current = _digest(path, item.is_directory if len(item.source_paths) == 1 else path.is_dir())
        if current != digest:
            return False
    return True


def _write_exclusive(destination: Path, data: bytes) -> None:
    destination = destination.absolute()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination.anchor, directory_flags)
    try:
        for part in destination.parent.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(destination.name, file_flags, 0o600, dir_fd=descriptor)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(data)
        except BaseException:
            try:
                os.unlink(destination.name, dir_fd=descriptor)
            except OSError:
                pass
            raise
    finally:
        os.close(descriptor)


def _provenance(destination_home: Path, item: SettingsImportItem) -> None:
    directory = destination_home / ".fluid" / "provenance"
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "item_id": item.id, "source_system": item.source_system, "source_paths": list(item.source_paths),
        "destination": item.destination, "source_digest": item.source_digest, "provenance": item.provenance,
    }, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    for suffix in range(1000):
        path = directory / f"{item.id}{'' if suffix == 0 else '-' + str(suffix)}.json"
        try:
            _write_exclusive(path, payload)
            return
        except FileExistsError:
            continue
    raise OSError("could not allocate a unique provenance sidecar")


def apply_settings_import(preview: SettingsImportPreview, *, selected_ids: Iterable[str] | None = None) -> SettingsImportResult:
    """Apply selected ready entries with digest/state rechecks and no-overwrite writes."""
    selected = set(selected_ids) if selected_ids is not None else {item.id for item in preview.items if item.status == "ready"}
    results: list[SettingsImportItem] = []
    destination_home = Path(preview.destination_home)
    for item in preview.items:
        if item.id not in selected or item.status != "ready":
            results.append(item)
            continue
        if item.destination is None or not _source_unchanged(item):
            results.append(replace(item, status="skipped", message="source changed, disappeared, or became unsafe after preview"))
            continue
        destination = Path(item.destination)
        if _path_has_symlink(destination):
            results.append(replace(item, status="skipped", message="destination path has a symlink ancestor; rejected"))
            continue
        if destination.exists() or _is_symlink(destination):
            results.append(replace(item, status="conflict", message="destination appeared after preview; native file wins"))
            continue
        try:
            if item._content is not None:
                _write_exclusive(destination, item._content)
            elif item.is_directory:
                source = Path(item.source_paths[0])
                # copytree creates the target itself and refuses an existing target.
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source, destination, symlinks=True)
                if _tree_digest(destination) is None:
                    raise OSError("copied tree contains a symlink; removed")
            else:
                source = Path(item.source_paths[0])
                if _is_symlink(source):
                    raise OSError("source became a symlink")
                data = source.read_bytes()
                if hashlib.sha256(data).hexdigest() != item.source_digest:
                    raise OSError("source changed while being copied")
                _write_exclusive(destination, data)
            _provenance(destination_home, item)
        except FileExistsError:
            results.append(replace(item, status="conflict", message="destination appeared during apply; no overwrite"))
        except OSError as exc:
            # Roll back a target created by this item when copy validation or
            # provenance recording fails. FileExistsError is handled above and
            # never removes the competing native path.
            if destination.exists():
                if item.is_directory:
                    shutil.rmtree(destination, ignore_errors=True)
                else:
                    try:
                        destination.unlink()
                    except OSError:
                        pass
            results.append(replace(item, status="skipped", message=f"copy failed without overwrite: {exc}"))
        else:
            results.append(replace(item, status="imported", message="copied without modifying source"))
    return SettingsImportResult(preview=preview, items=tuple(results))


def import_settings(*, apply: bool = False, selected_ids: Iterable[str] | None = None, **kwargs: Any) -> SettingsImportPreview | SettingsImportResult:
    """Convenience API: preview by default, or preview and apply selected IDs."""
    preview = preview_settings_import(**kwargs)
    return apply_settings_import(preview, selected_ids=selected_ids) if apply else preview

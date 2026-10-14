"""Check a plugin folder against AI Shuffle's plugin format.

This is the validator an agent runs before it installs a plugin it wrote. It
reads a folder in the native format (see ``docs/content/features/plugins.md``,
"Plugin format"), lists every problem it finds in plain words, and, when there
are none, turns the folder into the spec :func:`save_authored_plugin` installs
— so a plugin that passes here installs exactly as checked.

Run it from a shell::

    python -m fluid.context.plugin_check path/to/my-plugin

or call :func:`check_plugin_folder`. Exit status 0 means valid, 1 means
problems were found.

The native layout::

    my-plugin/
      plugin.json                 required: name, version, description
      skills/<name>/SKILL.md      frontmatter: name (= folder), description
      commands/<name>.md          frontmatter: description (optional)
      agents/<name>.md            frontmatter: name (= file), description, tools (optional)
      mcp.json                    {"mcpServers": {...}} (optional)
      README.md, AGENTS.md, CLAUDE.md  optional draft guidance, not installed

Nothing else may be in the folder: no hooks, scripts or binaries, because a
plugin installed from a chat can only carry what the owner can review in
Settings.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fluid.context.instructions import parse_frontmatter
from fluid.context.plugin_authoring import (
    MAX_TOTAL_BYTES,
    PluginDraftError,
    build_files,
    secret_findings,
)
from fluid.context.plugin_formats import SHUFFLE_EXTENSION

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_VERSION_RE = re.compile(r"^\d{1,6}\.\d{1,6}\.\d{1,6}$")
_MANIFEST_KEYS = {"name", "version", "description", "author", "skills", "mcpServers", "extensions"}
_SHUFFLE_KEYS = {"commands", "agents", "services", "authored"}
_SKILL_FIELDS = {"name", "description"}
_COMMAND_FIELDS = {"description"}
_AGENT_FIELDS = {"name", "description", "tools"}
_PIN_HINT = {
    "model": "remove it; the model is chosen per chat",
    "effort": "remove it; effort is chosen per chat",
    "allowed-tools": "remove it; tools come from the chat's permissions",
    "permissionMode": "remove it; permissions come from the chat",
    "hooks": "remove it; plugins built in a chat cannot carry hooks",
}
MAX_FILE_BYTES = 64 * 1024


@dataclass
class CheckResult:
    """Problems found in one folder, and the installable spec when there are none."""

    path: Path
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    spec: dict[str, Any] | None = None
    components: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        """The plain-text report an agent reads."""
        lines: list[str] = []
        if self.ok and self.spec is not None:
            lines.append(
                f"OK: {self.spec['name']} {self.spec.get('version', '0.1.0')} is a valid plugin "
                f"({', '.join(self.components)})."
            )
        else:
            lines.append(f"{len(self.errors)} problem(s) in {self.path}:")
            lines.extend(f"- {e}" for e in self.errors)
        if self.warnings:
            lines.append("Notes:")
            lines.extend(f"- {w}" for w in self.warnings)
        return "\n".join(lines)


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_text(root: Path, path: Path, result: CheckResult) -> str | None:
    rel = _rel(root, path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        result.errors.append(f"{rel}: cannot be read ({type(exc).__name__})")
        return None
    if len(data) > MAX_FILE_BYTES:
        result.errors.append(f"{rel}: larger than {MAX_FILE_BYTES // 1024} KB")
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result.errors.append(f"{rel}: is not UTF-8 text")
        return None
    found = secret_findings(text)
    if found:
        result.errors.append(
            f"{rel}: looks like it contains {found[0]}. A plugin never holds a credential; "
            "remove it and ask for access through the private access card (RequestChatAccess)"
        )
    return text


def _frontmatter_fields(
    root: Path, path: Path, text: str, allowed: set[str], result: CheckResult
) -> tuple[dict[str, Any], str]:
    fields, body = parse_frontmatter(text)
    rel = _rel(root, path)
    for key in sorted(set(fields) - allowed):
        hint = _PIN_HINT.get(key, f"only {', '.join(sorted(allowed))} are read here")
        result.errors.append(f"{rel}: frontmatter field '{key}' is not allowed; {hint}")
    if not body.strip():
        result.errors.append(f"{rel}: the body is empty")
    return fields, body


def _check_name(value: Any, what: str, result: CheckResult) -> bool:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        result.errors.append(
            f"{what} must be lowercase letters, digits and hyphens (1-63 characters), got {value!r}"
        )
        return False
    return True


def check_plugin_folder(path: str | Path) -> CheckResult:
    """Check ``path`` and return every problem found. Never raises."""
    root = Path(path).expanduser()
    result = CheckResult(path=root)
    try:
        root = root.resolve()
        result.path = root
    except OSError:
        pass
    if not root.is_dir():
        result.errors.append(f"{root} is not a folder")
        return result

    for foreign in (".claude-plugin", ".codex-plugin"):
        if (root / foreign).exists():
            result.errors.append(
                f"{foreign}/ is another tool's manifest folder. A plugin built here keeps its "
                "manifest as plugin.json at the folder root"
            )

    files: list[Path] = []
    total = 0
    for item in sorted(root.rglob("*")):
        rel = _rel(root, item)
        if item.is_symlink():
            result.errors.append(f"{rel}: symbolic links are not allowed")
            continue
        if item.is_dir():
            continue
        files.append(item)
        try:
            total += item.stat().st_size
        except OSError:
            pass
    if total > MAX_TOTAL_BYTES:
        result.errors.append(f"the folder holds {total} bytes; the limit is {MAX_TOTAL_BYTES}")

    skills: list[dict[str, Any]] = []
    commands: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    manifest: dict[str, Any] | None = None
    servers: dict[str, Any] = {}

    for item in files:
        rel = _rel(root, item)
        parts = rel.split("/")
        if rel == "plugin.json":
            text = _read_text(root, item, result)
            if text is None:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                result.errors.append(f"plugin.json: not valid JSON ({exc.msg} at line {exc.lineno})")
                continue
            if not isinstance(data, dict):
                result.errors.append("plugin.json: must be a JSON object")
                continue
            manifest = data
        elif rel == "mcp.json":
            text = _read_text(root, item, result)
            if text is None:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                result.errors.append(f"mcp.json: not valid JSON ({exc.msg} at line {exc.lineno})")
                continue
            block = data.get("mcpServers") if isinstance(data, dict) else None
            if not isinstance(block, dict):
                result.errors.append('mcp.json: must be {"mcpServers": {"<name>": {...}}}')
                continue
            servers = block
        elif rel in {"README.md", "AGENTS.md", "CLAUDE.md"}:
            _read_text(root, item, result)
            result.warnings.append(
                f"{rel} is not installed; the description in plugin.json is shown instead"
            )
        elif len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            text = _read_text(root, item, result)
            if text is None:
                continue
            fields, body = _frontmatter_fields(root, item, text, _SKILL_FIELDS, result)
            if _check_name(parts[1], f"{rel}: the skill folder name", result):
                if fields.get("name") != parts[1]:
                    result.errors.append(f"{rel}: frontmatter 'name' must be '{parts[1]}' (the folder name)")
            if not str(fields.get("description") or "").strip():
                result.errors.append(f"{rel}: frontmatter 'description' is required (say when to use the skill)")
            skills.append({"name": parts[1], "description": fields.get("description", ""), "body": body})
        elif len(parts) == 2 and parts[0] == "commands" and parts[1].endswith(".md"):
            text = _read_text(root, item, result)
            if text is None:
                continue
            fields, body = _frontmatter_fields(root, item, text, _COMMAND_FIELDS, result)
            name = parts[1][:-3]
            _check_name(name, f"{rel}: the command file name", result)
            entry: dict[str, Any] = {"name": name, "body": body}
            if fields.get("description"):
                entry["description"] = fields["description"]
            commands.append(entry)
        elif len(parts) == 2 and parts[0] == "agents" and parts[1].endswith(".md"):
            text = _read_text(root, item, result)
            if text is None:
                continue
            fields, body = _frontmatter_fields(root, item, text, _AGENT_FIELDS, result)
            name = parts[1][:-3]
            if _check_name(name, f"{rel}: the agent file name", result):
                if fields.get("name") != name:
                    result.errors.append(f"{rel}: frontmatter 'name' must be '{name}' (the file name)")
            if not str(fields.get("description") or "").strip():
                result.errors.append(f"{rel}: frontmatter 'description' is required (say when to use the agent)")
            entry = {"name": name, "description": fields.get("description", ""), "body": body}
            if fields.get("tools"):
                entry["tools"] = fields["tools"] if isinstance(fields["tools"], list) else str(fields["tools"])
            agents.append(entry)
        elif parts[0] == "hooks" or rel.endswith("hooks.json"):
            result.errors.append(
                f"{rel}: plugins built in a chat cannot carry hooks (they run automatically); "
                "the owner adds hooks in Settings > Hooks"
            )
        else:
            result.errors.append(
                f"{rel}: not part of the plugin format. Allowed: plugin.json, mcp.json, README.md, "
                "skills/<name>/SKILL.md, commands/<name>.md, agents/<name>.md"
            )

    if manifest is None:
        if not (root / "plugin.json").is_file():
            result.errors.append("plugin.json is missing at the folder root")
        return result

    for key in sorted(set(manifest) - _MANIFEST_KEYS):
        hint = "remove it; hooks cannot be installed from a chat" if key == "hooks" else "remove it"
        result.errors.append(f"plugin.json: field '{key}' is not part of the format; {hint}")
    _check_name(manifest.get("name"), "plugin.json: 'name'", result)
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        result.errors.append(f"plugin.json: 'version' must look like 1.2.3, got {version!r}")
    if not str(manifest.get("description") or "").strip():
        result.errors.append("plugin.json: 'description' is required (one sentence: what the plugin is for)")
    if "skills" in manifest and manifest["skills"] != "./skills/":
        result.errors.append("plugin.json: 'skills' must be \"./skills/\" (or left out)")
    if "mcpServers" in manifest and manifest["mcpServers"] != "./mcp.json":
        result.errors.append("plugin.json: 'mcpServers' must be \"./mcp.json\" (or left out)")
    if servers and "mcpServers" not in manifest:
        result.warnings.append("mcp.json is read even though plugin.json does not name it")
    extensions = manifest.get("extensions", {})
    shuffle: dict[str, Any] = {}
    if not isinstance(extensions, dict) or set(extensions) - {SHUFFLE_EXTENSION}:
        result.errors.append(f"plugin.json: 'extensions' may only hold \"{SHUFFLE_EXTENSION}\"")
    else:
        shuffle = extensions.get(SHUFFLE_EXTENSION, {})
        if not isinstance(shuffle, dict):
            result.errors.append(f"plugin.json: extensions.\"{SHUFFLE_EXTENSION}\" must be an object")
            shuffle = {}
        for key in sorted(set(shuffle) - _SHUFFLE_KEYS):
            result.errors.append(
                f"plugin.json: extensions.\"{SHUFFLE_EXTENSION}\".{key} is not part of the format"
            )
        if shuffle.get("commands", "./commands/") != "./commands/":
            result.errors.append('plugin.json: commands must be "./commands/"')
        if shuffle.get("agents", "./agents/") != "./agents/":
            result.errors.append('plugin.json: agents must be "./agents/"')

    if result.errors:
        return result

    spec: dict[str, Any] = {
        "name": manifest["name"],
        "version": manifest["version"],
        "description": manifest.get("description", ""),
    }
    for key, value in (("skills", skills), ("commands", commands), ("agents", agents)):
        if value:
            spec[key] = value
    if servers:
        spec["mcp_servers"] = servers
    if shuffle.get("services"):
        spec["services"] = shuffle["services"]
    try:
        _, _, _, components = build_files(spec)
    except PluginDraftError as exc:
        result.errors.append(str(exc))
        return result
    result.spec = spec
    result.components = components
    return result


def scaffold_plugin(name: str, *, drafts_root: Path | None = None) -> Path:
    """Create a new draft, never replacing an existing folder or symlink."""
    if not _NAME_RE.fullmatch(name):
        raise ValueError("name must be 1-63 lowercase letters, digits or hyphens, starting with a letter or digit")
    parent = drafts_root if drafts_root is not None else Path.home() / ".fluid" / "plugin-drafts"
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / name
    root.mkdir()  # exist_ok=False: no overwriting a draft (including a symlink).
    (root / "skills").mkdir()
    manifest = {
        "name": name, "version": "0.1.0",
        "description": "Describe what this plugin does.", "skills": "./skills/",
    }
    (root / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (root / "AGENTS.md").write_text(_DRAFT_RULES, encoding="utf-8")
    return root


_DRAFT_RULES = """# Plugin draft rules

Allowed files: plugin.json; skills/<name>/SKILL.md; commands/<name>.md;
agents/<name>.md; mcp.json; root README.md, AGENTS.md and CLAUDE.md.
The three root Markdown files are draft guidance only and are not installed.
No hooks, scripts, binaries, .env files or foreign manifest folders.

Never put keys, tokens or passwords in any file or ask for them in chat.
Every MCP env value must be a ${VAR} reference, never a literal value.
Declare outside services by name; credentials belong in approved private cards.

plugin.json requires name, version and description; start at 0.1.0. Allowed
optional keys: author, skills, mcpServers, extensions (ai.shuffle).
Skill frontmatter: name (folder name), description. Command frontmatter:
optional description. Subagent frontmatter: name (file name), description,
optional tools. Subagents must omit model, effort, provider, permissionMode,
hooks and allowed-tools: model/effort and permissions come from the chat.
Use lowercase letters, digits and hyphens for names.

Add at least one skill, command, agent or MCP server, then run from this folder:
`python -m fluid.context.plugin_check .`
Fix every problem before installing. Install switched off; only the owner
reviews and enables the plugin. Raise the version for subsequent changes.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--new":
        try:
            root = scaffold_plugin(args[1])
        except (OSError, ValueError) as exc:
            print(f"Cannot scaffold plugin: {exc}")
            return 1
        print(f"Created {root}. Read AGENTS.md, add a component, then check the draft.")
        return 0
    if len(args) != 1 or args[0].startswith("--"):
        print("usage: python -m fluid.context.plugin_check <plugin folder> | --new <name>")
        return 2
    result = check_plugin_folder(args[0])
    print(result.render())
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

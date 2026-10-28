"""Skill — progressive disclosure: load a discovered skill's full body on demand.

Skills are advertised in the system prompt as name+description only (~100
tokens each, parsed from SKILL.md frontmatter at discovery time). The body —
which can be arbitrarily long — enters context ONLY when the model invokes
this tool, keeping the prefix small and cache-stable.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fluid._filelock import file_lock
from fluid.context.instructions import Skill, discover_skills, parse_frontmatter, skill_search_roots
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec


# How long a load waits for another writer of the sidecar. A missed count is
# acceptable telemetry loss; a turn blocked on a stuck lock holder is not.
_USAGE_LOCK_WAIT_S = 5.0
# Serializes writers inside this process before the cross-process lock: flock
# on Linux is per open file description, so two threads each opening the
# lock file would otherwise both "hold" it.
_USAGE_THREAD_LOCK = threading.Lock()


def _note_usage(skill_path: str) -> None:
    """Stamp load count + last-used time in a sidecar next to the skill.

    Best-effort telemetry for curation and the user (`.usage.json` beside the
    global skills root) — kept out of SKILL.md frontmatter so user-authored
    files never accrue merge noise.

    Loads are noted concurrently (the delegated leg starts one thread per
    SKILL.md Read), so the read-modify-write holds a lock — a thread lock plus
    an advisory file lock beside the sidecar for other processes — and the new
    document replaces the old one atomically: a reader never sees a half
    written file. A sidecar that does not parse is left as it is rather than
    reset, so one bad write can never wipe every skill's counts. Async callers
    must run this off the event loop because acquiring the locks can wait.
    """
    try:
        root = Path.home() / ".fluid" / "skills"
        root.mkdir(parents=True, exist_ok=True)
        sidecar = root / ".usage.json"
        with _USAGE_THREAD_LOCK, file_lock(
            root / ".usage.json.lock", wait=_USAGE_LOCK_WAIT_S,
        ) as held:
            if not held:
                return
            try:
                raw = sidecar.read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = ""
            if raw.strip():
                try:
                    usage = json.loads(raw)
                except ValueError:
                    return
                if not isinstance(usage, dict):
                    return
            else:
                usage = {}
            entry = usage.get(skill_path)
            if not isinstance(entry, dict):
                entry = usage[skill_path] = {"loads": 0}
            try:
                loads = int(entry.get("loads", 0))
            except (TypeError, ValueError):
                loads = 0
            entry["loads"] = loads + 1
            entry["last_used_at"] = time.time()
            _replace_text(sidecar, json.dumps(usage, indent=2))
    except OSError:
        pass


def _replace_text(path: Path, text: str) -> None:
    """Write ``text`` to a sibling temp file, then ``os.replace`` it over
    ``path``. Raises ``OSError``; the temp file is removed on failure."""
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except OSError as exc:
                # Windows readers/indexers can briefly deny an atomic rename.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(0.01 * (2 ** attempt))
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def note_skill_file_read(
    path: str, cwd: str | None, sources: Any = None,
) -> str | None:
    """Count a direct read of a discovered skill's ``SKILL.md`` as a load.

    The delegated Claude leg lists skills with their SKILL.md paths and the
    model Reads the file instead of calling Skill, so without this its loads
    never reach ``.usage.json``. Only a file the Skill tool itself would
    resolve (same discovery, same scope gates) counts; any other path named
    SKILL.md is ignored. Returns the counted skill path, or None. Blocking
    disk IO (discovery walks the skill roots), never raises.
    """
    try:
        if not path or os.path.basename(path).lower() != "skill.md":
            return None
        user_on = sources is None or "user" in sources
        project_on = sources is None or "project" in sources
        from fluid.context.plugins import discover_plugins

        plugins = (
            discover_plugins(cwd or ".", sources=sources)
            if user_on and sources is not None else None
        )
        skills = discover_skills(
            cwd or ".", include_global=user_on, include_project=project_on,
            include_plugins=user_on, plugins=plugins,
        )
        target = os.path.normcase(os.path.realpath(path))
        for skill in skills:
            if os.path.normcase(os.path.realpath(skill.path)) == target:
                _note_usage(skill.path)
                return skill.path
    except Exception:  # noqa: BLE001 — telemetry never fails a turn
        return None
    return None


def _local(name: str) -> str:
    """The part after the ``<plugin>:`` prefix, lowercased."""
    return name.rsplit(":", 1)[-1].lower()


def _resolve(skills: list[Skill], name: str) -> tuple[Skill | None, list[str]]:
    """Find the requested skill, tolerating the plugin namespace.

    Plugin skills are indexed as ``<plugin>:<name>``, and a model reading
    ``unslop:unslop`` in the prompt routinely asks for ``unslop`` — a first
    call wasted on an error it then has to retry. So: exact match, then
    case-insensitive, then a match on the local name from either side (bare
    request against a namespaced skill, or the reverse). Ambiguity is never
    guessed; the candidates come back for the caller to report.
    """
    exact = next((s for s in skills if s.name == name), None)
    if exact is not None:
        return exact, []
    folded = [s for s in skills if s.name.lower() == name.lower()]
    if len(folded) == 1:
        return folded[0], []
    local = _local(name)
    candidates = [s for s in skills if _local(s.name) == local]
    if len(candidates) == 1:
        return candidates[0], []
    return None, sorted(s.name for s in candidates)


def _expand_plugin_body(body: str, path: str, plugins: Any, cwd: Any, sources: Any) -> str:
    """Fill in a plugin skill's placeholders, as Claude Code does on load:
    ``${CLAUDE_PLUGIN_ROOT}``/``${PLUGIN_ROOT}`` become the plugin's folder,
    the data variables its data folder, and ``${user_config.KEY}`` a note that
    the setting is not configured here (see
    :func:`fluid.context.plugin_import.expand_plugin_paths`). Best effort: on
    any failure the body is returned unchanged."""
    try:
        from fluid.context.plugin_import import expand_plugin_paths
        from fluid.context.plugins import discover_plugins, plugin_data_dir

        if plugins is None:
            plugins = discover_plugins(cwd) if sources is None else discover_plugins(cwd, sources=sources)
        for plugin in plugins:
            if any(s.path == path for s in plugin.skills):
                return expand_plugin_paths(
                    body, root=plugin.root, data=plugin_data_dir(plugin.name, plugin.marketplace)
                )
    except Exception:  # noqa: BLE001 — loading a skill never fails on this
        pass
    return body


SPEC = ToolSpec(
    name="Skill",
    description=(
        "Load the full instructions of an installed skill by name. When any "
        "skills are installed they appear under '# Available skills' in the "
        "system prompt (name + description only — bodies load on demand "
        "here); no such section means none are installed. When the task at "
        "hand is one a listed skill covers, call this first and follow the "
        "loaded instructions in place of your default approach. Plugin "
        "skills are listed as '<plugin>:<name>' and are opt-in: load one "
        "only when the user asks for it or the task is mainly its job. "
        "Either spelling resolves while it is unambiguous. Calling with an unknown name returns the "
        "list of known skills and the searched roots. In code mode reach it "
        "with `from tools.Skill import Skill` and `Skill(name=\"<skill>\")` "
        "inside run_python."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The skill's name."},
        },
        "required": ["name"],
    },
    risk="safe",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    name = str(input.get("name", "")).strip()
    if not name:
        return ToolOutcome(text="missing skill name", is_error=True)
    # The same scope gates that shaped the prompt's index: a skill the index
    # could not list is not offered as "available" here either.
    sources = ctx.extra.get("setting_sources")
    user_on = sources is None or "user" in sources
    project_on = sources is None or "project" in sources
    from fluid.context.plugins import discover_plugins

    plugins = discover_plugins(ctx.cwd, sources=sources) if user_on and sources is not None else None
    skills = discover_skills(
        ctx.cwd, include_global=user_on, include_project=project_on,
        include_plugins=user_on, plugins=plugins,
    )
    match, ambiguous = _resolve(skills, name)
    if match is None:
        if ambiguous:
            return ToolOutcome(
                text=(
                    f"ambiguous skill: {name}. Several match: "
                    f"{', '.join(ambiguous)} — ask for one by its full name."
                ),
                is_error=True,
            )
        known = ", ".join(sorted(s.name for s in skills)) or "(none discovered)"
        roots = [str(root) for root, _ in skill_search_roots(
            ctx.cwd, include_global=user_on, include_project=project_on,
            include_builtin=False,
        )]
        if user_on:
            roots.append(str(Path.home() / ".fluid" / "plugins"))
        searched = ", ".join(roots) or "(none — disabled by setting_sources)"
        return ToolOutcome(
            text=f"unknown skill: {name}. Available: {known}. Searched roots: {searched}",
            is_error=True,
        )
    try:
        raw = Path(match.path).read_text(encoding="utf-8")
    except OSError as exc:
        return ToolOutcome(text=f"failed to read skill: {exc}", is_error=True)
    _, body = parse_frontmatter(raw)
    if match.scope == "plugin":
        body = _expand_plugin_body(body, match.path, plugins, ctx.cwd, sources)
    await asyncio.to_thread(_note_usage, match.path)
    return ToolOutcome(
        text=f"# Skill: {match.name}\n# From: {match.path}\n\n{body.strip()}"
    )

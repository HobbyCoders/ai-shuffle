"""Skill — progressive disclosure: load a discovered skill's full body on demand.

Skills are advertised in the system prompt as name+description only (~100
tokens each, parsed from SKILL.md frontmatter at discovery time). The body —
which can be arbitrarily long — enters context ONLY when the model invokes
this tool, keeping the prefix small and cache-stable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fluid.context.instructions import Skill, discover_skills, parse_frontmatter
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec


def _note_usage(skill_path: str) -> None:
    """Stamp load count + last-used time in a sidecar next to the skill.

    Best-effort telemetry for curation and the user (`.usage.json` beside the
    global skills root) — kept out of SKILL.md frontmatter so user-authored
    files never accrue merge noise.
    """
    try:
        root = Path.home() / ".fluid" / "skills"
        root.mkdir(parents=True, exist_ok=True)
        sidecar = root / ".usage.json"
        try:
            usage = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            usage = {}
        entry = usage.setdefault(skill_path, {"loads": 0})
        entry["loads"] = int(entry.get("loads", 0)) + 1
        entry["last_used_at"] = time.time()
        sidecar.write_text(json.dumps(usage, indent=2), encoding="utf-8")
    except OSError:
        pass


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


SPEC = ToolSpec(
    name="Skill",
    description=(
        "Load the full instructions of an installed skill by name. When any "
        "skills are installed they appear under '# Available skills' in the "
        "system prompt (name + description only — bodies load on demand "
        "here); no such section means none are installed. When the task at "
        "hand is one a listed skill covers, call this first and follow the "
        "loaded instructions in place of your default approach. Plugin "
        "skills are listed as '<plugin>:<name>'; either spelling resolves "
        "while it is unambiguous. Calling with an unknown name returns the "
        "list of known skills."
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
    skills = discover_skills(
        ctx.cwd, include_global=user_on, include_project=project_on,
        include_plugins=user_on,
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
        return ToolOutcome(
            text=f"unknown skill: {name}. Available: {known}", is_error=True
        )
    try:
        raw = Path(match.path).read_text(encoding="utf-8")
    except OSError as exc:
        return ToolOutcome(text=f"failed to read skill: {exc}", is_error=True)
    _, body = parse_frontmatter(raw)
    _note_usage(match.path)
    return ToolOutcome(
        text=f"# Skill: {match.name}\n# From: {match.path}\n\n{body.strip()}"
    )

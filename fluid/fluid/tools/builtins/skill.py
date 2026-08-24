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

from fluid.context.instructions import discover_skills, parse_frontmatter
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

SPEC = ToolSpec(
    name="Skill",
    description=(
        "Load the full instructions of an installed skill by name. When any "
        "skills are installed they appear under '# Available skills' in the "
        "system prompt (name + description only — bodies load on demand "
        "here); no such section means none are installed. When the task at "
        "hand is one a listed skill covers, call this first and follow the "
        "loaded instructions in place of your default approach. Calling with "
        "an unknown name returns the list of known skills."
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
    skills = discover_skills(ctx.cwd, include_plugins=True)
    match = next((s for s in skills if s.name == name), None)
    if match is None:
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

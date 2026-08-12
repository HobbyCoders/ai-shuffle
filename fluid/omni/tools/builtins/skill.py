"""Skill — progressive disclosure: load a discovered skill's full body on demand.

Skills are advertised in the system prompt as name+description only (~100
tokens each, parsed from SKILL.md frontmatter at discovery time). The body —
which can be arbitrarily long — enters context ONLY when the model invokes
this tool, keeping the prefix small and cache-stable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omni.context.instructions import discover_skills, parse_frontmatter
from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Skill",
    description=(
        "Load the full instructions of an available skill by name. Skills are "
        "listed (name + description) in the system prompt; call this before "
        "performing a task a skill covers."
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
    skills = discover_skills(ctx.cwd)
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
    return ToolOutcome(
        text=f"# Skill: {match.name}\n# From: {match.path}\n\n{body.strip()}"
    )

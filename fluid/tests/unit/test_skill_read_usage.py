"""A direct Read of a skill's SKILL.md counts as a load (#687).

The delegated Claude leg lists skills by path and the model Reads the file,
so ``note_skill_file_read`` stands in for the Skill tool's own usage stamp.
"""

from __future__ import annotations

import json
from pathlib import Path

from fluid.tools.builtins.skill import note_skill_file_read

SKILL = "---\nname: {name}\ndescription: Something useful.\n---\nBody.\n"


def _usage(home: Path) -> dict:
    path = home / ".fluid" / "skills" / ".usage.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(SKILL.format(name=name), encoding="utf-8")
    return path


def test_a_read_of_a_discovered_skill_counts_once_per_read(isolate_home, tmp_path) -> None:
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    global_skill = _skill(isolate_home / ".fluid" / "skills", "deploy")
    project_skill = _skill(proj / ".fluid" / "skills", "lint")

    assert note_skill_file_read(str(global_skill), str(proj)) == str(global_skill)
    assert note_skill_file_read(str(global_skill), str(proj)) == str(global_skill)
    assert note_skill_file_read(str(project_skill), str(proj)) == str(project_skill)
    usage = _usage(isolate_home)
    assert usage[str(global_skill)]["loads"] == 2
    assert usage[str(project_skill)]["loads"] == 1


def test_other_files_and_scoped_out_skills_are_not_counted(isolate_home, tmp_path) -> None:
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    global_skill = _skill(isolate_home / ".fluid" / "skills", "deploy")
    stray = _skill(tmp_path / "elsewhere", "stray")

    assert note_skill_file_read(str(stray), str(proj)) is None
    assert note_skill_file_read(str(proj / "README.md"), str(proj)) is None
    # The same scope gates as the Skill tool: no "user" source, no global skill.
    assert note_skill_file_read(str(global_skill), str(proj), ("project",)) is None
    assert _usage(isolate_home) == {}

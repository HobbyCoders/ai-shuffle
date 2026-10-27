"""Measure what one project's ``# Memory`` snapshot makes visible.

python scripts/measure_memory_index.py --memory-file <copy of memories.json> [--project KEY]
python scripts/measure_memory_index.py            # synthetic fixture store

The memory file is copied into a temporary directory first, so the source is
never opened for writing. Without --project the project scope holding the
most live facts is used. stdout carries aggregate counts only, never fact
text: the section's characters (and ceil(chars/4) as a token estimate, not
billing), how many facts appear with their full text, how many as an index
line (a when-line or a shortened text), and how many in-scope facts the
snapshot only counts. Runs against whichever fluid is on the path, so the
same command measures a baseline checkout and this one.
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fluid"))

from fluid.codemode.skills import SkillStore  # noqa: E402


def _synthetic(root: Path) -> None:
    store = SkillStore(root)
    for i in range(240):
        scope = "project:fixture" if i % 4 else "global"
        store.save_memory(f"Synthetic fact {i}: the fixture service on port {8000 + i} "
                          f"uses runner r{i % 7} and keeps logs under logs/{i}.txt for later checks.",
                          scope=scope, protected=i % 3 == 0, durable=i % 3 != 0)


def measure(memory_file: Path | None, project: str | None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if memory_file is None:
            _synthetic(root)
        else:
            shutil.copyfile(memory_file, root / "memories.json")
        doc = json.loads((root / "memories.json").read_text(encoding="utf-8"))
        store = SkillStore(root, owner=doc.get("owner") or "local")
        live = store.memories()
        if project is None:
            scopes = collections.Counter(e.scope for e in live if e.scope != "global")
            project = scopes.most_common(1)[0][0] if scopes else None
        section = store.render_memory_section(project) or ""
        plan = store.memory_snapshot_plan(project, entries=live, archived=store.archived_memories())
        texts = {e.id: e.text for e in live}
        full = index = 0
        for line in section.splitlines():
            if not line.startswith("- [") or "] " not in line:
                continue
            fid = line[3:].split("]", 1)[0].split(" ", 1)[0]
            if fid not in texts:
                continue
            if texts[fid] in line:
                full += 1
            else:
                index += 1
        in_scope = sum(1 for e in live if project is None or e.scope in ("global", project))
        return {
            "project_scope_picked": "most entries" if memory_file else "fixture",
            "live_in_scope": in_scope,
            "section_chars": len(section),
            "section_tokens_est": math.ceil(len(section) / 4),
            "facts_full_text": full,
            "facts_index_line": index,
            "facts_visible": full + index,
            "facts_only_counted": int(plan.get("over_budget", 0)),
            "archived_in_scope": int(plan.get("archived", 0)),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-file", type=Path)
    parser.add_argument("--project")
    args = parser.parse_args()
    print(json.dumps(measure(args.memory_file, args.project), indent=2))


if __name__ == "__main__":
    main()

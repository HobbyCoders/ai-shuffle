"""Measure only the added owner-identity injection; no live data or model calls.

Run with PYTHONPATH=fluid python scripts/measure_owner_identity_memory.py.
For the unchanged native/delegated system and per-tool prefix breakdown, also run
scripts/measure_prompt_catalog.py --json (with --delegated for the CLI append).
Counts are characters and ceil(chars/4) estimates, not billed provider tokens.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]


def measure_identity_snapshot() -> dict[str, int]:
    from fluid.codemode.skills import SkillStore

    from app.core.owner_introduction import remember_identity
    from app.core.memory_reminder import plan_memory_note

    with TemporaryDirectory(prefix="owner-identity-measure-") as directory:
        store = SkillStore(Path(directory), owner="local")
        with patch("app.core.fluid_memory.store_for_owner", return_value=store):
            before = plan_memory_note(store.render_memory_section(), None).note or ""
            fixture = {"detected": {"preferred_name": "Ada", "github_username": "ada-example"}}
            remember_identity(fixture)
            after = plan_memory_note(store.render_memory_section(), None).note or ""
            remember_identity(fixture)
            repeated = plan_memory_note(store.render_memory_section(), None).note or ""
        paragraphs = [part for part in after.split("\n\n") if part.strip()]
        result = {"before_chars": len(before), "after_chars": len(after),
                  "repeated_chars": len(repeated), "delta_chars": len(after) - len(before),
                  "duplicate_paragraphs": len(paragraphs) - len(set(paragraphs))}
        for name in ("before", "after", "repeated", "delta"):
            result[name + "_estimated_tokens"] = (result[name + "_chars"] + 3) // 4
        return result


def main() -> None:
    print(json.dumps(measure_identity_snapshot(), indent=2))


if __name__ == "__main__":
    main()

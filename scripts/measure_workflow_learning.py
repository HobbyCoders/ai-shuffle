"""Deterministic workflow-learning request cost; no live data or model calls.

Run: PYTHONPATH=fluid python scripts/measure_workflow_learning.py [--catalog]
Tokens are a chars/4 estimate, not provider billing. --catalog reuses the
owner code-mode native/delegated prefix renderer (including per-tool sizes).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]

from fluid.codemode.continual import Synthesizer
from fluid.codemode.skills import SkillStore


def episode(session: str, surface: str = "native") -> list[dict[str, object]]:
    """Build a fixed successful episode in the selected producer's journal shape."""
    common = {"project": "fixture-project", "session": session, "ts": 1}
    execution = ({"kind": "cell", "code": "result = 'fixture-ok'", "value": "fixture-ok"}
                 if surface == "code" else
                 {"kind": "tool", "name": "Bash", "input": "printf fixture-ok",
                  "output": "fixture-ok", "error": False})
    return [{**common, **row} for row in [
        {"kind": "user", "text": "Summarize the checked fixture result."},
        execution,
        {"kind": "assistant", "text": "Completed the fixture summary: fixture-ok."},
        {"kind": "user", "text": "workflow accepted: fixture-summary"},
    ]]


def render(root: Path, surface: str = "native", repeated: bool = True) -> dict[str, str]:
    """Render exact synthesis inputs using an isolated fixture-backed store."""
    store = SkillStore(root)
    rows = episode("first", surface)
    if repeated:
        rows += episode("second", surface)
    for row in rows:
        store.journal_append(row)
    synth = Synthesizer(None, "fixture", store, project="fixture-project")
    group = store.journal_entries()[4:] if repeated else store.journal_entries()[:4]
    # Retains compatibility with the base revision for reproducible before/after.
    if hasattr(synth, "_synthesis_input"):
        prompt, system, _ = synth._synthesis_input(group)
    else:
        prompt, system = synth._prompt(group), synth._synthesis_system(group)
    return {"system": system, "request": prompt}


def measure() -> dict[str, dict[str, dict[str, int | float]]]:
    """Measure characters and estimated tokens by surface and request section."""
    results = {}
    with tempfile.TemporaryDirectory() as temporary:
        for surface in ("native", "code", "delegated"):
            for repeated in (False, True):
                name = surface + ("_repeated" if repeated else "_ordinary")
                texts = render(Path(temporary) / name, surface, repeated)
                results[name] = {key: {"characters": len(text), "estimated_tokens": len(text) / 4}
                                 for key, text in texts.items()}
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", action="store_true")
    args = parser.parse_args()
    result = {"workflow_requests": measure()}
    if args.catalog:
        from scripts.measure_prompt_catalog import measure as catalog
        result["owner_prefix"] = {
            "native": catalog(native=True), "delegated": catalog(native=False),
        }
    print(json.dumps(result, indent=2))

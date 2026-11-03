"""Offline owner/code prefix and scratch lifetime measurement. No real provider or DB.

Run: PYTHONPATH=fluid python scripts/measure_scratch_context.py --turns 242
Host prefix sections/tool catalogs use measure_prompt_catalog's production factories;
the CLI preset is proprietary and excluded. Turn notes go through the real adapter
and native/delegated loops with fake providers. Counts are chars/4 estimates only.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "fluid")]


async def turns(count: int, *, native: bool):
    from pytest import MonkeyPatch
    from fluid.state import scratch_prompt
    from fluid.tools.registry import ToolCtx
    from tests.core.test_memory_reminder import _adapter, _turn, _delegated, _delegated_turn

    with tempfile.TemporaryDirectory() as temp, MonkeyPatch.context() as patch:
        from app.core.provider.harness_adapter import HarnessAdapter
        from fluid.store.base import MemoryStore
        patch.setenv("TESTING", "1")
        patch.setattr(HarnessAdapter, "_test_memory_store_factory", MemoryStore, raising=False)
        memory = {"section": "# Memory\n- [m1 r1; user] Keep owner facts until explicitly corrected."}
        if native:
            adapter, loop = await _adapter(patch, memory)
        else:
            adapter = await _delegated(patch, Path(temp), memory)
            loop = adapter._engine
        note = scratch_prompt(ToolCtx(cwd="/fixture", session_id="owner-code",
                                      extra={"harness_state_root": "/fixture/state"}))
        patch.setattr(adapter, "_scratch_note", lambda: note)
        advance = _turn if native else _delegated_turn
        try:
            for i in range(count):
                await advance(adapter, f"Continue code task {i}.")
            nodes = await loop.store.load_path(loop.session_id)
            messages = [n.message.text() or "" for n in nodes if n.message.role == "user"]
            copies = [m.count("# Harness scratch") for m in messages]
            total = sum(copies)
            reread = sum((len(copies) - i) * n for i, n in enumerate(copies))
            return {"turns": len(messages), "copies": total, "block_chars": len(note),
                    "history_chars": total * len(note),
                    "history_tokens_estimated": (total * len(note) + 3) // 4,
                    "cumulative_read_chars": reread * len(note),
                    "cumulative_read_tokens_estimated": (reread * len(note) + 3) // 4}
        finally:
            await adapter.disconnect()


def measure(count=242):
    import logging
    from unittest.mock import patch
    from scripts.measure_prompt_catalog import measure as prefix
    # Refuse all database access, including best-effort optional context readers.
    # Render no installed owner data and never load a live worker descriptor.
    with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {
            "TESTING": "1", "DATA_DIR": temp, "AI_SHUFFLE_ENV_FILE": ""}), \
            patch("app.db.base.get_connection", side_effect=AssertionError("offline measurement opened DB")):
        logging.disable(logging.CRITICAL)
        return {leg: {"prefix": prefix(native=native), "scratch": asyncio.run(turns(count, native=native))}
                for leg, native in (("native", True), ("delegated", False))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=242)
    args = parser.parse_args()
    print(json.dumps(measure(args.turns), indent=2))

"""``DelegatedLoop(on_initialize=...)`` receives the CLI's initialize reply.

The host keeps the model list the CLI reports there (no prompt is needed to
learn it); a failing hook never fails the turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fluid.engine.delegated import DelegatedLoop
from fluid.events import TurnComplete, UserTurn
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
MODELS = [{"value": "claude-opus-4-6", "displayName": "Opus 4.6", "supportsEffort": True,
           "supportedEffortLevels": ["low", "medium", "high", "max"]}]


async def _run(tmp_path: Path, hook) -> list:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"initialize_reply": {"models": MODELS, "claude_code_version": "9.9.9"}}))
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-opus-4-6", claude_cmd=[sys.executable, str(FAKE)],
        env={"FAKE_CLAUDE_SCRIPT": str(script)}, cwd=str(tmp_path), on_initialize=hook,
    )
    try:
        await loop.submit(UserTurn(content="hi"))
        await loop.wait_turn()
        return loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()


async def test_the_hook_gets_the_reply_payload(tmp_path) -> None:
    seen: list = []
    events = await _run(tmp_path, seen.append)
    assert isinstance(events[-1], TurnComplete)
    assert len(seen) == 1
    assert seen[0]["models"] == MODELS
    assert seen[0]["claude_code_version"] == "9.9.9"


async def test_a_failing_hook_does_not_fail_the_turn(tmp_path) -> None:
    def boom(_payload):
        raise RuntimeError("nope")

    events = await _run(tmp_path, boom)
    assert isinstance(events[-1], TurnComplete)

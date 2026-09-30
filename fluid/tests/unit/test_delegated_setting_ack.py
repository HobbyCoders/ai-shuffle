"""R9-16: the delegated leg records a model / permission-mode switch only once
the Claude CLI acknowledges it.

The CLI answers every ``control_request`` with a ``control_response``
(``success``, or ``error`` with a reason). ``SetModel`` / ``SetPermissionMode``
used to write the frame fire-and-forget and record the new value at once, so a
refused switch was recorded as applied and the host's next turn with the same
value skipped it. Driven against the scripted fake CLI.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

import fluid.engine.delegated as delegated_mod
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import FORCED_CHILD_ENV
from fluid.events import ErrorEvent, TurnComplete, UserTurn
from fluid.events.model import Interrupted
from fluid.events.ops import SetModel, SetPermissionMode
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)


def _loop(tmp_path: Path, controls: dict | None = None) -> tuple[DelegatedLoop, Path]:
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"controls": controls or {}}), encoding="utf-8")
    record = tmp_path / "record.jsonl"
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake", permission_mode="default",
        claude_cmd=[sys.executable, str(FAKE)],
        env={"FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_RECORD": str(record)},
    )
    return loop, record


async def _turn(loop: DelegatedLoop) -> None:
    """One plain turn, so the CLI process is up."""
    sid = (await loop.store.create_session()).session_id
    loop._session_id = sid
    start = loop.bus.last_seq(sid)

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid, since_seq=start):
            if isinstance(event, _TERMINALS):
                return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await loop.submit(UserTurn(content="hi"))
    await asyncio.wait_for(watcher, 20)
    await asyncio.wait_for(loop.wait_turn(), 20)


def _sent(record: Path, subtype: str) -> list[dict]:
    rows = [json.loads(line) for line in record.read_text().splitlines() if line]
    return [r["frame"] for r in rows if r["kind"] == "stdin"
            and r["frame"].get("type") == "control_request"
            and (r["frame"].get("request") or {}).get("subtype") == subtype]


async def test_acknowledged_run_settings_keep_cli_process_and_update_spawn_flags(tmp_path):
    loop, record = _loop(tmp_path)
    loop.effort = "high"
    loop.settings = json.dumps({"fastMode": False, "env": {"SAFE_TEST": "keep"}})
    try:
        await _turn(loop)
        proc = loop._proc
        await loop.set_run_settings(effort="low", fast=True)
        await loop.set_run_settings(effort=None, fast=False)
        assert loop._proc is proc
        assert loop.effort is None
        # The host's env block stays, with the forced overrides on top.
        assert json.loads(loop.settings) == {
            "effortLevel": None, "fastMode": False,
            "env": {"SAFE_TEST": "keep", **FORCED_CHILD_ENV},
        }
        sent = _sent(record, "apply_flag_settings")
        assert sent[0]["request"]["settings"]["effortLevel"] == "low"
        assert sent[1]["request"]["settings"]["fastMode"] is False
        assert loop._setting_request("apply_flag_settings")["settings"] == json.loads(loop.settings)
        argv = loop._build_argv()
        assert "--effort" not in argv
        assert json.loads(argv[argv.index("--settings") + 1])["fastMode"] is False
    finally:
        await loop.aclose()


@pytest.mark.parametrize("how", ["error", "mute"])
async def test_unacknowledged_run_settings_do_not_record_success(tmp_path, monkeypatch, how):
    monkeypatch.setattr(delegated_mod, "CONTROL_TIMEOUT", 0.2)
    loop, record = _loop(tmp_path, {"apply_flag_settings": how})
    loop.effort = "high"
    loop.settings = '{"fastMode":false}'
    try:
        await _turn(loop)
        with pytest.raises(RuntimeError, match="apply_flag_settings"):
            await loop.set_run_settings(effort="low", fast=True)
        assert loop.effort == "high"
        assert json.loads(loop.settings)["fastMode"] is False
        assert loop._setting_request("apply_flag_settings")["settings"] == {
            "effortLevel": "high", "fastMode": False, "env": dict(FORCED_CHILD_ENV),
        }
        assert len(_sent(record, "apply_flag_settings")) == 1
    finally:
        await loop.aclose()


async def test_acknowledged_switches_are_recorded(tmp_path) -> None:
    loop, record = _loop(tmp_path)
    try:
        await _turn(loop)
        await loop.submit(SetModel(model="claude-other"))
        await loop.submit(SetPermissionMode(mode="acceptEdits"))
        assert loop.model == "claude-other"
        assert loop.permission_mode == "acceptEdits"
        assert _sent(record, "set_model")[0]["request"]["model"] == "claude-other"
    finally:
        await loop.aclose()


@pytest.mark.parametrize("how", ["error", "mute"])
async def test_a_refused_or_unanswered_model_switch_keeps_the_old_model(tmp_path, monkeypatch, how) -> None:
    monkeypatch.setattr(delegated_mod, "CONTROL_TIMEOUT", 0.5)
    loop, record = _loop(tmp_path, {"set_model": how})
    try:
        await _turn(loop)
        with pytest.raises(RuntimeError, match="set_model"):
            await loop.submit(SetModel(model="claude-other"))
        assert loop.model == "claude-fake"
        assert len(_sent(record, "set_model")) == 1
    finally:
        await loop.aclose()


async def test_a_refused_mode_switch_keeps_the_old_mode(tmp_path) -> None:
    loop, _record = _loop(tmp_path, {"set_permission_mode": "error"})
    try:
        await _turn(loop)
        with pytest.raises(RuntimeError, match="refused set_permission_mode"):
            await loop.submit(SetPermissionMode(mode="bypassPermissions"))
        assert loop.permission_mode == "default"
    finally:
        await loop.aclose()


async def test_with_no_cli_running_the_switch_applies_to_the_next_spawn(tmp_path) -> None:
    loop, _record = _loop(tmp_path)
    await loop.submit(SetModel(model="claude-other"))
    await loop.submit(SetPermissionMode(mode="acceptEdits"))
    argv = loop._build_argv()
    assert argv[argv.index("--model") + 1] == "claude-other"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


async def test_the_repl_model_command_reports_a_refused_switch(tmp_path) -> None:
    """R11-5: ``/model`` printed the refusal instead of crashing the REPL."""
    from fluid.cli.repl import Repl

    loop, _record = _loop(tmp_path, {"set_model": "error"})
    try:
        await _turn(loop)
        out = io.StringIO()
        repl = Repl(loop, loop.session_id, input_fn=lambda prompt: "", out=out)
        assert await repl.dispatch_slash("/model claude-other") is True
        assert "model not changed" in out.getvalue()
        assert "refused set_model" in out.getvalue()
        assert loop.model == "claude-fake"
    finally:
        await loop.aclose()

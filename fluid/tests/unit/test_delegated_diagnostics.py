"""What a delegated session records about the ``claude`` CLI that served it.

A stale worker without the bash-first steer override, a CLI running on an API
key instead of the subscription, or a CLI that attached the steer anyway must
be visible from the session afterwards: the ``DelegatedSurface`` carries the
CLI's own init facts and the forced env, the spawn log names the overrides,
and a steer found in the CLI's transcript becomes a WARNING plus a
``DelegatedDiagnostic``. Driven against the scripted fake CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

import fluid.engine.delegated as delegated_mod
from fluid.engine.delegated import DelegatedLoop, _find_bash_first_steer
from fluid.engine.delegated_proto import FORCED_CHILD_ENV
from fluid.events import ErrorEvent, TurnComplete, UserTurn
from fluid.events.model import (
    DelegatedDiagnostic,
    DelegatedSurface,
    Interrupted,
    event_from_dict,
    event_to_dict,
)
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)
CLI_ID = "cli-steer-1"

# The attachment CLI 2.1.285 wrote for a steered Opus 5.5 turn (probe transcript).
STEER = {"type": "auto_mode", "autoModeConsentFlow": False, "bashFirst": True,
         "bashFirstSteer": "relaxed", "steerOnly": True, "bypass": True}


def _loop(tmp_path: Path, script: dict | None = None, *, env: dict | None = None,
          **kwargs: Any) -> tuple[DelegatedLoop, Path]:
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script or {}), encoding="utf-8")
    record = tmp_path / "record.jsonl"
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake",
        claude_cmd=[sys.executable, str(FAKE)],
        env={"FAKE_CLAUDE_SCRIPT": str(script_path), "FAKE_CLAUDE_RECORD": str(record),
             **(env or {})},
        **kwargs,
    )
    return loop, record


async def _turn(loop: DelegatedLoop, text: str = "hi") -> list:
    if loop._session_id is None:
        loop._session_id = (await loop.store.create_session()).session_id
    sid = loop._session_id
    start = loop.bus.last_seq(sid)
    seen: list = []

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid, since_seq=start):
            seen.append(event)
            if isinstance(event, _TERMINALS):
                return

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    await loop.submit(UserTurn(content=text))
    await asyncio.wait_for(watcher, 20)
    await asyncio.wait_for(loop.wait_turn(), 20)
    # The steer check runs off the loop after the turn's terminal event.
    await asyncio.wait_for(asyncio.gather(*loop._steer_scans), 10)
    return seen


def _starts(record: Path) -> list[dict]:
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r["kind"] == "start"]


# ─── DelegatedSurface carries what the CLI says about itself ────────────────


async def test_the_surface_records_the_cli_build_mode_credentials_and_forced_env(tmp_path) -> None:
    loop, _record = _loop(tmp_path, {"init": {
        "claude_code_version": "2.1.285", "apiKeySource": "none", "model": "claude-opus-5-5",
        "session_id": CLI_ID,
    }}, permission_mode="bypassPermissions")
    try:
        events = await _turn(loop)
    finally:
        await loop.aclose()
    [surface] = [e for e in events if isinstance(e, DelegatedSurface)]
    assert surface.cli_version == "2.1.285"
    assert surface.api_key_source == "none"
    assert surface.permission_mode == "bypassPermissions"
    assert surface.model == "claude-opus-5-5"
    assert surface.cli_session_id == CLI_ID
    assert surface.child_env_overrides == FORCED_CHILD_ENV
    assert "Bash" in surface.tools


async def test_the_spawned_cli_gets_the_override_whatever_the_host_says(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_THRIFTY_SONIC", "1")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "0")
    loop, record = _loop(tmp_path, env={
        "CLAUDE_CODE_THRIFTY_SONIC": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0",
    })
    try:
        await _turn(loop)
    finally:
        await loop.aclose()
    [start] = _starts(record)
    assert start["env"]["CLAUDE_CODE_THRIFTY_SONIC"] == "0"
    assert start["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    argv = start["argv"]
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["env"] == FORCED_CHILD_ENV


async def test_the_spawn_log_names_the_forced_overrides(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    loop, _record = _loop(tmp_path)
    try:
        await _turn(loop)
    finally:
        await loop.aclose()
    [line] = [r.getMessage() for r in caplog.records if "claude CLI started" in r.getMessage()]
    assert "CLAUDE_CODE_THRIFTY_SONIC=0" in line
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1" in line
    assert "settings_env=forced" in line


# ─── The steer check reads the CLI's transcript ─────────────────────────────


def _transcript(config: Path, cli_id: str, entries: list[dict]) -> Path:
    path = config / "projects" / "C--some-project" / f"{cli_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


_USER = {"type": "user", "message": {"role": "user", "content": "hi"}}
_STEER_ENTRY = {"type": "attachment", "attachment": STEER, "uuid": "a1"}
_BOUNDARY = {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"}


async def _run_with_transcript(tmp_path, caplog, entries: list[dict] | None, **kwargs):
    config = tmp_path / "claude-config"
    config.mkdir()
    cli_id = kwargs.pop("cli_id", CLI_ID)
    version = kwargs.pop("version", "2.1.285")
    if entries is not None:
        _transcript(config, cli_id, entries)
    caplog.set_level(logging.DEBUG, logger="fluid.engine.delegated")
    loop, _record = _loop(
        tmp_path, {"version": f"{version} (Claude Code)",
                   "init": {"session_id": CLI_ID, "claude_code_version": version}},
        env={"CLAUDE_CONFIG_DIR": str(config)}, permission_mode="bypassPermissions", **kwargs,
    )
    try:
        events = await _turn(loop)
        events += await _turn(loop, "again")  # the check runs once per process
    finally:
        await loop.aclose()
    # Read back from the store: the diagnostic is persisted for audits.
    stored = await loop.store.events_since(loop.session_id, 0)
    diagnostics = [e for e in stored if isinstance(e, DelegatedDiagnostic)]
    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "bash-first steer" in r.getMessage()]
    return diagnostics, warnings


async def test_a_steer_in_the_transcript_is_reported_once(tmp_path, caplog) -> None:
    diagnostics, warnings = await _run_with_transcript(tmp_path, caplog, [_USER, _STEER_ENTRY])
    [warning] = warnings
    assert "2.1.285" in warning and "relaxed" in warning
    [diagnostic] = diagnostics
    assert diagnostic.kind == "bash_first_steer"
    assert diagnostic.data == {"cli_version": "2.1.285", "variant": "relaxed",
                               "resumed": False, "cli_session_id": CLI_ID,
                               "claude_mod_loaded": False}
    assert "mod" not in warning


async def test_a_steer_recorded_while_the_mod_ran_says_the_model_did_not_read_it(
    tmp_path, caplog,
) -> None:
    # The CLI's transcript keeps its own record of an attachment the mod left
    # out of the request, so the check still finds it and must say so.
    diagnostics, warnings = await _run_with_transcript(
        tmp_path, caplog, [_USER, _STEER_ENTRY], version="2.1.287", claude_mod=True,
    )
    [warning] = warnings
    assert "mod was loaded" in warning
    [diagnostic] = diagnostics
    assert diagnostic.data["claude_mod_loaded"] is True


async def test_a_resumed_process_says_the_steer_may_predate_the_fix(tmp_path, caplog) -> None:
    diagnostics, warnings = await _run_with_transcript(
        tmp_path, caplog, [_USER, _STEER_ENTRY], cli_id="resumed-cli", cli_session_id="resumed-cli",
    )
    [diagnostic] = diagnostics
    assert diagnostic.data["resumed"] is True
    assert diagnostic.data["cli_session_id"] == "resumed-cli"
    [warning] = warnings
    assert "resumed" in warning


@pytest.mark.parametrize("entries", [
    [_USER, _STEER_ENTRY, _BOUNDARY, _USER],  # compacted away
    [_USER, {"type": "attachment", "attachment": {**STEER, "bashFirst": False}}],
    [_USER],
    None,  # no transcript at all
])
async def test_no_steer_means_no_warning_and_no_diagnostic(tmp_path, caplog, entries) -> None:
    diagnostics, warnings = await _run_with_transcript(tmp_path, caplog, entries)
    assert diagnostics == [] and warnings == []


def test_a_steer_after_the_last_boundary_counts(tmp_path) -> None:
    config = tmp_path / "c"
    _transcript(config, "x", [_STEER_ENTRY, _BOUNDARY, _STEER_ENTRY])
    assert _find_bash_first_steer(str(config), "x") == STEER


def test_only_the_tail_of_a_huge_transcript_is_read(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(delegated_mod, "_STEER_SCAN_BYTES", 400)
    config = tmp_path / "c"
    filler = {"type": "user", "message": {"content": "x" * 300}}
    # The steer sits beyond the window; the cut first line must not break parsing.
    _transcript(config, "x", [_STEER_ENTRY, filler, filler, filler])
    assert _find_bash_first_steer(str(config), "x") is None
    _transcript(config, "y", [filler, filler, filler, _STEER_ENTRY])
    assert _find_bash_first_steer(str(config), "y") == STEER


async def test_a_failing_check_never_breaks_the_session(tmp_path, monkeypatch, caplog) -> None:
    def boom(*_args):
        raise OSError("disk gone")

    monkeypatch.setattr(delegated_mod, "_find_bash_first_steer", boom)
    loop, _record = _loop(tmp_path, {"init": {"session_id": CLI_ID}})
    try:
        events = await _turn(loop)
    finally:
        await loop.aclose()
    assert isinstance(events[-1], TurnComplete)


# ─── The new fields persist and old rows still load ─────────────────────────


def test_the_new_events_round_trip_and_old_surface_rows_still_load() -> None:
    surface = DelegatedSurface(
        session_id="s", turn_id="t", tools=("Bash", "Read"),
        mcp_servers=({"name": "fluid_bundled", "status": "connected"},),
        cli_version="2.1.285", permission_mode="bypassPermissions", model="opus",
        api_key_source="none", cli_session_id=CLI_ID,
        child_env_overrides=dict(FORCED_CHILD_ENV),
    )
    diagnostic = DelegatedDiagnostic(
        session_id="s", kind="bash_first_steer", message="m", data={"variant": "strict"},
    )
    for event in (surface, diagnostic):
        assert event_from_dict(json.loads(json.dumps(event_to_dict(event)))) == event

    old_row = {"_cls": "DelegatedSurface", "_v": 1, "session_id": "s", "node_id": None,
               "turn_id": "t", "seq": 3, "ts": 1.0, "tools": ["Bash"], "mcp_servers": []}
    old = event_from_dict(old_row)
    assert isinstance(old, DelegatedSurface)
    assert old.tools == ("Bash",)
    assert old.cli_version is None and old.child_env_overrides == {}

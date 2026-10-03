"""AI Shuffle's Claude Code mod on the delegated leg (``fluid/engine/claude_mod``).

The loop hands the CLI a per-session copy of the mod with ``--plugin-dir`` on
Claude Code 2.1.287 and newer, and nothing otherwise: an older or unknown CLI,
``FLUID_CLAUDE_MOD=false`` or a copy that fails all leave the spawn exactly as
it was before the mod. The mod's own behaviour is tested offline with
``claude plugin test`` (``claude_mod/tests``); these tests cover the wiring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import fluid.engine.delegated as delegated_mod
from fluid.engine.delegated import (
    CLAUDE_MOD_PLUGIN,
    CLAUDE_MOD_SINCE,
    CLAUDE_MOD_SOURCE,
    DelegatedLoop,
    claude_mod_enabled,
)
from fluid.events import ErrorEvent, TurnComplete, UserTurn
from fluid.events.model import Interrupted
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)


# ─── The switch ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("value", "enabled"), [
    (None, True), ("1", True), ("true", True), (" On ", True), ("yes", True),
    ("0", False), ("false", False), ("off", False), ("no", False), ("", False),
])
def test_the_switch_reads_fluid_claude_mod(value, enabled) -> None:
    env = {} if value is None else {"FLUID_CLAUDE_MOD": value}
    assert claude_mod_enabled(env) is enabled


def test_the_loop_reads_the_switch_unless_the_host_decides(monkeypatch) -> None:
    monkeypatch.setenv("FLUID_CLAUDE_MOD", "false")
    assert DelegatedLoop(store=MemoryStore(), model="m").claude_mod is False
    assert DelegatedLoop(store=MemoryStore(), model="m", claude_mod=True).claude_mod is True
    monkeypatch.delenv("FLUID_CLAUDE_MOD")
    assert DelegatedLoop(store=MemoryStore(), model="m").claude_mod is True
    assert DelegatedLoop(store=MemoryStore(), model="m", claude_mod=False).claude_mod is False


# ─── What ships ──────────────────────────────────────────────────────────────


def test_the_shipped_mod_is_a_complete_plugin() -> None:
    root = Path(CLAUDE_MOD_SOURCE)
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert f"{manifest['name']}@inline" == CLAUDE_MOD_PLUGIN
    hooks = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert hooks == {"modules": ["./register.ts"]}
    for name in ("register.ts", "steer.ts"):
        assert (root / "hooks" / name).is_file()
    # Nothing the CLI generates is ever committed into the shipped folder.
    assert not (root / ".claude-plugin" / "types").exists()
    assert not (root / "tsconfig.json").exists()


def test_the_mod_never_asks_for_credentials() -> None:
    source = "\n".join(
        p.read_text(encoding="utf-8") for p in (Path(CLAUDE_MOD_SOURCE) / "hooks").glob("*.ts")
    )
    for call in ("$.session.authorize", "$.http", "$.env", "$.process", "$.fs", "$.model"):
        assert call not in source


# ─── The gate and the argv ───────────────────────────────────────────────────


def _bare_loop(tmp_path: Path, version: str | None, **kwargs: Any) -> DelegatedLoop:
    loop = DelegatedLoop(store=MemoryStore(), model="claude-fake", **kwargs)
    loop.cli_version = version
    session_dir = tmp_path / "session"
    session_dir.mkdir(exist_ok=True)
    loop._tmpdir = str(session_dir)
    return loop


@pytest.mark.parametrize(("version", "state"), [
    (None, "skipped: CLI version unknown"),
    ("not a version", "skipped: CLI version unknown"),
    ("2.1.286 (Claude Code)", f"skipped: CLI 2.1.286 (Claude Code) is older than {CLAUDE_MOD_SINCE}"),
    ("2.1.287-beta.1 (Claude Code)", "skipped: CLI 2.1.287-beta.1 (Claude Code) is older than 2.1.287"),
])
def test_a_cli_without_mods_gets_the_old_argv(tmp_path, version, state) -> None:
    loop = _bare_loop(tmp_path, version, claude_mod=True)
    argv = loop._build_argv()
    assert "--plugin-dir" not in argv
    assert loop._claude_mod_state == state
    assert loop._claude_mod_dir is None
    assert not (tmp_path / "session" / "claude-mod").exists()


@pytest.mark.parametrize("version", ["2.1.287 (Claude Code)", "2.1.288", "2.2.0 (Claude Code)"])
def test_a_cli_with_mods_gets_a_session_copy_of_the_mod(tmp_path, version) -> None:
    loop = _bare_loop(tmp_path, version, claude_mod=True)
    argv = loop._build_argv()
    staged = tmp_path / "session" / "claude-mod"
    assert argv[argv.index("--plugin-dir") + 1] == str(staged)
    assert argv.count("--plugin-dir") == 1
    assert loop._claude_mod_state == "requested"
    # The CLI writes into the folder it loads, so it gets a copy, never the
    # shipped tree, and the copy carries only what the CLI loads.
    assert Path(loop._claude_mod_dir) != Path(CLAUDE_MOD_SOURCE)
    assert (staged / ".claude-plugin" / "plugin.json").is_file()
    assert (staged / "hooks" / "hooks.json").is_file()
    assert (staged / "hooks" / "register.ts").is_file()
    assert (staged / "hooks" / "steer.ts").is_file()
    assert not (staged / "tests").exists()


def test_the_mod_flag_is_the_only_difference(tmp_path) -> None:
    on = _bare_loop(tmp_path, "2.1.287 (Claude Code)", claude_mod=True)
    off = _bare_loop(tmp_path, "2.1.287 (Claude Code)", claude_mod=False)
    with_mod, without = on._build_argv(), off._build_argv()
    assert off._claude_mod_state == "off"
    assert "--plugin-dir" not in without
    i = with_mod.index("--plugin-dir")
    assert with_mod[:i] + with_mod[i + 2:] == without


def test_partial_messages_still_ride_after_the_mod(tmp_path) -> None:
    loop = _bare_loop(tmp_path, "2.1.287", claude_mod=True, partial=True)
    argv = loop._build_argv()
    assert argv[-1] == "--include-partial-messages"
    assert argv.index("--plugin-dir") < argv.index("--include-partial-messages")


def test_a_respawn_reuses_the_copy(tmp_path) -> None:
    loop = _bare_loop(tmp_path, "2.1.287", claude_mod=True)
    loop._build_argv()
    marker = tmp_path / "session" / "claude-mod" / ".claude-plugin" / "types" / "written-by-cli.d.ts"
    marker.parent.mkdir(parents=True)
    marker.write_text("// the CLI's own files", encoding="utf-8")
    loop._build_argv()
    assert marker.exists()


def test_a_copy_that_fails_falls_back_to_the_old_argv(tmp_path, monkeypatch, caplog) -> None:
    def refuse(tmpdir: str) -> str:
        raise PermissionError("read-only scratch")

    monkeypatch.setattr(delegated_mod, "_stage_claude_mod", refuse)
    caplog.set_level(logging.WARNING, logger="fluid.engine.delegated")
    loop = _bare_loop(tmp_path, "2.1.287", claude_mod=True)
    argv = loop._build_argv()
    assert "--plugin-dir" not in argv
    assert loop._claude_mod_state == "skipped: copy failed"
    assert any("runs without it" in r.getMessage() for r in caplog.records)


# ─── Against the scripted fake CLI ───────────────────────────────────────────


def _loop(tmp_path: Path, version: str, *, init: dict | None = None, **kwargs: Any):
    script = tmp_path / "script.json"
    script.write_text(json.dumps({"version": f"{version} (Claude Code)", "init": init or {}}),
                      encoding="utf-8")
    record = tmp_path / "record.jsonl"
    loop = DelegatedLoop(
        store=MemoryStore(), model="claude-fake",
        claude_cmd=[sys.executable, str(FAKE)],
        env={"FAKE_CLAUDE_SCRIPT": str(script), "FAKE_CLAUDE_RECORD": str(record)},
        **kwargs,
    )
    return loop, record


async def _turn(loop: DelegatedLoop) -> None:
    if loop._session_id is None:
        loop._session_id = (await loop.store.create_session()).session_id
    sid = loop._session_id
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


def _spawn_argv(record: Path) -> list[str]:
    for line in record.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("kind") == "start" or "argv" in entry:
            return entry["argv"]
    raise AssertionError("the fake CLI recorded no start")


async def test_a_mod_capable_cli_loads_the_mod_and_the_copy_goes_with_the_session(
    tmp_path, caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    loop, record = _loop(tmp_path, "2.1.287", claude_mod=True)
    try:
        await _turn(loop)
        staged = loop._claude_mod_dir
        assert staged is not None and os.path.isdir(staged)
        argv = _spawn_argv(record)
        assert argv[argv.index("--plugin-dir") + 1] == staged
        assert loop._claude_mod_listed is True
        messages = [r.getMessage() for r in caplog.records]
        assert any("claude_mod=requested" in m for m in messages)
        assert any(f"loaded AI Shuffle's mod ({CLAUDE_MOD_PLUGIN})" in m for m in messages)
    finally:
        await loop.aclose()
    assert not os.path.exists(staged)


async def test_a_mod_the_cli_does_not_list_is_reported_and_the_turn_still_runs(
    tmp_path, caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    loop, _record = _loop(tmp_path, "2.1.287", init={"drop_plugins": True}, claude_mod=True)
    try:
        await _turn(loop)
        assert loop._claude_mod_listed is False
        [warning] = [r.getMessage() for r in caplog.records
                     if r.levelno == logging.WARNING and "did not list" in r.getMessage()]
        assert "Path not found" in warning
    finally:
        await loop.aclose()


@pytest.mark.parametrize(("version", "claude_mod"), [("2.1.286", True), ("2.1.287", False)])
async def test_without_the_mod_the_spawn_is_unchanged(tmp_path, caplog, version, claude_mod) -> None:
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    loop, record = _loop(tmp_path, version, claude_mod=claude_mod)
    try:
        await _turn(loop)
        assert "--plugin-dir" not in _spawn_argv(record)
        assert loop._claude_mod_listed is None
        assert not any("AI Shuffle's mod" in r.getMessage() for r in caplog.records)
    finally:
        await loop.aclose()

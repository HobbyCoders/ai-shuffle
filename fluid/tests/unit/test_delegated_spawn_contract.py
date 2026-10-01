"""How DelegatedLoop starts the ``claude`` CLI: the argv contract.

* Bypass stays selectable mid-session (``--allow-dangerously-skip-permissions``).
  CLI 2.1.285 refuses ``set_permission_mode bypassPermissions`` from a process
  launched in another mode without it; the scripted fake enforces the same rule.
* ``--settings`` always carries the forced child env, so a settings file's env
  block cannot switch the bash-first steer back on.
* The system prompt rides a file, not the command line, which Windows caps at
  32,767 characters and an npm ``claude.cmd`` shim cuts at the first newline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import fluid.engine.delegated as delegated_mod
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.delegated_proto import FORCED_CHILD_ENV
from fluid.events import ErrorEvent, TurnComplete, UserTurn
from fluid.events.model import Interrupted
from fluid.events.ops import SetPermissionMode
from fluid.store.base import MemoryStore

FAKE = Path(__file__).parent.parent / "fixtures" / "fake_claude_script.py"
_TERMINALS = (TurnComplete, Interrupted, ErrorEvent)
_ALLOW_BYPASS = "--allow-dangerously-skip-permissions"


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _scripted(tmp_path: Path, cls: type[DelegatedLoop] = DelegatedLoop, script: dict | None = None,
              **kwargs: Any) -> tuple[DelegatedLoop, Path]:
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script or {}), encoding="utf-8")
    record = tmp_path / "record.jsonl"
    loop = cls(
        store=MemoryStore(), model="claude-fake",
        claude_cmd=[sys.executable, str(FAKE)],
        env={"FAKE_CLAUDE_SCRIPT": str(script_path), "FAKE_CLAUDE_RECORD": str(record)},
        **kwargs,
    )
    return loop, record


def _records(record: Path, kind: str) -> list[dict]:
    if not record.exists():
        return []
    rows = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r["kind"] == kind]


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
    return seen


# ─── Bypass is selectable after launch ──────────────────────────────────────


@pytest.mark.parametrize("mode", [None, "default", "acceptEdits", "auto", "bypassPermissions"])
def test_every_launch_can_switch_to_bypass_later(mode) -> None:
    argv = DelegatedLoop(store=MemoryStore(), model="m", permission_mode=mode)._build_argv()
    assert _ALLOW_BYPASS in argv
    assert _flag(argv, "--permission-mode") == (mode if mode not in (None, "plan") else "default")


@pytest.mark.parametrize("version,expected", [
    (None, True),
    ("2.1.285 (Claude Code)", True),
    ("2.1.0 (Claude Code)", True),
    ("not a version", True),
    ("2.0.99 (Claude Code)", False),
])
def test_the_bypass_flag_is_left_off_only_for_a_known_older_cli(version, expected) -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m")
    loop.cli_version = version
    assert (_ALLOW_BYPASS in loop._build_argv()) is expected


@pytest.mark.parametrize("env,expected", [
    ({}, False),
    ({"IS_SANDBOX": "1"}, True),
    ({"IS_SANDBOX": "yes"}, False),
    ({"CLAUDE_CODE_BUBBLEWRAP": "1"}, True),
])
def test_a_root_run_cli_outside_a_sandbox_gets_no_bypass_flag(monkeypatch, env, expected) -> None:
    # CLI 2.1.285 exits 1 on --allow-dangerously-skip-permissions as root
    # outside IS_SANDBOX=1 or bubblewrap, so the flag would stop every session.
    monkeypatch.setattr(delegated_mod, "_ON_WINDOWS", False)
    monkeypatch.setattr(delegated_mod.os, "getuid", lambda: 0, raising=False)
    for name in ("IS_SANDBOX", "CLAUDE_CODE_BUBBLEWRAP"):
        monkeypatch.delenv(name, raising=False)
    loop = DelegatedLoop(store=MemoryStore(), model="m", permission_mode="default", env=env)
    argv = loop._build_argv()
    assert (_ALLOW_BYPASS in argv) is expected
    assert _flag(argv, "--permission-mode") == "default"


def test_a_non_root_posix_cli_keeps_the_bypass_flag(monkeypatch) -> None:
    monkeypatch.setattr(delegated_mod, "_ON_WINDOWS", False)
    monkeypatch.setattr(delegated_mod.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    assert _ALLOW_BYPASS in DelegatedLoop(store=MemoryStore(), model="m")._build_argv()


async def test_a_session_started_in_ask_mode_switches_to_bypass(tmp_path) -> None:
    loop, record = _scripted(tmp_path, permission_mode="default")
    try:
        await _turn(loop)
        await loop.submit(SetPermissionMode(mode="bypassPermissions"))
        assert loop.permission_mode == "bypassPermissions"
        applied = _records(record, "applied")
        assert applied == [{"kind": "applied", "subtype": "set_permission_mode",
                            "value": "bypassPermissions"}]
        [start] = _records(record, "start")
        assert _ALLOW_BYPASS in start["argv"]
        assert start["argv"][start["argv"].index("--permission-mode") + 1] == "default"
    finally:
        await loop.aclose()


class _NoBypassFlag(DelegatedLoop):
    """What the loop spawned before the fix: no way into Bypass later."""

    def _build_argv(self) -> list[str]:
        return [a for a in super()._build_argv() if a != _ALLOW_BYPASS]


async def test_without_the_flag_the_cli_refuses_the_switch(tmp_path) -> None:
    """Proves the fake enforces the CLI's rule, so the test above means something."""
    loop, record = _scripted(tmp_path, _NoBypassFlag, permission_mode="default")
    try:
        await _turn(loop)
        with pytest.raises(RuntimeError, match="bypass_not_launched"):
            await loop.submit(SetPermissionMode(mode="bypassPermissions"))
        assert loop.permission_mode == "default"
        assert _records(record, "applied") == []
        # Other switches still go through.
        await loop.submit(SetPermissionMode(mode="acceptEdits"))
        assert loop.permission_mode == "acceptEdits"
    finally:
        await loop.aclose()


# ─── --settings carries the forced env ──────────────────────────────────────


def _settings(loop: DelegatedLoop) -> dict:
    return json.loads(_flag(loop._build_argv(), "--settings"))


def test_settings_carry_the_forced_env_when_the_host_sets_none() -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m")
    assert _settings(loop) == {"env": FORCED_CHILD_ENV}
    assert _settings(loop)["env"]["CLAUDE_CODE_THRIFTY_SONIC"] == "0"


def test_settings_keep_the_hosts_keys_and_never_invent_fast_mode() -> None:
    fast = DelegatedLoop(store=MemoryStore(), model="m", settings='{"fastMode": true}')
    assert _settings(fast) == {"fastMode": True, "env": FORCED_CHILD_ENV}
    plain = DelegatedLoop(store=MemoryStore(), model="m", settings=None)
    assert "fastMode" not in _settings(plain)


def test_a_settings_env_block_cannot_undo_the_override() -> None:
    loop = DelegatedLoop(
        store=MemoryStore(), model="m",
        settings=json.dumps({"env": {"CLAUDE_CODE_THRIFTY_SONIC": "1", "KEEP": "me"}}),
    )
    assert _settings(loop)["env"] == {"KEEP": "me", **FORCED_CHILD_ENV}


def test_settings_are_the_same_bytes_on_every_spawn() -> None:
    loop = DelegatedLoop(
        store=MemoryStore(), model="m", settings='{"fastMode": true, "b": 1, "a": [1, 2]}',
    )
    first = _flag(loop._build_argv(), "--settings")
    second = _flag(loop._build_argv(), "--settings")
    assert first is not None and first.encode() == second.encode()
    assert " " not in first  # compact, deterministic separators


def test_settings_that_name_a_file_pass_unchanged(caplog) -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", settings="/etc/claude/settings.json")
    caplog.set_level("INFO", logger="fluid.engine.delegated")
    assert _flag(loop._build_argv(), "--settings") == "/etc/claude/settings.json"
    loop._build_argv()
    assert len([r for r in caplog.records if "not a JSON object" in r.getMessage()]) == 1


async def test_run_settings_keep_the_forced_env_in_payload_argv_and_state(tmp_path) -> None:
    loop, record = _scripted(tmp_path, settings=json.dumps({"env": {"SAFE": "keep"}}))
    try:
        await _turn(loop)
        await loop.set_run_settings(effort="high", fast=True)
        [sent] = [r["frame"]["request"] for r in _records(record, "stdin")
                  if (r["frame"].get("request") or {}).get("subtype") == "apply_flag_settings"]
        expected_env = {"SAFE": "keep", **FORCED_CHILD_ENV}
        assert sent["settings"]["env"] == expected_env
        assert sent["settings"]["fastMode"] is True and sent["settings"]["effortLevel"] == "high"
        assert json.loads(loop.settings)["env"] == expected_env
        assert _settings(loop)["env"] == expected_env
        assert _settings(loop)["fastMode"] is True
        assert loop._setting_request("apply_flag_settings")["settings"]["env"] == expected_env
        # The spawn that started this CLI already carried the override.
        [start] = _records(record, "start")
        spawned = json.loads(_flag(start["argv"], "--settings"))
        assert spawned["env"]["CLAUDE_CODE_THRIFTY_SONIC"] == "0"
    finally:
        await loop.aclose()


# ─── The system prompt rides a file ─────────────────────────────────────────


def _big_prompt() -> str:
    # Starts with a newline like the app's append; non-ASCII and CRLF-free.
    line = "Project rule: prefer Read, Grep and Edit over shell tools — ünïcode ✓.\n"
    return "\n" + line * (40_000 // len(line) + 1)


def test_a_long_append_rides_a_file_not_the_command_line() -> None:
    text = _big_prompt()
    assert len(text) > 40_000
    loop = DelegatedLoop(store=MemoryStore(), model="m", append_system_prompt=text)
    try:
        argv = loop._build_argv()
        assert "--append-system-prompt" not in argv and "--system-prompt-file" not in argv
        path = _flag(argv, "--append-system-prompt-file")
        assert path is not None
        assert max(len(a) for a in argv) < 8_000
        assert Path(path).read_bytes() == text.encode("utf-8")
    finally:
        _cleanup(loop)


def _cleanup(loop: DelegatedLoop) -> None:
    """What ``aclose`` does to the prompt file, for loops that never spawned."""
    if loop._prompt_file and os.path.exists(loop._prompt_file):
        os.unlink(loop._prompt_file)
    if loop._tmpdir and os.path.isdir(loop._tmpdir):
        os.rmdir(loop._tmpdir)


def test_a_replacing_system_prompt_uses_the_system_prompt_file_flag() -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", system="You are terse.\nReally.")
    try:
        argv = loop._build_argv()
        assert "--system-prompt" not in argv and "--append-system-prompt-file" not in argv
        assert Path(_flag(argv, "--system-prompt-file")).read_bytes() == b"You are terse.\nReally."
    finally:
        _cleanup(loop)


def test_a_known_older_cli_keeps_the_prompt_inline() -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", append_system_prompt="APPEND")
    loop.cli_version = "2.0.0 (Claude Code)"
    argv = loop._build_argv()
    assert _flag(argv, "--append-system-prompt") == "APPEND"
    assert "--append-system-prompt-file" not in argv
    assert loop._tmpdir is None


def test_the_prompt_file_is_rewritten_only_when_the_text_changes() -> None:
    loop = DelegatedLoop(store=MemoryStore(), model="m", append_system_prompt="one")
    try:
        path = Path(_flag(loop._build_argv(), "--append-system-prompt-file"))
        stamp = path.stat().st_mtime_ns
        os.utime(path, ns=(stamp - 10_000_000_000, stamp - 10_000_000_000))
        old = path.stat().st_mtime_ns
        assert Path(_flag(loop._build_argv(), "--append-system-prompt-file")) == path
        assert path.stat().st_mtime_ns == old  # same text: not rewritten
        loop.append_system_prompt = "two"
        assert Path(_flag(loop._build_argv(), "--append-system-prompt-file")) == path
        assert path.read_bytes() == b"two"
    finally:
        _cleanup(loop)


class _RecordingLoop(DelegatedLoop):
    """Keeps the bytes of the prompt file as each spawn saw it."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.prompt_bytes: list[bytes] = []

    def _build_argv(self) -> list[str]:
        argv = super()._build_argv()
        self.prompt_bytes.append(Path(_flag(argv, "--append-system-prompt-file")).read_bytes())
        return argv


async def test_every_spawn_reads_the_same_prompt_bytes_and_aclose_removes_them(tmp_path) -> None:
    text = _big_prompt()
    loop, record = _scripted(tmp_path, _RecordingLoop, append_system_prompt=text)
    try:
        await _turn(loop, "first")
        await loop._stop_process()
        await _turn(loop, "second")
        starts = _records(record, "start")
        assert len(starts) == 2
        assert all(max(len(a) for a in s["argv"]) < 8_000 for s in starts)
        assert loop.prompt_bytes == [text.encode("utf-8")] * 2
        path = loop._prompt_file
        tmpdir = loop._tmpdir
        assert path is not None and os.path.dirname(path) == tmpdir
    finally:
        await loop.aclose()
    assert not os.path.exists(path)
    assert not os.path.exists(tmpdir)


async def test_the_bridge_config_and_prompt_share_one_directory(tmp_path) -> None:
    from fluid.tools.registry import ToolOutcome, ToolRegistry, ToolSpec

    async def handler(input, ctx):
        return ToolOutcome(text="ok")

    registry = ToolRegistry()
    registry.register_builtin(ToolSpec(name="Recall", description="r", input_schema={}), handler)
    loop, _record = _scripted(tmp_path, registry=registry, append_system_prompt="APPEND")
    try:
        await _turn(loop)
        assert os.path.dirname(loop._mcp_config_path) == loop._tmpdir
        assert os.path.dirname(loop._prompt_file) == loop._tmpdir
    finally:
        await loop.aclose()


def test_the_cli_older_than_gate() -> None:
    older = delegated_mod._cli_older_than
    assert older("2.0.9", "2.1.0") and not older("2.1.0", "2.1.0")
    assert not older(None, "2.1.0") and not older("garbage", "2.1.0")

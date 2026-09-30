"""A delegated connect hands fluid's conversation to the claude CLI.

The CLI only receives the new user frame each turn; its history is whatever
``--resume <handle>`` loads. When the session has history the CLI cannot
load (it ran on another provider, the handle's transcript is on another
worker, or turns ran elsewhere since), ``HarnessAdapter.connect`` writes
fluid's path as a fresh CLI transcript and resumes that instead.

No CLI is spawned: ``DelegatedLoop`` is replaced with a kwargs-capturing fake.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, List, Optional

import pytest
from fluid.messages.content import Message, Text
from fluid.store.base import MemoryStore, SessionNode

import app.core.provider.harness_adapter as ha
from app.core.provider import fluid_transcript
from app.core.provider.fluid_transcript import ReseedResult, cli_project_dir_name
from app.core.provider.types import SessionOptions

SID = "ses_pelican0001"


class _CapturingLoop:
    """Records the kwargs HarnessAdapter builds DelegatedLoop with."""

    captured: dict = {}

    def __init__(self, **kwargs: Any) -> None:
        type(self).captured = dict(kwargs)
        self.store = kwargs["store"]
        self._session_id = kwargs.get("session_id")

    async def _ensure_session(self):
        meta = await self.store.get_session(self._session_id)
        if meta is None:
            meta = await self.store.create_session(self._session_id)
        return meta

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _node(node_id: str, parent: Optional[str], role: str, text: str) -> SessionNode:
    return SessionNode(
        node_id=node_id, parent_id=parent,
        message=Message(role=role, content=[Text(text)]),
    )


PELICAN = [
    ("n1", None, "user", "My codename is PELICAN"),
    ("n2", "n1", "assistant", "Noted: PELICAN"),
]
EXTRA_NATIVE_TURN = [
    ("n3", "n2", "user", "And my favourite colour is teal"),
    ("n4", "n3", "assistant", "Teal, noted"),
]


async def _seed(store, nodes, handle: Optional[str] = None) -> None:
    await store.create_session(SID)
    for node_id, parent, role, text in nodes:
        await store.append(SID, _node(node_id, parent, role, text))
    if handle:
        await store.set_cli_session_id(SID, handle)


def _options(tmp_path: Path, **kw: Any) -> SessionOptions:
    workspace = tmp_path / "work_space.dir"
    workspace.mkdir(exist_ok=True)
    kw.setdefault("resume_session_id", SID)
    return SessionOptions(
        model="opus",
        cwd=str(workspace),
        env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")},
        **kw,
    )


def _projects(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "projects"


def _write_cli_jsonl(tmp_path: Path, handle: str, entries: List[dict]) -> Path:
    path = _projects(tmp_path) / "some-other-project" / f"{handle}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8",
    )
    return path


def _user(text: Any, **extra: Any) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **extra}


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {
        "role": "assistant", "content": [{"type": "text", "text": text}],
    }}


async def _connect(monkeypatch, store, options) -> dict:
    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _o: store))
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude-not-spawned"])
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    assert adapter.provider_kind == "delegated"
    return _CapturingLoop.captured


def _fake_reseed(monkeypatch, result: ReseedResult) -> list:
    calls: list = []

    def fake(*args: Any, **kwargs: Any) -> ReseedResult:
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", fake)
    return calls


def _no_reseed(monkeypatch) -> None:
    def boom(*args: Any, **kwargs: Any):
        raise AssertionError("connect re-seeded a transcript that was current")

    monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", boom)


# ─── a session that ran on another provider ─────────────────────────────────


@pytest.mark.asyncio
async def test_native_history_is_written_for_the_cli_and_resumed(mock_db, monkeypatch, tmp_path):
    """Nodes from a native provider and no handle: the CLI resumes a
    transcript holding both earlier turns (the real re-seed, over the DB)."""
    from app.core.provider.fluid_pg_store import PgFluidStore

    store = PgFluidStore()
    await _seed(store, PELICAN)
    calls: list = []
    real = fluid_transcript.reseed_delegated_cli_session

    def spy(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", spy)

    options = _options(tmp_path)
    captured = await _connect(monkeypatch, store, options)

    handle = captured["cli_session_id"]
    assert handle and handle != SID
    assert len(calls) == 1 and calls[0][1] == {"force": True}
    assert calls[0][0][0] == SID
    assert fluid_transcript.get_cli_session_id(SID) == handle

    transcript = (
        _projects(tmp_path) / cli_project_dir_name(os.path.abspath(options.cwd))
        / f"{handle}.jsonl"
    )
    lines = [json.loads(line) for line in transcript.read_text(encoding="utf-8").splitlines()]
    assert [line["type"] for line in lines] == ["user", "assistant"]
    assert lines[0]["message"]["content"] == "My codename is PELICAN"
    assert lines[1]["message"]["content"] == [{"type": "text", "text": "Noted: PELICAN"}]
    assert {line["sessionId"] for line in lines} == {handle}


@pytest.mark.asyncio
async def test_session_id_alone_still_resolves_the_handle(monkeypatch, tmp_path):
    """A connect naming the fluid session by ``session_id`` (no resume id)
    gets the same treatment."""
    store = MemoryStore()
    await _seed(store, PELICAN)
    calls = _fake_reseed(monkeypatch, ReseedResult(status="reseeded", cli_session_id="new-uuid"))

    captured = await _connect(
        monkeypatch, store, _options(tmp_path, resume_session_id=None, session_id=SID),
    )
    assert captured["cli_session_id"] == "new-uuid"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_session_without_a_finished_turn_is_not_reseeded(monkeypatch, tmp_path):
    store = MemoryStore()
    await _seed(store, PELICAN[:1])
    _no_reseed(monkeypatch)

    captured = await _connect(monkeypatch, store, _options(tmp_path))
    assert captured["cli_session_id"] is None


# ─── a stored handle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_handle_whose_transcript_is_not_on_this_worker_is_reseeded(monkeypatch, tmp_path):
    store = MemoryStore()
    await _seed(store, PELICAN, handle="old-handle")
    _projects(tmp_path).mkdir(parents=True)
    calls = _fake_reseed(monkeypatch, ReseedResult(status="reseeded", cli_session_id="new-uuid"))

    captured = await _connect(monkeypatch, store, _options(tmp_path))
    assert captured["cli_session_id"] == "new-uuid"
    assert calls[0][1] == {"force": True}
    # Written under the root the CLI will read (CLAUDE_CONFIG_DIR in its env).
    assert Path(calls[0][0][2]) == _projects(tmp_path)
    assert (await store.get_session(SID)).cli_session_id == "new-uuid"


@pytest.mark.asyncio
async def test_a_current_transcript_is_resumed_as_is(monkeypatch, tmp_path):
    store = MemoryStore()
    await _seed(store, PELICAN, handle="cli-handle")
    _write_cli_jsonl(tmp_path, "cli-handle", [
        _user([{"type": "text", "text": "My codename is PELICAN"}]),
        _assistant("Noted: PELICAN"),
    ])
    _no_reseed(monkeypatch)

    captured = await _connect(monkeypatch, store, _options(tmp_path))
    assert captured["cli_session_id"] == "cli-handle"


@pytest.mark.asyncio
async def test_steers_and_interrupt_markers_after_the_prompt_are_not_staleness(monkeypatch, tmp_path):
    """A mid-turn steer and the CLI's interrupt marker are user text entries
    written after the turn's prompt; fluid keeps neither as a prompt node."""
    store = MemoryStore()
    await _seed(store, PELICAN, handle="cli-handle")
    _write_cli_jsonl(tmp_path, "cli-handle", [
        _user("My codename is PELICAN"),
        _assistant("Noted"),
        _user([{"type": "text", "text": "also mention the date"}]),
        _user([{"type": "text", "text": "[Request interrupted by user]"}]),
        _user("<meta caveat>", isMeta=True),
    ])
    _no_reseed(monkeypatch)

    captured = await _connect(monkeypatch, store, _options(tmp_path))
    assert captured["cli_session_id"] == "cli-handle"


@pytest.mark.asyncio
async def test_a_transcript_missing_a_later_native_turn_is_reseeded(monkeypatch, tmp_path):
    """Claude, then another provider, then Claude again: the stored handle's
    transcript ends before the other provider's turn."""
    store = MemoryStore()
    await _seed(store, PELICAN + EXTRA_NATIVE_TURN, handle="cli-handle")
    _write_cli_jsonl(tmp_path, "cli-handle", [
        _user("My codename is PELICAN"),
        _assistant("Noted: PELICAN"),
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "x"},
        ]}},
    ])
    calls = _fake_reseed(monkeypatch, ReseedResult(status="reseeded", cli_session_id="new-uuid"))

    captured = await _connect(monkeypatch, store, _options(tmp_path))
    assert captured["cli_session_id"] == "new-uuid"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_failed_reseed_keeps_the_stored_handle(monkeypatch, tmp_path):
    store = MemoryStore()
    await _seed(store, PELICAN, handle="old-handle")
    _fake_reseed(monkeypatch, ReseedResult(status="failed", error="disk full"))

    captured = await _connect(monkeypatch, store, _options(tmp_path))
    assert captured["cli_session_id"] == "old-handle"


@pytest.mark.asyncio
async def test_a_raising_reseed_keeps_the_stored_handle(monkeypatch, tmp_path):
    store = MemoryStore()
    await _seed(store, PELICAN, handle="old-handle")

    def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("db down")

    monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", boom)
    captured = await _connect(monkeypatch, store, _options(tmp_path))
    assert captured["cli_session_id"] == "old-handle"


# ─── the CLI's project dir encoding ──────────────────────────────────────────


def test_cli_project_dir_name_matches_the_cli():
    assert (
        cli_project_dir_name("D:\\Projects\\ai-shuffle\\.claude\\worktrees\\a_b")
        == "D--Projects-ai-shuffle--claude-worktrees-a-b"
    )
    assert cli_project_dir_name("/workspace/new folder") == "-workspace-new-folder"


@pytest.fixture
def _short_dir_limit(monkeypatch):
    """The CLI cuts names at 200 characters; a 20-character cut exercises the
    same code without paths too long for Windows."""
    monkeypatch.setattr(fluid_transcript, "CLI_PROJECT_DIR_MAX", 20)


def test_a_long_cwd_reuses_the_cli_hashed_project_dir(tmp_path, _short_dir_limit):
    long_cwd = "/w/" + "x" * 30
    name = cli_project_dir_name(long_cwd)
    hashed = tmp_path / (name[:20] + "-r2jn9i")
    hashed.mkdir()

    path = fluid_transcript._write_cli_transcript(
        [{"type": "user"}], "cli-uuid", long_cwd, tmp_path,
    )
    assert path.parent == hashed


def test_a_long_cwd_without_a_cli_dir_keeps_the_old_name(tmp_path, _short_dir_limit):
    from app.core.jsonl_parser import get_project_dir_name

    long_cwd = "/w/" + "x" * 30
    path = fluid_transcript._write_cli_transcript(
        [{"type": "user"}], "cli-uuid", long_cwd, tmp_path,
    )
    assert path.parent.name == get_project_dir_name(long_cwd)

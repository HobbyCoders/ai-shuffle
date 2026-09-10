"""The cached prompt prefix must not move while a session runs.

Providers key their prompt cache on the request prefix — system block, then
tool schemas, then history in order. Anything volatile up there re-bills the
whole prefix on the next turn, so the date, the branch pair and the continual
learner's ``# Memory`` snapshot ride the turn's user message instead. These
tests pin both halves of that bargain: the system block is byte-identical
across turns that changed all three, and the model still reads all three.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.core.environment import (
    TURN_ENV_HEADER,
    generate_environment_details,
    generate_turn_environment_note,
)
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─── the app's <env> block ───────────────────────────────────────────────────


def test_env_block_drops_the_facts_that_change_mid_session(tmp_path):
    block = generate_environment_details(
        str(tmp_path),
        execution_mode="worktree",
        worktree_info={"branch": "feature/x", "base_branch": "main"},
        include_volatile=False,
    )
    assert "Working directory:" in block
    assert "Execution mode: worktree" in block
    assert "Today's date" not in block
    assert "Current branch" not in block
    assert "Base branch" not in block


def test_env_block_is_identical_across_a_date_and_branch_change(tmp_path, monkeypatch):
    """The two turns a cache miss was observed between: a new day, a new
    branch, same session."""
    monkeypatch.setattr("app.core.environment._today", lambda: "2026-09-04")
    turn_one = generate_environment_details(
        str(tmp_path),
        execution_mode="worktree",
        worktree_info={"branch": "claude/one", "base_branch": "main"},
        include_volatile=False,
    )
    monkeypatch.setattr("app.core.environment._today", lambda: "2026-09-05")
    turn_two = generate_environment_details(
        str(tmp_path),
        execution_mode="worktree",
        worktree_info={"branch": "claude/two", "base_branch": "develop"},
        include_volatile=False,
    )
    assert _hash(turn_one) == _hash(turn_two)


def test_turn_note_carries_the_date_and_the_branch_pair(monkeypatch):
    monkeypatch.setattr("app.core.environment._today", lambda: "2026-09-04")
    note = generate_turn_environment_note(
        execution_mode="worktree",
        worktree_info={"branch": "claude/one", "base_branch": "main"},
    )
    assert TURN_ENV_HEADER in note
    assert "Today's date: 2026-09-04" in note
    assert "Current branch: claude/one" in note
    assert "Base branch: main" in note


def test_turn_note_outside_worktree_mode_is_just_the_date(monkeypatch):
    monkeypatch.setattr("app.core.environment._today", lambda: "2026-09-04")
    note = generate_turn_environment_note()
    assert "Today's date: 2026-09-04" in note
    assert "Current branch" not in note


# ─── the compiled system prompt ──────────────────────────────────────────────


@pytest.fixture
def memory_session_id():
    from app.db.sessions import create_session

    session_id = str(uuid.uuid4())
    create_session(session_id, "default", derive_provider_from_profile=False)
    return session_id


@pytest.fixture
def memory_store(tmp_path, monkeypatch, memory_session_id):
    """A real session-owned store, with no dependence on a learning model."""
    from app.core.config import settings
    from app.core.fluid_memory import session_memory

    monkeypatch.setattr(settings, "fluid_skills_dir", str(tmp_path / "skills"))
    monkeypatch.setattr(settings, "fluid_continual", False)
    # Exercise authenticated session lookup in the isolated test database.
    # Do not bypass owner resolution.
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    store, _ = session_memory(memory_session_id, str(tmp_path))
    return store


def test_compiled_system_survives_another_sessions_memory_write(
    tmp_path, memory_store, memory_session_id
):
    """The memories file is install-global: any session's Remember used to
    rewrite the ``# Memory`` section of every other session's prompt, and with
    it their cached prefix."""
    options = SessionOptions(model="fake-model", cwd=str(tmp_path), extra={"app_session_id": memory_session_id})
    memory_store.save_memory("the deploy script lives in ops/deploy.sh")

    first = HarnessAdapter._compile_prompt_sections(
        "AGENT PROMPT", None, options, native=True
    )
    memory_store.save_memory("the staging database is read-only")
    second = HarnessAdapter._compile_prompt_sections(
        "AGENT PROMPT", None, options, native=True
    )

    assert first is not None and second is not None
    assert _hash(first.text) == _hash(second.text)
    # ``kind="memory"`` is also how instruction files (AGENTS.md) are labeled,
    # so the learner's snapshot is identified by its own header.
    assert "# Memory\n" not in first.text
    assert not [s for s in first.sections if s.text.startswith("# Memory\n")]


@pytest.mark.parametrize("delegated", [False, True])
def test_memory_snapshot_rides_the_turn_instead(tmp_path, memory_store, memory_session_id, delegated):
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="fake-model", cwd=str(tmp_path), extra={"app_session_id": memory_session_id})
    adapter._is_delegated = delegated

    assert adapter._memory_snapshot() is None  # empty store costs nothing

    memory_store.save_memory("the deploy script lives in ops/deploy.sh")
    snapshot = adapter._memory_snapshot()
    assert snapshot is not None
    assert "# Memory" in snapshot
    assert "ops/deploy.sh" in snapshot


@pytest.mark.parametrize("delegated", [False, True])
def test_memory_snapshot_fails_closed_without_owner_identity(tmp_path, memory_store, delegated):
    """An unstamped adapter cannot read the installation owner's facts."""
    memory_store.save_memory("the deploy script lives in ops/deploy.sh")
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="fake-model", cwd=str(tmp_path))
    adapter._is_delegated = delegated
    assert adapter._memory_snapshot() is None


# ─── what actually reaches the wire ──────────────────────────────────────────


class _RecordingEngine:
    def __init__(self):
        self.ops = []

    async def submit(self, op):
        self.ops.append(op)


@pytest.mark.asyncio
async def test_send_query_appends_the_volatile_context(
    tmp_path, memory_store, memory_session_id, monkeypatch
):
    monkeypatch.setattr("app.core.environment._today", lambda: "2026-09-04")
    memory_store.save_memory("the deploy script lives in ops/deploy.sh")

    adapter = HarnessAdapter()
    adapter._options = SessionOptions(
        model="fake-model",
        cwd=str(tmp_path),
        extra={
            "app_session_id": memory_session_id,
            "turn_env": {
                "execution_mode": "worktree",
                "worktree_info": {"branch": "claude/one", "base_branch": "main"},
            }
        },
    )
    adapter._is_delegated = False
    engine = _RecordingEngine()
    adapter._engine = engine

    await adapter.send_query("do the thing")

    sent = engine.ops[0].content
    assert sent.startswith("do the thing")
    assert "Today's date: 2026-09-04" in sent
    assert "Current branch: claude/one" in sent
    assert "ops/deploy.sh" in sent


@pytest.mark.asyncio
async def test_send_query_is_unchanged_without_an_app_env_block(tmp_path):
    """A profile whose prompt never carried the app's <env> block does not
    start getting one on its user messages."""
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="fake-model", cwd=str(tmp_path))
    adapter._is_delegated = True
    engine = _RecordingEngine()
    adapter._engine = engine

    await adapter.send_query("do the thing")

    assert engine.ops[0].content == "do the thing"

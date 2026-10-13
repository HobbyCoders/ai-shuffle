"""Away mode never touches the provider's cached prefix.

The instruction rides the tail of each new user message (the turn's context
note), never the system prompt or the tool schemas, and switching Away on or
off is a database write that does not reconnect the session. So across turns
with Away off, on and off again, every request's system prompt and tools are
byte-identical, and each request's messages extend the previous request's
messages unchanged (an earlier user message keeps exactly the text it was sent
with). Both legs are covered: the native loop against the fake provider
server in Anthropic and chat-completions formats, and the delegated Claude
Code leg against the fake CLI.
"""
from __future__ import annotations

import sys

import pytest

import app.core.provider.harness_adapter as ha
from app.core import away_mode
from app.db import database
from tests.core import test_native_scripted_provider_app as native_fixture
from tests.core.test_delegated_leg import FAKE, _ArgvLoop, _preset_options, _run_turn
from tests.core.test_native_scripted_provider_app import (  # noqa: F401 — fixtures
    Chat,
    Messages,
    NativeRun,
    _isolated_home,
)

provider = native_fixture.provider

# off → on → on → off → off: cancellation only on the first delivered off turn.
SEQUENCE = (False, True, True, False, False)


@pytest.fixture
def away_state(mock_db, monkeypatch):
    """Use the real flag writer and prompt producer, not seeded receipts."""
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    database.create_session("away-chat", "agent")
    monkeypatch.setattr(away_mode, "assistant_work", lambda _sid: False)
    return "away-chat"


def _content(messages: list) -> list:
    """Messages without Anthropic's rolling ``cache_control`` breakpoint.

    The client marks the newest message as the cache breakpoint on every call,
    so the marker moves forward each turn by design; it is not message content.
    """
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "cache_control"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(messages)


def _user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["anthropic", "openrouter"])
async def test_native_prefix_is_identical_with_away_on_off_and_across_a_toggle(
    monkeypatch, tmp_path, provider, away_state, kind
):
    reply = Messages if kind == "anthropic" else Chat
    provider.script(*[reply.text(f"reply {n}") for n in range(len(SEQUENCE))])
    async with NativeRun(monkeypatch, tmp_path, provider, kind=kind) as run:
        run.options.extra["app_session_id"] = away_state
        engine = run.adapter.engine
        for n, away in enumerate(SEQUENCE):
            database.set_session_run_binding(away_state, away_mode=away)
            await run.turn(f"turn {n}")
            # A toggle never reconnects: the same engine serves every turn.
            assert run.adapter.engine is engine

    bodies = [request.body for request in provider.requests]
    assert len(bodies) == len(SEQUENCE)
    for earlier, later in zip(bodies, bodies[1:]):
        # The cached prefix: system prompt and tool schemas never change.
        assert later.get("system") == earlier.get("system")
        assert later.get("tools") == earlier.get("tools")
        # Earlier messages (and, for chat-completions, the system message at
        # their head) are resent byte for byte; only new turns are appended.
        assert _content(later["messages"][: len(earlier["messages"])]) == _content(earlier["messages"])
    for body, away in zip(bodies, SEQUENCE):
        newest = _user_text(body["messages"][-1])
        assert body["messages"][-1]["role"] == "user"
        # The agent still gets the instruction on every away turn, at the tail.
        assert ("The owner is away" in newest) is away
        assert ("The owner is back" in newest) is (body is bodies[3])
        assert "owner is away" not in str(body.get("system") or "").lower()
        assert "owner is away" not in str(body.get("tools") or "").lower()


@pytest.mark.asyncio
async def test_delegated_toggle_neither_respawns_the_cli_nor_changes_its_prompt(
    monkeypatch, tmp_path, away_state
):
    """Claude Code holds the conversation; its cached prefix is the system
    prompt fixed at spawn plus the history. Away rides the newest prompt, so a
    toggle must not spawn the CLI again (a new system prompt) or rewrite it."""
    monkeypatch.setattr(ha, "DelegatedLoop", _ArgvLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    _ArgvLoop.argvs = []
    sent: list[str] = []

    adapter = ha.HarnessAdapter()
    await adapter.connect(_preset_options(tmp_path, app_session_id=away_state))
    real_submit = adapter.engine.submit

    async def record(turn):
        content = getattr(turn, "content", None)
        if isinstance(content, str):
            sent.append(content)
        return await real_submit(turn)

    monkeypatch.setattr(adapter.engine, "submit", record)
    append = adapter.engine.append_system_prompt
    try:
        for n, away in enumerate(SEQUENCE):
            database.set_session_run_binding(away_state, away_mode=away)
            result = await _run_turn(adapter, f"turn {n}")
            assert not result.is_error
            assert adapter.engine.append_system_prompt == append
    finally:
        await adapter.disconnect()

    assert len(_ArgvLoop.argvs) == 1, "a toggle must not respawn the CLI"
    assert "owner is away" not in append.lower()
    assert [("The owner is away" in text) for text in sent] == list(SEQUENCE)
    assert [("The owner is back" in text) for text in sent] == [False, False, False, True, False]
    for n, text in enumerate(sent):
        assert text.startswith(f"turn {n}")

@pytest.mark.asyncio
async def test_native_compaction_and_preview_do_not_consume_owner_back(
    monkeypatch, tmp_path, provider, away_state
):
    provider.script(Messages.text("away reply"), Messages.text("back reply"), Messages.text("next"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra["app_session_id"] = away_state
        database.set_session_run_binding(away_state, away_mode=True)
        await run.turn("work")
        database.set_session_run_binding(away_state, away_mode=False)
        # Duplicate diagnostic reads must remain pure, as must /compact.
        for _ in range(2):
            assert away_mode.OWNER_BACK_NOTE in run.adapter._turn_context_note()
        await run.turn("/compact")
        assert away_mode.OWNER_BACK_NOTE in run.adapter._turn_context_note()
        await run.turn("owner returned")
        assert away_mode.turn_note(away_state) is None
        await run.turn("another turn")
    newest = [_user_text(r.body["messages"][-1]) for r in provider.requests]
    assert len(newest) == 3, "/compact must not issue a model call"
    assert away_mode.OWNER_BACK_NOTE in newest[1]
    assert away_mode.OWNER_BACK_NOTE not in newest[2]


@pytest.mark.asyncio
async def test_native_owner_back_survives_adapter_restart_and_compacted_history(
    monkeypatch, tmp_path, provider, away_state
):
    from fluid.messages import Message, Text
    from fluid.store.base import MemoryStore, SessionNode

    store = MemoryStore()
    monkeypatch.setattr(ha.HarnessAdapter, "_test_memory_store_factory", staticmethod(lambda: store))
    provider.script(Messages.text("away reply"), Messages.text("owner reply"), Messages.text("next"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra["app_session_id"] = away_state
        database.set_session_run_binding(away_state, away_mode=True)
        await run.turn("work")
        sid = run.adapter.engine.session_id
        # Simulate the problematic summarizer output, after exercising the real
        # Away prompt producer. The model's visible path no longer has its note.
        await store.append(sid, SessionNode(
            node_id="compacted", parent_id=None,
            message=Message(role="user", content=[Text(
                "Compaction summary: standing owner rules: never ask questions; "
                "every reply ends with a Decisions made for you table."
            )]),
        ))
    database.set_session_run_binding(away_state, away_mode=False)
    # A fresh adapter has no in-memory mode history; the receipt is durable.
    async with NativeRun(monkeypatch, tmp_path, provider, resume_session_id=sid) as run:
        run.options.extra["app_session_id"] = away_state
        await run.turn("How do I send a report?")
        await run.turn("continue")
    body = provider.requests[1].body
    assert "standing owner rules" in _user_text(body["messages"][0])
    note = _user_text(body["messages"][-1])
    assert away_mode.OWNER_BACK_NOTE in note
    assert "supersedes every earlier Away" in note and "compaction summary" in note
    assert away_mode.OWNER_BACK_NOTE not in _user_text(provider.requests[2].body["messages"][-1])

@pytest.mark.asyncio
async def test_legacy_prompt_without_receipts_gets_cancellation_after_reconnect(
    monkeypatch, tmp_path, provider, away_state
):
    from app.core.provider.fluid_pg_store import PgFluidStore

    # PgFluidStore writes to the disposable SQLite shim via the real store API.
    # Disabling only the new receipt for the first turn simulates an upgrade
    # from the prior app version; the Away instruction itself is really produced.
    monkeypatch.setattr(ha.HarnessAdapter, "_test_memory_store_factory", PgFluidStore)
    provider.script(Messages.text("away reply"), Messages.text("back reply"), Messages.text("next"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra["app_session_id"] = away_state
        database.set_session_run_binding(away_state, away_mode=True)
        with monkeypatch.context() as old_version:
            old_version.setattr(away_mode, "record_context_delivery", lambda *_args: None)
            await run.turn("legacy work")
        sid = run.adapter.engine.session_id
        database.update_session(away_state, sdk_session_id=sid)
    database.set_session_run_binding(away_state, away_mode=False)
    async with NativeRun(monkeypatch, tmp_path, provider, resume_session_id=sid) as run:
        run.options.extra["app_session_id"] = away_state
        assert away_mode.OWNER_BACK_NOTE in run.adapter._turn_context_note()
        await run.turn("owner returned")
        await run.turn("next")
    assert away_mode.OWNER_BACK_NOTE in _user_text(provider.requests[1].body["messages"][-1])
    assert away_mode.OWNER_BACK_NOTE not in _user_text(provider.requests[2].body["messages"][-1])


@pytest.mark.asyncio
async def test_no_model_output_keeps_owner_back_pending_for_retry(
    monkeypatch, tmp_path, provider, away_state
):
    from tests.core.test_native_scripted_provider_app import _resume, fps

    provider.script(Messages.text("away reply"), fps.error_reply(400, "request refused"),
                    Messages.text("retry reply"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra["app_session_id"] = away_state
        database.set_session_run_binding(away_state, away_mode=True)
        await run.turn("work")
        database.set_session_run_binding(away_state, away_mode=False)
        failed = await run.turn("owner returned")
        assert failed[-1].is_error
        assert away_mode.OWNER_BACK_NOTE in _user_text(provider.requests[1].body["messages"][-1])
        assert away_mode.OWNER_BACK_NOTE in away_mode.turn_note(away_state)
        result = await _resume(run)
        assert not result[-1].is_error
        assert away_mode.OWNER_BACK_NOTE in _user_text(provider.requests[2].body["messages"][-1])
        assert away_mode.turn_note(away_state) is None


@pytest.mark.asyncio
async def test_failed_and_concurrent_native_submits_leave_cancellation_pending(
    monkeypatch, tmp_path, provider, away_state
):
    provider.script(Messages.text("away reply"), Messages.text("back reply"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra["app_session_id"] = away_state
        database.set_session_run_binding(away_state, away_mode=True)
        await run.turn("work")
        database.set_session_run_binding(away_state, away_mode=False)
        real_submit = run.adapter.engine.submit

        async def rejected(_op):
            raise RuntimeError("submit failed")

        with monkeypatch.context() as patcher:
            patcher.setattr(run.adapter.engine, "submit", rejected)
            with pytest.raises(RuntimeError, match="submit failed"):
                await run.adapter.send_query("retry me")
        assert away_mode.OWNER_BACK_NOTE in away_mode.turn_note(away_state)
        await run.adapter.send_query("owner returned")
        snapshot = run.adapter._pending_away_note
        # The real engine rejects a duplicate while the accepted turn runs.
        with pytest.raises(RuntimeError, match="already running"):
            await run.adapter.send_query("duplicate")
        assert run.adapter._pending_away_note == snapshot
        assert run.adapter.engine.submit == real_submit
        async for _message in run.adapter.receive_events():
            pass
        assert away_mode.turn_note(away_state) is None
    assert len(provider.requests) == 2
    assert away_mode.OWNER_BACK_NOTE in _user_text(provider.requests[-1].body["messages"][-1])


@pytest.mark.asyncio
async def test_native_retry_after_away_off_receives_cancellation(
    monkeypatch, tmp_path, provider, away_state
):
    from tests.core.test_native_scripted_provider_app import _resume, fps

    provider.script(Messages.text("away reply"), fps.error_reply(400, "request refused"),
                    Messages.text("retried"), Messages.text("next"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra["app_session_id"] = away_state
        database.set_session_run_binding(away_state, away_mode=True)
        await run.turn("work")
        failed = await run.turn("unfinished question")
        assert failed[-1].is_error
        database.set_session_run_binding(away_state, away_mode=False)
        result = await _resume(run)
        assert not result[-1].is_error
        assert away_mode.OWNER_BACK_NOTE in _user_text(provider.requests[2].body["messages"][-1])
        assert "unfinished question" in str(provider.requests[2].body["messages"])
        assert away_mode.turn_note(away_state) is None
        await run.turn("next")
        assert away_mode.OWNER_BACK_NOTE not in _user_text(provider.requests[3].body["messages"][-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True], ids=["memory-store", "pg-store"])
async def test_replayed_away_output_does_not_repeat_cancellation(
    monkeypatch, tmp_path, provider, away_state, persisted
):
    from app.core.provider.fluid_pg_store import PgFluidStore

    if persisted:
        monkeypatch.setattr(ha.HarnessAdapter, "_test_memory_store_factory", PgFluidStore)
    deliveries = []
    record = away_mode.record_context_delivery

    def observed(*args):
        deliveries.append(args)
        record(*args)

    monkeypatch.setattr(away_mode, "record_context_delivery", observed)
    provider.script(Messages.text("away reply"), Messages.text("still away"),
                    Messages.text("back reply"), Messages.text("next"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra["app_session_id"] = away_state
        database.update_session(away_state, sdk_session_id=run.adapter.engine.session_id)
        database.set_session_run_binding(away_state, away_mode=True)
        await run.turn("work")
        await run.turn("more work")
        away_deliveries = deliveries[:]
        assert len(away_deliveries) == 2
        database.set_session_run_binding(away_state, away_mode=False)
        await run.turn("owner returned")
        assert away_mode.turn_note(away_state) is None
        # Every observed Away turn needs a stable receipt, not just transitions.
        # Otherwise late output replay can append an old mode after cancellation.
        for delivery in away_deliveries:
            record(*delivery)
            assert away_mode.turn_note(away_state) is None
        await run.turn("next")
    assert away_mode.OWNER_BACK_NOTE in _user_text(provider.requests[2].body["messages"][-1])
    assert away_mode.OWNER_BACK_NOTE not in _user_text(provider.requests[3].body["messages"][-1])


@pytest.mark.asyncio
async def test_delegated_compact_hook_and_slash_command_do_not_consume_cancellation(
    monkeypatch, tmp_path, away_state
):
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    adapter = ha.HarnessAdapter()
    await adapter.connect(_preset_options(tmp_path, app_session_id=away_state))
    try:
        database.set_session_run_binding(away_state, away_mode=True)
        await _run_turn(adapter, "work")
        database.set_session_run_binding(away_state, away_mode=False)
        loop = adapter.engine
        for _ in range(2):
            loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
            output = await loop._cli_session_start({"source": "compact", "transcript_path": ""})
            assert away_mode.OWNER_BACK_NOTE in output["hookSpecificOutput"]["additionalContext"]
        await _run_turn(adapter, "/compact")
        assert away_mode.OWNER_BACK_NOTE in away_mode.turn_note(away_state)

        async def spawn_failed():
            raise RuntimeError("CLI spawn failed")

        with monkeypatch.context() as patcher:
            patcher.setattr(loop, "_ensure_process", spawn_failed)
            failed = await _run_turn(adapter, "failed before CLI input")
            assert failed.is_error
        assert away_mode.OWNER_BACK_NOTE in away_mode.turn_note(away_state)
        await _run_turn(adapter, "owner returned")
        assert away_mode.turn_note(away_state) is None
    finally:
        await adapter.disconnect()

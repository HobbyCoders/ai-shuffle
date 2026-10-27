"""A working-record change never touches the provider's cached prefix (#680).

The owner working record changes whenever a report lands or an item moves. It
used to be appended to the system prompt, so an ordinary background resume
re-billed the whole prefix (probe: ``prefix_changed``/``system`` ->
``harness_mutation``) and the pool replaced the client
(``Options changed ... (system_prompt)``). It now rides the tail of the turn's
user message, only when it differs from what the model last read.
"""
from __future__ import annotations

import sys

import pytest

import app.core.provider.harness_adapter as ha
from app.core import working_record
from app.core import session_orchestration as orch
from app.db import database
from app.db import working_record as store
from tests.core import test_native_scripted_provider_app as native_fixture
from tests.core.test_delegated_leg import FAKE, _ArgvLoop, _preset_options, _run_turn
from tests.core.test_native_scripted_provider_app import (  # noqa: F401 — fixtures
    Messages,
    NativeRun,
    _isolated_home,
)
from tests.core.test_session_orchestration import chat

provider = native_fixture.provider
HEADER = "Owner working record"


@pytest.fixture
def owner(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    return chat("coordinator", mode="chat")[0]["id"]


def _user_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


def _content(messages: list) -> list:
    """Messages without the rolling ``cache_control`` breakpoint marker."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "cache_control"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(messages)


def test_record_change_keeps_system_prompt_and_pooled_client(owner, monkeypatch, tmp_path):
    """The production options builder: a report landing between two builds
    changes neither the system prompt nor anything the pool reconnects on."""
    from types import SimpleNamespace

    from app.core import config, query_options
    from app.core.session_pool import reconnect_option_names

    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    store.add("decision", "Ship only after review.", owner)
    first, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=owner)
    working_record.record_report(owner, {"task_id": "child", "agent_message_turn_id": "t1",
                                         "status": "completed", "summary": "Patch ready.", "title": "Fix"})
    second, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=owner)
    assert first.system_prompt == second.system_prompt
    assert HEADER not in str(second.system_prompt)
    assert reconnect_option_names(SimpleNamespace(connect_options=first), second) == []
    assert second.aih_working_record is True
    assert "Patch ready." in working_record.turn_note(owner)


def test_measured_background_resume_reuses_the_prefix():
    """The reproducible measurement (scripts/measure_working_record_prefix.py).
    Before #680's fix: probe ``prefix_changed``/``system``, class
    ``harness_mutation``, 0 reusable of ~90.8k tokens, reconnect on
    ``system_prompt``. After: the whole prior request is a reusable prefix."""
    from scripts.measure_working_record_prefix import measure

    result = measure()
    assert result["record_in_system_prompt"] is False
    assert result["system_prompt_changed"] is False
    assert result["reconnect_option_names"] == []
    assert result["probe_changed"] is None and result["probe_reason"] == "prefix_grew_only"
    assert result["miss_reported_when_prefix_reused"] is False
    # Only the new turn (prompt + changed record) is fresh: well under 1%.
    assert result["fresh_tokens_if_prefix_reused"] < result["request_tokens_estimated"] // 100
    assert 0 < result["turn_note_chars"][1] <= working_record.SUMMARY_BYTES + 64


def test_turn_note_is_owner_only(owner):
    assert working_record.turn_note(owner).startswith("<system-reminder>\n" + HEADER)
    worker, _ = chat("worker-run", is_worker=True)
    assert working_record.turn_note(worker["id"]) is None
    assert working_record.turn_note(None) is None


@pytest.mark.asyncio
async def test_native_prefix_is_identical_across_record_changes(monkeypatch, tmp_path, provider, owner):
    provider.script(*[Messages.text(f"reply {n}") for n in range(4)])
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra.update(app_session_id=owner, working_record=True)
        engine = run.adapter.engine
        store.add("decision", "Ship only after review.", owner)
        await run.turn("turn 0")
        await run.turn("turn 1")  # unchanged record: not repeated
        store.add("pending", "Collect performance numbers.", owner)
        await run.turn("turn 2")  # a background change between turns
        await run.turn("turn 3")
        assert run.adapter.engine is engine
    bodies = [request.body for request in provider.requests]
    assert len(bodies) == 4
    for earlier, later in zip(bodies, bodies[1:]):
        assert later.get("system") == earlier.get("system")
        assert later.get("tools") == earlier.get("tools")
        assert _content(later["messages"][: len(earlier["messages"])]) == _content(earlier["messages"])
    newest = [_user_text(body["messages"][-1]) for body in bodies]
    assert [HEADER in text for text in newest] == [True, False, True, False]
    assert "Ship only after review." in newest[0]
    assert "Collect performance numbers." in newest[2] and "Ship only after review." in newest[2]
    for body in bodies:
        assert HEADER not in str(body.get("system") or "")


@pytest.mark.asyncio
async def test_native_record_is_resent_until_the_model_reads_it(monkeypatch, tmp_path, provider, owner):
    from tests.core.test_native_scripted_provider_app import fps

    provider.script(fps.error_reply(400, "request refused"), Messages.text("ok"), Messages.text("next"))
    async with NativeRun(monkeypatch, tmp_path, provider) as run:
        run.options.extra.update(app_session_id=owner, working_record=True)
        store.add("decision", "Ship only after review.", owner)
        failed = await run.turn("first")
        assert failed[-1].is_error
        assert run.adapter._working_record_held is None
        await run.turn("second")
        await run.turn("third")
    newest = [_user_text(request.body["messages"][-1]) for request in provider.requests]
    assert [HEADER in text for text in newest] == [True, True, False]


def test_compaction_and_model_switch_forget_the_held_record():
    adapter = ha.HarnessAdapter()
    adapter._working_record_held = "digest"

    class ContextCompaction:
        reason = "auto"

    adapter._observe_memory_event(ContextCompaction())
    assert adapter._working_record_held is None


@pytest.mark.asyncio
async def test_delegated_record_change_neither_respawns_nor_changes_the_prompt(monkeypatch, tmp_path, owner):
    monkeypatch.setattr(ha, "DelegatedLoop", _ArgvLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    _ArgvLoop.argvs = []
    sent: list[str] = []
    adapter = ha.HarnessAdapter()
    await adapter.connect(_preset_options(tmp_path, app_session_id=owner, working_record=True))
    real_submit = adapter.engine.submit

    async def record(turn):
        content = getattr(turn, "content", None)
        if isinstance(content, str):
            sent.append(content)
        return await real_submit(turn)

    monkeypatch.setattr(adapter.engine, "submit", record)
    append = adapter.engine.append_system_prompt
    try:
        store.add("decision", "Ship only after review.", owner)
        assert not (await _run_turn(adapter, "turn 0")).is_error
        assert not (await _run_turn(adapter, "turn 1")).is_error
        store.add("pending", "Collect performance numbers.", owner)
        assert not (await _run_turn(adapter, "turn 2")).is_error
        assert adapter.engine.append_system_prompt == append
        # A compaction put-back always carries the current record.
        assert "Collect performance numbers." in (adapter._compact_note() or "")
    finally:
        await adapter.disconnect()
    assert len(_ArgvLoop.argvs) == 1, "a record change must not respawn the CLI"
    assert HEADER not in append
    assert [HEADER in text for text in sent] == [True, False, True]
    assert "Collect performance numbers." in sent[2]

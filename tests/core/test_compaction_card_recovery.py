"""#1886: a compaction the engine persisted but the app never recorded a card
for (the process died between the two writes) gets its card at the next
connect, once, and only for the last turn.

No CLI is spawned: ``DelegatedLoop`` is replaced with the kwargs-capturing
fake from the re-seed tests, over the real PostgreSQL store (test shim).
"""

from __future__ import annotations

import dataclasses

import pytest
from fluid.events.model import ContextCompaction, TurnStarted

import app.core.provider.harness_adapter as ha
from app.core.provider import fluid_transcript
from app.db import database
from tests.core.test_delegated_reseed_on_connect import PELICAN, SID, _CapturingLoop, _options, _seed

APP_SID = "app-recover-1"


@pytest.fixture(autouse=True)
def _no_reseed_or_cli(monkeypatch):
    monkeypatch.setattr(fluid_transcript, "reseed_delegated_cli_session", lambda *a, **k: None)
    monkeypatch.setattr(ha, "DelegatedLoop", _CapturingLoop)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: ["claude-not-spawned"])


async def _store_with_compactions(*events) -> object:
    from app.core.provider.fluid_pg_store import PgFluidStore

    store = PgFluidStore()
    await _seed(store, PELICAN)
    for seq, event in enumerate(events, start=1):
        event = dataclasses.replace(event, seq=seq, ts=1_790_000_000.0 + seq)
        await store.append_event(SID, event)
    return store


async def _connect(monkeypatch, store, tmp_path) -> ha.HarnessAdapter:
    monkeypatch.setattr(ha.HarnessAdapter, "_build_store", staticmethod(lambda _o: store))
    options = _options(tmp_path)
    options.extra = {**(options.extra or {}), "app_session_id": APP_SID}
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    return adapter


def _cards() -> list:
    return [r for r in fluid_transcript.recorded_compaction_cards(APP_SID)]


def _turn() -> TurnStarted:
    return TurnStarted(session_id=SID, model="opus", provider="claude")


def _compaction(**kw) -> ContextCompaction:
    kw.setdefault("tier", 3)
    kw.setdefault("reason", "cli_auto_compact")
    return ContextCompaction(session_id=SID, **kw)


@pytest.mark.asyncio
async def test_a_card_lost_after_the_engine_wrote_its_compaction_is_recorded_at_connect(
    mock_db, monkeypatch, tmp_path,
):
    database.create_session(APP_SID, None)
    store = await _store_with_compactions(
        _turn(),
        _compaction(before_tokens=169_000, after_tokens=19_600, context_window=200_000),
    )
    # The recorder append never happened: no card yet.
    assert _cards() == []

    await (await _connect(monkeypatch, store, tmp_path)).disconnect()

    [card] = _cards()
    assert card["compaction_event_id"] == f"{SID}:2"
    from app.db import session_events

    [row] = [r for r in session_events.get_events(APP_SID)
             if r["event_type"] == "system.compaction_boundary"]
    metadata = row["payload"]["data"]["compact_metadata"]
    assert metadata["trigger"] == "auto"
    assert metadata["pre_tokens"] == 169_000 and metadata["post_tokens"] == 19_600
    assert metadata["context_window"] == 200_000

    # A second connect finds the card and adds nothing.
    await (await _connect(monkeypatch, store, tmp_path)).disconnect()
    assert len(_cards()) == 1


@pytest.mark.asyncio
async def test_only_the_last_turns_compactions_are_recovered(mock_db, monkeypatch, tmp_path):
    database.create_session(APP_SID, None)
    store = await _store_with_compactions(
        _turn(),
        _compaction(before_tokens=150_000, after_tokens=10_000),  # an older turn's
        _turn(),
        _compaction(before_tokens=160_000, after_tokens=12_000),
    )
    await (await _connect(monkeypatch, store, tmp_path)).disconnect()
    assert [c["compaction_event_id"] for c in _cards()] == [f"{SID}:4"]


@pytest.mark.asyncio
async def test_a_card_an_older_recorder_wrote_without_an_id_is_not_doubled(
    mock_db, monkeypatch, tmp_path,
):
    from app.core.turn_recorder import record_drained_event

    database.create_session(APP_SID, None)
    store = await _store_with_compactions(
        _turn(),
        _compaction(before_tokens=169_000, after_tokens=19_600),
    )
    await record_drained_event(APP_SID, {"type": "system", "subtype": "compact_boundary", "data": {
        "compact_metadata": {"trigger": "auto", "pre_tokens": 169_000, "post_tokens": 19_600}}})
    await (await _connect(monkeypatch, store, tmp_path)).disconnect()
    assert len(_cards()) == 1


@pytest.mark.asyncio
async def test_a_forks_copied_card_is_not_doubled(mock_db, monkeypatch, tmp_path):
    """A fork copies the cards with the source's event ids and renumbers the
    engine events; the event's timestamp still matches its card."""
    from app.core.turn_recorder import record_drained_event

    database.create_session(APP_SID, None)
    store = await _store_with_compactions(
        _turn(),
        _compaction(before_tokens=169_000, after_tokens=19_600),
    )
    await record_drained_event(APP_SID, {"type": "system", "subtype": "compact_boundary", "data": {
        "compact_metadata": {"trigger": "auto", "pre_tokens": 169_000, "post_tokens": 19_600},
        "compaction_event_id": "ses_forksource:9", "compaction_event_ts": 1_790_000_002.0}})
    await (await _connect(monkeypatch, store, tmp_path)).disconnect()
    assert [c["compaction_event_id"] for c in _cards()] == ["ses_forksource:9"]

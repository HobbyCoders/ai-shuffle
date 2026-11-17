"""The context-window pin a model switch drops, and its revision (#1640).

``sessions.context_window`` is the saved pin. A model that cannot hold it used
to leave it on the row, so the session API kept reporting the previous model's
larger window. Now the write that saves such a model clears the pin.

``sessions.context_window_rev`` counts every change of the pin, the clear
included. Everything a client is told about the pin (the row, ``/live``, the
picker's answer, a ``session_updated`` push) names that revision, and a client
applies it only when it is newer than the one it holds. A push that arrives
late therefore changes nothing, which is why no writer has to announce a clear
at a particular moment: the row itself is the announcement.

Covers:
- the clear: only a pin above the new model's maximum, never a pin written
  with the model, never a pin another writer saved for a newer model;
- the counter: one revision per change, none for a write that changes nothing;
- every model writer goes through the same clear (send, picker, voice patch,
  background query), and the session API reports the cleared pin;
- what a send tells open clients, with the revision that orders it.
"""

import asyncio
from unittest.mock import AsyncMock, call, patch

import pytest

from app.core import turn_manager
from app.core.sync_engine import sync_engine
from app.db import database as db
from tests.core.helpers import make_profile

BIG, SMALL = "claude-opus-4-6", "claude-haiku-4-5"


def _pin(session_id):
    row = db.get_session(session_id)
    return row["context_window"], int(row["context_window_rev"] or 0)


def _chat(session_id, *, model=BIG, pin=1_000_000, **extra):
    make_profile("agent", {})
    db.create_session(session_id, profile_id="agent", model=model, context_window=pin, **extra)


class TestTheClear:
    @pytest.mark.parametrize("pin, model, kept", [
        # #1640: a 1M model pinned at 1M, then a 200k model: the pin goes.
        (1_000_000, SMALL, None),
        # A pin the new model can hold stays the ceiling it is.
        (200_000, SMALL, 200_000),
        (1_000_000, "claude-opus-4-7", 1_000_000),
    ])
    def test_model_switch_drops_a_pin_the_new_model_cannot_hold(
            self, mock_db, authenticated_client, pin, model, kept):
        _chat("s", pin=pin)
        saved = db.set_session_run_binding("s", model=model)
        dropped = kept is None
        assert saved["context_window"] == kept
        assert saved.get("context_window_dropped") == (pin if dropped else None)
        assert _pin("s") == (kept, 1 if dropped else 0)
        assert saved["context_window_rev"] == (1 if dropped else 0)
        body = authenticated_client.get("/api/v1/sessions/s").json()
        assert (body["context_window"], body["context_window_rev"]) == (kept, 1 if dropped else 0)

    def test_a_window_written_with_the_model_is_not_dropped(self, mock_db):
        _chat("s")
        saved = db.set_session_run_binding("s", model="claude-opus-4-7", context_window=400_000)
        assert "context_window_dropped" not in saved
        assert _pin("s") == (400_000, 1)

    def test_a_write_that_names_no_model_never_judges_the_pin(self, mock_db):
        # An older row may hold a pin above its model: only a model write
        # clears it, so an effort pick does not change the window under a turn.
        _chat("s", model=SMALL)
        db.set_session_run_binding("s", effort="high")
        assert _pin("s") == (1_000_000, 0)

    def test_the_clear_never_erases_a_pin_saved_for_a_newer_model(self, mock_db, monkeypatch):
        """The clear is judged against the 200k model; meanwhile another writer
        saves the 1M model with a deliberately chosen 1M pin. That pin stays."""
        from app.core import model_registry
        from app.db import sessions as sessions_db

        _chat("s", model=SMALL)
        judged = model_registry.session_context_window_maximum

        def judge_then_race(row):
            maximum = judged(row)
            db.set_session_run_binding("s", model=BIG, context_window=1_000_000)
            return maximum

        monkeypatch.setattr(model_registry, "session_context_window_maximum", judge_then_race)
        assert sessions_db._drop_pin_above_model_maximum("s") is None
        row = db.get_session("s")
        assert (row["model"], row["context_window"]) == (BIG, 1_000_000)


class TestTheCounter:
    def test_every_change_of_the_pin_is_one_revision(self, mock_db):
        _chat("s", pin=None)
        assert _pin("s") == (None, 0)
        db.set_session_run_binding("s", context_window=1_000_000)
        assert _pin("s") == (1_000_000, 1)
        # The same window again changes nothing, so it is no new revision.
        db.set_session_run_binding("s", context_window=1_000_000)
        assert _pin("s") == (1_000_000, 1)
        db.set_session_run_binding("s", context_window=200_000)
        assert _pin("s") == (200_000, 2)
        # Another leg of the binding leaves the counter alone.
        db.set_session_run_binding("s", effort="high", fast=True)
        assert _pin("s") == (200_000, 2)
        # A model that can hold the pin: nothing changes.
        db.set_session_run_binding("s", model=SMALL)
        assert _pin("s") == (200_000, 2)

    def test_a_clear_is_a_revision_and_a_later_pick_is_the_next(self, mock_db):
        _chat("s")
        db.set_session_run_binding("s", model=SMALL)
        assert _pin("s") == (None, 1)
        db.set_session_run_binding("s", model=BIG, context_window=1_000_000)
        assert _pin("s") == (1_000_000, 2)

    def test_the_voice_patch_counts_and_clears_the_same_way(self, mock_db):
        _chat("s", mode="voice", provider_id="realtime-provider")
        db.patch_voice_session_context("s", {"model": SMALL})
        assert _pin("s") == (None, 1)
        db.patch_voice_session_context("s", {"model": BIG, "context_window": 1_000_000})
        assert _pin("s") == (1_000_000, 2)
        db.patch_voice_session_context("s", {"context_window": 1_000_000})
        assert _pin("s") == (1_000_000, 2)

    async def test_the_picker_reports_and_announces_the_revision(self, mock_db):
        from app.api.sessions_permissions import RunSettingsUpdate, _announce_run_settings, apply_run_settings

        _chat("s", pin=200_000)
        result = await apply_run_settings("s", RunSettingsUpdate(context_window=1_000_000))
        assert (result["context_window"], result["context_window_rev"]) == (1_000_000, 1)
        with patch.object(sync_engine, "broadcast_session_updated", new=AsyncMock()) as broadcast:
            await _announce_run_settings("s", result, effort_asked=False)
        updates = broadcast.await_args.kwargs["updates"]
        assert (updates["context_window"], updates["context_window_rev"]) == (1_000_000, 1)


class TestEveryModelWriter:
    """No writer announces the clear itself: each saves the model through the
    same two functions, and the row's new revision is what clients order by."""

    async def test_background_query(self, mock_db, tmp_path):
        import app.core.query_background as qb

        _chat("s")
        with patch.object(qb, "_run_background_query", new=AsyncMock()):
            await qb.start_background_query(
                "hi", "agent", session_id="s", overrides={"cwd": str(tmp_path), "model": SMALL})
            await asyncio.sleep(0)
        assert _pin("s") == (None, 1)

    def test_a_queued_send(self, mock_db):
        _chat("s")
        turn_manager.submit("s", "user", prompt="hi", profile_id="agent",
                            overrides={"model": SMALL}, no_kick=True)
        assert _pin("s") == (None, 1)

    def test_a_replicated_row_carries_the_revision(self):
        # The pin's revision travels with the row, like ``effort_rev``: only
        # the owner writes it, and a mirror's page adopts it from the row.
        from app.network.policy import COLLECTIONS

        sessions = COLLECTIONS["sessions"]
        assert "context_window_rev" in sessions.fields
        assert "context_window_rev" in sessions.late_fields


class TestWhatASendTells:
    async def _send(self, session_id, **overrides):
        with patch.object(sync_engine, "broadcast_session_updated", new=AsyncMock()) as broadcast:
            await turn_manager.submit_async(
                session_id, "user", prompt="hi", profile_id="agent", overrides=overrides, no_kick=True)
        return [c for c in broadcast.await_args_list if "context_window" in c.kwargs["updates"]]

    async def test_a_send_that_drops_the_pin_says_so_with_its_revision(self, mock_db):
        """Open composers hold the dropped pin as their base; they learn it is
        gone, or their next window would be ignored as outdated."""
        _chat("s")
        pushes = await self._send("s", model=SMALL)
        assert _pin("s") == (None, 1)
        assert pushes == [call(session_id="s", source_device_id=None,
                               updates={"context_window": None, "context_window_rev": 1})]

    async def test_the_senders_window_is_judged_against_the_pin_it_saw(self, mock_db):
        # The composer switched to the 200k model and still names the old pin
        # as its base: that base is current, the new model offers no such
        # window, so nothing is pinned and the sender learns the pin is gone.
        _chat("s")
        pushes = await self._send("s", model=SMALL, context_window=1_000_000,
                                  context_window_base=1_000_000)
        assert _pin("s") == (None, 1)
        assert pushes == [call(session_id="s", source_device_id=None,
                               updates={"context_window": None, "context_window_rev": 1})]

    async def test_a_send_that_keeps_the_pin_tells_nothing(self, mock_db):
        _chat("s", pin=200_000)
        assert await self._send("s", model=SMALL) == []
        assert _pin("s") == (200_000, 0)

    async def test_an_outdated_base_learns_the_saved_pin_and_its_revision(self, mock_db):
        # Another tab picked 200k (revision 1) after this one read 1M.
        _chat("s")
        db.set_session_run_binding("s", context_window=200_000)
        pushes = await self._send("s", model=BIG, context_window=1_000_000,
                                  context_window_base=1_000_000)
        assert _pin("s") == (200_000, 1)
        assert pushes == [call(session_id="s", source_device_id=None,
                               updates={"context_window": 200_000, "context_window_rev": 1})]

    async def test_a_send_that_saves_a_window_tells_the_new_revision(self, mock_db):
        _chat("s", pin=200_000)
        pushes = await self._send("s", model=BIG, context_window=1_000_000,
                                  context_window_base=200_000)
        assert _pin("s") == (1_000_000, 1)
        assert pushes == [call(session_id="s", source_device_id=None,
                               updates={"context_window": 1_000_000, "context_window_rev": 1})]

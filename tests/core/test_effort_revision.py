"""The effort revision (#1609): a send from a client that holds an older view
of a chat's effort level must not save that level over a newer pick.

``sessions.effort_rev`` counts the changes of ``sessions.effort``. A composer
sends the revision it last saw with each message (``overrides["effort_rev"]``);
the send saves its level only while that is still the row's revision.
"""
from unittest.mock import AsyncMock, call, patch

import pytest

from app.api import sessions_permissions as api
from app.core import turn_manager
from app.core.models import Session
from app.core.sync_engine import sync_engine
from app.db import database as db
from app.db import turns
from tests.core.helpers import make_profile


def _pair(session_id):
    row = db.get_session(session_id)
    return row["effort"], int(row["effort_rev"] or 0)


def _send(session_id, **overrides):
    """One composer send; returns the overrides the queued turn runs on."""
    turn_id = turn_manager.submit(
        session_id, "user", prompt="hi", profile_id="agent",
        overrides={"model": "opus", **overrides}, no_kick=True)
    return turns.get_turn(turn_id)["overrides"]


def _bind(session_id, **binding):
    """The effort pair a binding write returns (it also names the pin, #1640)."""
    saved = db.set_session_run_binding(session_id, **binding)
    return saved and {"effort": saved["effort"], "effort_rev": saved["effort_rev"]}


class TestRevisionCounter:
    def test_every_change_of_the_level_is_one_revision(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        assert _pair("s") == ("high", 0)
        pick = {"effort_explicit": True}
        assert _bind("s", effort="low", **pick) == {"effort": "low", "effort_rev": 1}
        # Writing the level the row holds is no change: one revision, one level.
        assert _bind("s", effort="low", **pick) == {"effort": "low", "effort_rev": 1}
        # Auto is a level too.
        assert _bind("s", clear_effort=True, **pick) == {"effort": None, "effort_rev": 2}
        assert _bind("s", clear_effort=True, **pick) == {"effort": None, "effort_rev": 2}
        assert _bind("s", effort="max", **pick) == {"effort": "max", "effort_rev": 3}
        # A write of another field reports the pair and leaves it alone.
        assert _bind("s", fast=True) == {"effort": "max", "effort_rev": 3}
        assert _pair("s") == ("max", 3)

    def test_nothing_to_write_and_unknown_session(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus")
        assert _bind("s") is None
        assert _bind("missing", effort="low") is None

    def test_a_guarded_write_needs_the_rows_revision(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")
        # Revision 0 is no longer the row's: the level is dropped, the other
        # fields of the same write are saved.
        assert _bind("s", effort="high", fast=True, effort_rev_base=0) == {
            "effort": "low", "effort_rev": 1}
        assert db.get_session("s")["fast"]
        assert _bind("s", effort="high", effort_rev_base=1) == {
            "effort": "high", "effort_rev": 2}

    def test_a_base_above_the_rows_revision_moves_the_row_past_it(self, mock_db):
        """The row lost its count (a restored backup) while a tab kept its own:
        the row's level stands and its revision passes the tab's, so the tab
        adopts it. Without the jump no answer could ever be news to that tab."""
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")                    # (low, 1)
        assert _bind("s", effort="max", effort_rev_base=7) == {
            "effort": "low", "effort_rev": 8}
        # A send that names no level (Auto) moves it too.
        assert _bind("s", fast=False, effort_rev_base=12) == {
            "effort": "low", "effort_rev": 13}
        # ... and leaves a row alone that is not behind.
        assert _bind("s", fast=False, effort_rev_base=13) == {
            "effort": "low", "effort_rev": 13}
        assert _bind("s", fast=False, effort_rev_base=4) == {
            "effort": "low", "effort_rev": 13}

    def test_the_voice_context_write_counts_too(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.patch_voice_session_context("s", {"effort": "low"})
        assert _pair("s") == ("low", 1)
        db.patch_voice_session_context("s", {"effort": "low", "model": "sonnet"})
        assert _pair("s") == ("low", 1)
        db.patch_voice_session_context("s", {"model": "opus"})
        assert _pair("s") == ("low", 1)

    def test_the_session_api_reads_a_never_changed_effort_as_revision_zero(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        assert db.get_session("s")["effort_rev"] is None
        assert Session(**db.get_session("s")).effort_rev == 0
        db.set_session_run_binding("s", effort="low")
        assert Session(**db.get_session("s")).effort_rev == 1


class TestSendGuard:
    def test_a_stale_send_keeps_the_newer_level(self, mock_db):
        """Another device picked low; a tab that still shows high sends high."""
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")
        ran = _send("s", effort="high", effort_rev=0)
        assert _pair("s") == ("low", 1)
        # The turn runs on the saved level, and the revision is not stored on it.
        assert ran["effort"] == "low"
        assert "effort_rev" not in ran

    def test_a_stale_auto_send_runs_on_the_saved_level(self, mock_db):
        """A client on Auto names no level; behind, its turn takes the saved one."""
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus")
        db.set_session_run_binding("s", effort="low")
        ran = _send("s", effort_rev=0)
        assert _pair("s") == ("low", 1)
        assert ran["effort"] == "low"

    def test_a_stale_send_does_not_undo_a_newer_auto(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", clear_effort=True)
        ran = _send("s", effort="high", effort_rev=0)
        assert _pair("s") == (None, 1)
        assert "effort" not in ran

    def test_a_send_from_a_tab_ahead_of_the_row_runs_on_the_saved_level(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")
        ran = _send("s", effort="max", effort_rev=7)
        assert _pair("s") == ("low", 8)
        assert ran["effort"] == "low"
        # The tab adopted (low, 8): its next change is saved.
        assert _send("s", effort="max", effort_rev=8)["effort"] == "max"
        assert _pair("s") == ("max", 9)

    def test_a_matching_send_saves_its_level(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")
        ran = _send("s", effort="max", effort_rev=1)
        assert _pair("s") == ("max", 2)
        assert ran["effort"] == "max"
        # The level the row already holds is no new revision.
        _send("s", effort="max", effort_rev=2)
        assert _pair("s") == ("max", 2)

    def test_a_send_without_a_revision_runs_its_level_but_saves_none_over_a_change(self, mock_db):
        """S4 (#1609): an older client, a cache from before revisions, or the API
        names no revision, so it cannot show its level is newer than the row's.
        Its turn runs on the level it sent; a row whose effort changed keeps its
        own. Only the run-settings endpoint changes a saved level."""
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")                     # (low, 1)
        ran = _send("s", effort="high")
        assert _pair("s") == ("low", 1)
        assert ran["effort"] == "high"
        assert _send("s", effort="max")["effort"] == "max"
        assert _pair("s") == ("low", 1)

    def test_a_send_without_a_revision_on_a_never_changed_row_behaves_as_before(self, mock_db):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        ran = _send("s", effort="low")
        assert _pair("s") == ("low", 1)
        assert ran["effort"] == "low"
        # A send that names no level keeps the saved one, as before.
        _send("s")
        assert _pair("s") == ("low", 1)

    @pytest.mark.parametrize("bad", [True, -1, "0", 1.0, None])
    def test_a_revision_that_is_not_a_count_is_no_revision(self, mock_db, bad):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")
        ran = _send("s", effort="high", effort_rev=bad)
        # No revision: the turn runs its level, the changed row keeps its own.
        assert _pair("s") == ("low", 1)
        assert ran["effort"] == "high"
        assert "effort_rev" not in ran

    def test_two_tabs(self, mock_db):
        """Both tabs open the chat at revision 0. One picks; the other, which
        heard nothing, sends: the pick stands, and once the second tab holds
        the new revision its own change is saved."""
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        tab_a = tab_b = 0
        picked = db.set_session_run_binding("s", effort="low")      # tab A's picker
        tab_a = picked["effort_rev"]
        assert _send("s", effort="high", effort_rev=tab_b)["effort"] == "low"
        assert _pair("s") == ("low", 1)
        tab_b = 1                                                    # B adopts the answer
        assert _send("s", effort="max", effort_rev=tab_b)["effort"] == "max"
        assert _pair("s") == ("max", 2)
        # Now A is the one behind.
        assert _send("s", effort="low", effort_rev=tab_a)["effort"] == "max"
        assert _pair("s") == ("max", 2)


class TestAnnouncement:
    """What a send tells the chat's open clients, so they adopt the saved level."""

    @pytest.mark.parametrize("sent, expected", [
        # Behind: the level is dropped and the sender learns the saved pair.
        ({"effort": "high", "effort_rev": 0}, {"effort": "low", "effort_rev": 1}),
        # Behind but naming the saved level: only the revision is news.
        ({"effort": "low", "effort_rev": 0}, {"effort": "low", "effort_rev": 1}),
        # Current, and the send changed the level: every client learns it.
        ({"effort": "max", "effort_rev": 1}, {"effort": "max", "effort_rev": 2}),
        # Current and unchanged: nothing to tell.
        ({"effort": "low", "effort_rev": 1}, None),
        # No revision: nothing is announced, as before.
        ({"effort": "max"}, None),
        # Ahead of the row: the saved level with a revision past the sender's.
        ({"effort": "max", "effort_rev": 7}, {"effort": "low", "effort_rev": 8}),
        # On Auto and behind: no level was sent, the saved one is still told.
        ({"effort_rev": 0}, {"effort": "low", "effort_rev": 1}),
    ])
    async def test_send_pushes_the_saved_level_and_revision(self, mock_db, sent, expected):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")
        seen = {}
        with patch.object(sync_engine, "broadcast_session_updated", new=AsyncMock()) as broadcast:
            await turn_manager.submit_async(
                "s", "user", prompt="hi", profile_id="agent",
                overrides={"model": "opus", **sent}, no_kick=True, effort_seen=seen)
        pushes = [c for c in broadcast.await_args_list if "effort" in c.kwargs["updates"]]
        if expected is None:
            assert pushes == [] and seen == {}
        else:
            assert pushes == [call(session_id="s", updates=expected, source_device_id=None)]
            assert seen == expected

    async def test_the_picker_reports_and_announces_the_new_revision(self, mock_db, monkeypatch):
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        monkeypatch.setattr(api.control, "_require_owned", lambda sid: db.get_session(sid))
        result = await api.apply_run_settings("s", api.RunSettingsUpdate(effort="low"))
        assert (result["effort"], result["effort_rev"]) == ("low", 1)
        assert _pair("s") == ("low", 1)
        with patch.object(sync_engine, "broadcast_session_updated", new=AsyncMock()) as broadcast:
            await api._announce_run_settings("s", result, effort_asked=True)
        assert broadcast.await_args.kwargs["updates"]["effort"] == "low"
        assert broadcast.await_args.kwargs["updates"]["effort_rev"] == 1
        # A Fast change says nothing about effort, so it carries no revision.
        fast = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True))
        assert "effort_rev" not in fast
        # An owner too old to count announces the level alone.
        with patch.object(sync_engine, "broadcast_session_updated", new=AsyncMock()) as broadcast:
            await api._announce_run_settings("s", {"effort": "max", "fast": False}, effort_asked=True)
        assert "effort_rev" not in broadcast.await_args.kwargs["updates"]


class TestPathsThatDidNotPickALevel:
    """Review findings S1-S3 (#1609): every path that writes the row's level
    either was asked to change it or goes through the send guard. Each test is
    the interleaving the review reproduced: a snapshot read, a newer pick, then
    the write."""

    async def test_a_fast_only_change_leaves_a_level_saved_after_its_read(self, mock_db, monkeypatch):
        """S1: the Fast change reads high/0; a guarded send saves low/1 before
        its write; the Fast write used to restore high at revision 2."""
        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")

        def read_then_pick(session_id):
            row = db.get_session(session_id)
            _send(session_id, effort="low", effort_rev=0)
            return row

        monkeypatch.setattr(api.control, "_require_owned", read_then_pick)
        result = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True), propagate=False)
        assert result["fast"] is True
        assert _pair("s") == ("low", 1)
        assert db.get_session("s")["fast"]

    async def test_a_voice_delegation_runs_on_a_level_picked_after_its_read(self, mock_db, monkeypatch):
        """S2: the delegation copies high/0 from its read, a picker saves low/1
        while it awaits, then it submits as "user"."""
        from app.core import api_user_scope, session_runtime, voice_stream

        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        monkeypatch.setattr(turn_manager, "_kick", lambda *a, **k: None)
        monkeypatch.setattr(session_runtime, "try_steer_pending", AsyncMock(return_value=0))

        def pick_meanwhile(row):
            db.set_session_run_binding("s", effort="low")              # (low, 1)
            return None

        monkeypatch.setattr(api_user_scope, "session_api_user_id", pick_meanwhile)
        submitted = AsyncMock()
        with patch.object(sync_engine, "broadcast_session_updated", new=AsyncMock()):
            assert await voice_stream.delegate_to_selected_model(
                "s", "Run the tests.", on_submitted=submitted) == ""
        assert _pair("s") == ("low", 1)
        assert turns.get_turn(submitted.await_args.args[0])["overrides"]["effort"] == "low"

    async def test_a_background_send_from_an_older_view_keeps_the_newer_level(self, mock_db, monkeypatch):
        """S3: the background API lane bypassed the guard."""
        from unittest.mock import MagicMock

        from app.core import query_background

        make_profile("agent", {})
        db.create_session("s", profile_id="agent", model="opus", effort="high")
        db.set_session_run_binding("s", effort="low")                      # (low, 1)
        ran = {}

        async def run(**kwargs):
            ran.update(kwargs["overrides"])

        monkeypatch.setattr(query_background, "_run_background_query", run)
        monkeypatch.setattr(query_background.session_admission, "close", lambda lease: None)
        with patch.object(sync_engine, "broadcast_session_updated", new=AsyncMock()) as broadcast:
            await query_background._start_background_turn(
                MagicMock(), False, "hi", "agent", None,
                {"model": "opus", "effort": "high", "effort_rev": 0}, "s", None, None)
            await query_background._pending_background_tasks["s"]
        assert _pair("s") == ("low", 1)
        assert ran["effort"] == "low" and "effort_rev" not in ran
        assert broadcast.await_args.kwargs["updates"] == {"effort": "low", "effort_rev": 1}

    def test_the_public_request_keeps_the_revision(self):
        """S3: ``QueryOverrides`` dropped ``effort_rev`` before any lane saw it."""
        from app.core.models import ConversationRequest

        body = ConversationRequest.model_validate(
            {"prompt": "hi", "overrides": {"effort": "high", "effort_rev": 3}})
        assert body.overrides.model_dump(exclude_none=True) == {"effort": "high", "effort_rev": 3}

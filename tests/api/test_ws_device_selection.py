"""Device selection at session creation + auto-adopt on send.

Two halves of one feature, both driven through ``ChatWebSocketHandler.handle_query``:

* **Composer device pick (NEW sessions).** ``target_instance_id`` on the query
  frame creates the session OWNED BY that install with an EXPLICIT
  ``{"where": "runner:<id>"}`` pin, and the turn hands off exactly like any
  remotely-placed one (``no_kick`` submit + ``query_queued`` ack). An unknown /
  offline target is refused BEFORE anything is created.
* **Auto-adopt (EXISTING sessions).** Sending from a device that does not own a
  session takes ownership — but ONLY for a session that merely inherited its
  owner (raw ``placement`` NULL, the A11 implicit pin), is idle, and whose
  project is checked out locally. The write is ownership-only: ``placement``
  stays NULL so the session keeps following the user. An explicit pin or a live
  run leaves today's remote-dispatch path untouched.

Fixtures mirror tests/api/test_chat_ws_v2_turns.py (real ``database`` / turns on
the pg_shim, WS collaborators patched); the install registry is stubbed the same
way tests/core/test_placement.py stubs it — its online-derivation SQL is
Postgres-only.
"""

import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
from app.core import placement as placement_mod
from app.core.config import settings
from app.db import database

ME = "local-me"
PEER = "peer-install-1"


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: ME)
    )


@pytest.fixture(autouse=True)
def _not_streaming():
    """No lease-valid active run unless a test says otherwise."""
    with patch("app.core.streaming_status.has_active_run", return_value=False):
        yield


def _stub_installs(monkeypatch, online_ids):
    rows = {
        iid: {"id": iid, "online": True, "capabilities": {}} for iid in online_ids
    }
    monkeypatch.setattr(database, "get_installs_map", lambda: rows)


class _Patches:
    """Patch the WS-only collaborators; keep turns / database real."""

    def __enter__(self):
        self._stack = ExitStack()
        s = self._stack

        self.sync = s.enter_context(patch.object(cwh, "sync_engine"))
        for name in (
            "register_device",
            "unregister_device",
            "broadcast_message_added",
            "broadcast_queue_updated",
            "broadcast_session_updated",
            "broadcast_user_session_created",
            "broadcast_user_session_updated",
        ):
            setattr(self.sync, name, AsyncMock())

        async def _run_sync(fn, *a, **k):
            return fn(*a, **k)

        s.enter_context(
            patch.object(cwh, "run_sync", new=AsyncMock(side_effect=_run_sync))
        )
        # submit_async (the off-loop twin the handler calls — W6) is the
        # placed_remote witness: no_kick mirrors it exactly.
        self.submit = s.enter_context(
            patch.object(
                cwh.turn_manager, "submit_async", AsyncMock(return_value="turn-x")
            )
        )
        from app.core import turn_claim

        self.notify = s.enter_context(
            patch.object(turn_claim, "notify_turns_changed", MagicMock())
        )
        return self

    def __exit__(self, *exc):
        self._stack.close()
        return False


def _make_handler():
    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(),
        device_id="dev-1",
        user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    return handler


def _sent(handler, ftype):
    return [
        c.args[0]
        for c in handler.send_json.call_args_list
        if c.args and isinstance(c.args[0], dict) and c.args[0].get("type") == ftype
    ]


def _row(session_id):
    """The session row, asserted present — every caller below has just created
    it, so a None here is a test bug, not a branch to handle."""
    row = database.get_session(session_id)
    assert row is not None, f"session {session_id} vanished"
    return row


def _peer_owned_session(session_id="sess-1", *, placement=None):
    """A session this install does NOT own (owner = PEER)."""
    database.create_session(session_id=session_id, profile_id="default", title="t")
    with database.get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET instance_id = %s, placement = %s WHERE id = %s",
            (PEER, json.dumps(placement) if placement else None, session_id),
        )
    return session_id


# ═══ create_session — composer device pick ═══════════════════════════════════


def test_create_session_target_stamps_owner_and_explicit_pin(mock_db):
    """target_instance_id owns the row AND writes the sticky runner pin, so a
    send from any other device dispatches instead of auto-adopting."""
    database.create_session(
        session_id="sess-pick", profile_id="default", target_instance_id=PEER
    )

    row = _row("sess-pick")
    assert row["instance_id"] == PEER
    assert placement_mod._normalize_placement(row["placement"]) == {
        "where": f"runner:{PEER}"
    }


def test_create_session_without_target_is_unchanged(mock_db):
    """No pick → this install owns it and placement stays NULL (implicit pin)."""
    database.create_session(session_id="sess-plain", profile_id="default")

    row = _row("sess-plain")
    assert row["instance_id"] == ME
    assert placement_mod._normalize_placement(row["placement"]) is None


def test_create_session_stamps_project_v2_id_with_null_project_id(mock_db):
    database.create_session(
        session_id="sess-v2",
        profile_id="default",
        project_id=None,
        project_v2_id="pv2-remote-only",
    )
    row = _row("sess-v2")
    assert row["project_id"] is None
    assert row["project_v2_id"] == "pv2-remote-only"


# ═══ handle_query — NEW session with a device pick ═══════════════════════════


async def test_new_session_with_unknown_target_refuses_before_creating(
    mock_db, monkeypatch
):
    """Unknown install → error frame, no session row, no turn."""
    monkeypatch.setattr(database, "get_install", lambda _id: None)

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run this over there",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            "target_instance_id": "nope-1",
        })

    assert _sent(handler, "error"), "the refusal must reach the client"
    assert _sent(handler, "session_created") == []
    p.submit.assert_not_called()
    rows = mock_db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
    assert rows["n"] == 0


async def test_new_session_with_offline_target_refuses_before_creating(
    mock_db, monkeypatch
):
    """Registered but offline target → error frame, nothing created."""
    monkeypatch.setattr(
        database,
        "get_install",
        lambda _id: {"id": PEER, "device_name": "Studio", "online": False},
    )

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run this over there",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            "target_instance_id": PEER,
        })

    assert "Studio is offline" in _sent(handler, "error")[0]["message"]
    assert _sent(handler, "session_created") == []
    p.submit.assert_not_called()
    assert mock_db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0


async def test_new_session_with_online_target_pins_and_dispatches(
    mock_db, monkeypatch
):
    """Online target → session created pinned to it and the turn hands off
    (no_kick submit + query_queued ack + claim-loop wake)."""
    monkeypatch.setattr(
        database,
        "get_install",
        lambda _id: {"id": PEER, "device_name": "Studio", "online": True},
    )
    database.create_profile(
        profile_id="claude-code", name="Claude Code", description=None, config={}
    )

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run this over there",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            "target_instance_id": PEER,
        })

    created = _sent(handler, "session_created")
    assert len(created) == 1
    row = _row(created[0]["session_id"])
    assert row["instance_id"] == PEER
    assert placement_mod._normalize_placement(row["placement"]) == {
        "where": f"runner:{PEER}"
    }
    assert p.submit.call_args.kwargs["no_kick"] is True
    # the install-local no-profile sentinel never rides a remote turn
    assert p.submit.call_args.kwargs["profile_id"] is None
    p.notify.assert_called_once()
    assert _sent(handler, "query_queued")


async def test_new_session_with_branch_only_checkout_refuses_before_creating(
    mock_db, monkeypatch, tmp_path
):
    """``capable_runners`` would accept this row; the base-checkout gate must
    not — Phase 3 never clones, so a WS pick of a branch-only target 409s
    before anything is created.
    """
    # PEER is online so capable_runners(project_v2_id) WOULD accept the
    # branch-only row; the test only passes if we asked has_active_base_checkout.
    _stub_installs(monkeypatch, [ME, PEER])
    monkeypatch.setattr(
        database,
        "get_install",
        lambda _id: {"id": PEER, "device_name": "Studio", "online": True},
    )
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-1", name="P", description=None, path=str(path)
    )
    from app.core import project_resolver
    from app.db import checkouts as checkouts_db

    pv2 = project_resolver.resolve_project_v2({"project_id": "proj-1"})
    checkouts_db.upsert_checkout(
        pv2["id"], PEER, str(path / "feat"), branch="some/other-branch"
    )

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run this over there",
            "session_id": None,
            "profile": "claude-code",
            "project": "proj-1",
            "target_instance_id": PEER,
        })

    assert "isn't checked out on Studio" in _sent(handler, "error")[0]["message"]
    assert _sent(handler, "session_created") == []
    p.submit.assert_not_called()
    assert mock_db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0


async def test_new_session_with_active_base_checkout_pins_and_dispatches(
    mock_db, monkeypatch, tmp_path
):
    """Active BASE checkout on the target → WS pick creates the session.

    Guards a regression that 409s every project-targeted pick: branch-only
    refusal alone would still pass if the host gate refused everyone.
    """
    _stub_installs(monkeypatch, [ME, PEER])
    monkeypatch.setattr(
        database,
        "get_install",
        lambda _id: {"id": PEER, "device_name": "Studio", "online": True},
    )
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-1", name="P", description=None, path=str(path)
    )
    database.create_profile(
        profile_id="claude-code", name="Claude Code", description=None, config={}
    )
    from app.core import project_resolver
    from app.db import checkouts as checkouts_db

    pv2 = project_resolver.resolve_project_v2({"project_id": "proj-1"})
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(path))

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run this over there",
            "session_id": None,
            "profile": "claude-code",
            "project": "proj-1",
            "target_instance_id": PEER,
        })

    assert _sent(handler, "error") == []
    created = _sent(handler, "session_created")
    assert len(created) == 1
    row = _row(created[0]["session_id"])
    assert row["instance_id"] == PEER
    assert placement_mod._normalize_placement(row["placement"]) == {
        "where": f"runner:{PEER}"
    }
    assert p.submit.call_args.kwargs["no_kick"] is True
    p.notify.assert_called_once()
    assert _sent(handler, "query_queued")


async def test_new_session_with_project_v2_id_pins_and_stamps(
    mock_db, monkeypatch, tmp_path
):
    """WS create with only ``project_v2_id`` (no local projects row) stamps
    the session and honors a remote pick."""
    from app.db import projects_v2
    from app.db import checkouts as checkouts_db

    _stub_installs(monkeypatch, [ME, PEER])
    monkeypatch.setattr(
        database,
        "get_install",
        lambda _id: {"id": PEER, "device_name": "Studio", "online": True},
    )
    database.create_profile(
        profile_id="claude-code", name="Claude Code", description=None, config={}
    )
    path = tmp_path / "repo"
    path.mkdir()
    pv2 = projects_v2.upsert_project("git:example/remote-only", name="Remote")
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(path))

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "run this over there",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            "project_v2_id": pv2["id"],
            "target_instance_id": PEER,
        })

    assert _sent(handler, "error") == []
    created = _sent(handler, "session_created")
    assert len(created) == 1
    row = _row(created[0]["session_id"])
    assert row["project_id"] is None
    assert row["project_v2_id"] == pv2["id"]
    assert row["instance_id"] == PEER
    assert p.submit.call_args.kwargs["no_kick"] is True


async def test_ws_scoped_user_projectless_create_is_not_400(mock_db, monkeypatch, tmp_path):
    """A scoped API user sending neither id is still a projectless create,
    not an XOR 400 from the origin helper."""
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-assigned", name="Assigned", description=None, path=str(path)
    )
    database.create_api_user(
        user_id="user-1",
        name="Scoped",
        api_key_hash="hash",
        project_id="proj-assigned",
    )
    database.create_profile(
        profile_id="claude-code", name="Claude Code", description=None, config={}
    )

    with _Patches():
        handler = _make_handler()
        handler.api_user_id = "user-1"
        await handler.handle_query({
            "prompt": "hello with no project",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
        })

    assert _sent(handler, "error") == []
    assert _sent(handler, "session_created")


async def test_ws_scoped_user_other_origin_is_denied(
    mock_db, monkeypatch, tmp_path
):
    from app.db import projects as projects_db
    from app.db import projects_v2

    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-assigned", name="Assigned", description=None, path=str(path)
    )
    projects_db.set_project_link_group("proj-assigned", "git:example/mine")
    other = projects_v2.upsert_project("git:example/other", name="Other")
    database.create_api_user(
        user_id="user-1",
        name="Scoped",
        api_key_hash="hash",
        project_id="proj-assigned",
    )

    with _Patches() as p:
        handler = _make_handler()
        handler.api_user_id = "user-1"
        await handler.handle_query({
            "prompt": "hello",
            "session_id": None,
            "profile": "claude-code",
            "project_v2_id": other["id"],
        })

    assert "Access denied" in _sent(handler, "error")[0]["message"]
    assert _sent(handler, "session_created") == []
    p.submit.assert_not_called()


async def test_ws_projectless_create_is_not_400(mock_db, monkeypatch):
    """Neither project id is a projectless create — not an XOR 400."""
    monkeypatch.setattr(
        database,
        "get_install",
        lambda _id: {"id": PEER, "device_name": "Studio", "online": True},
    )
    database.create_profile(
        profile_id="claude-code", name="Claude Code", description=None, config={}
    )

    with _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "hello with no project",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            "target_instance_id": PEER,
        })

    assert _sent(handler, "error") == []
    assert _sent(handler, "session_created")


async def test_ws_both_project_ids_are_400(mock_db, monkeypatch, tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-1", name="P", description=None, path=str(path)
    )
    from app.core import project_resolver

    pv2 = project_resolver.resolve_project_v2({"project_id": "proj-1"})

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "hello",
            "session_id": None,
            "profile": "claude-code",
            "project": "proj-1",
            "project_v2_id": pv2["id"],
        })

    assert "exactly one" in _sent(handler, "error")[0]["message"]
    assert _sent(handler, "session_created") == []
    p.submit.assert_not_called()
    assert mock_db.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0


async def test_new_session_v2_only_refuses_attachments(
    mock_db, monkeypatch, tmp_path
):
    from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT
    from app.db import projects_v2

    monkeypatch.setattr(
        database,
        "get_install",
        lambda _id: {"id": PEER, "device_name": "Studio", "online": True},
    )
    pv2 = projects_v2.upsert_project("git:example/remote-only", name="Remote")

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "read this",
            "session_id": None,
            "profile": "claude-code",
            "project": None,
            "project_v2_id": pv2["id"],
            "upload_paths": ["/tmp/a.txt"],
        })

    assert _sent(handler, "error")[0]["message"] == ATTACHMENTS_NEED_LOCAL_CHECKOUT
    assert _sent(handler, "session_created") == []
    p.submit.assert_not_called()


async def test_target_instance_id_ignored_for_existing_session(mock_db, monkeypatch):
    """The pick is a CREATION-time choice: on an existing (locally owned)
    session the key is dropped — no ownership rewrite, ordinary local turn."""
    _stub_installs(monkeypatch, [ME])
    database.create_session(session_id="sess-1", profile_id="default", title="t")
    monkeypatch.setattr(
        database,
        "get_install",
        MagicMock(side_effect=AssertionError("must not preflight an existing session")),
    )

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "hello",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
            "target_instance_id": PEER,
        })

    row = _row("sess-1")
    assert row["instance_id"] == ME
    assert placement_mod._normalize_placement(row["placement"]) is None
    assert p.submit.call_args.kwargs["no_kick"] is False


# ═══ handle_query — auto-adopt on send ═══════════════════════════════════════


async def test_send_auto_adopts_implicitly_pinned_idle_session(mock_db, monkeypatch):
    """NULL placement + remote owner + idle + no project → ownership moves here,
    the turn runs LOCALLY, and placement stays NULL (still follows the user)."""
    _stub_installs(monkeypatch, [ME, PEER])
    _peer_owned_session("sess-1")

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "continue here",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    row = _row("sess-1")
    assert row["instance_id"] == ME
    assert placement_mod._normalize_placement(row["placement"]) is None
    assert p.submit.call_args.kwargs["no_kick"] is False
    p.notify.assert_not_called()
    assert _sent(handler, "query_queued") == []


async def test_explicitly_pinned_session_is_never_auto_adopted(mock_db, monkeypatch):
    """An explicit runner pin is a deliberate placement — dispatch, never adopt."""
    _stub_installs(monkeypatch, [ME, PEER])
    _peer_owned_session("sess-1", placement={"where": f"runner:{PEER}"})

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "continue here",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    row = _row("sess-1")
    assert row["instance_id"] == PEER
    assert placement_mod._normalize_placement(row["placement"]) == {
        "where": f"runner:{PEER}"
    }
    assert p.submit.call_args.kwargs["no_kick"] is True
    p.notify.assert_called_once()


async def test_live_run_blocks_auto_adopt(mock_db, monkeypatch):
    """A turn in flight on the owner is never wrestled away mid-stream."""
    _stub_installs(monkeypatch, [ME, PEER])
    _peer_owned_session("sess-1")

    with patch("app.core.streaming_status.has_active_run", return_value=True), \
            _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "continue here",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    assert _row("sess-1")["instance_id"] == PEER
    assert p.submit.call_args.kwargs["no_kick"] is True


async def test_upload_paths_are_lifted_only_for_a_remote_turn(mock_db, monkeypatch):
    """A locally-executed turn's attachments are already on the disk the agent
    will read — lifting them would just duplicate bytes into the database."""
    _stub_installs(monkeypatch, [ME])
    database.create_session(session_id="sess-1", profile_id="default", title="t")

    with _Patches() as p:
        handler = _make_handler()
        handler._lift_uploads = AsyncMock(
            side_effect=AssertionError("must not lift for a local turn")
        )
        await handler.handle_query({
            "prompt": "look at this",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
            "upload_paths": ["/tmp/a.txt"],
        })

    assert "upload_blob_ids" not in (p.submit.call_args.kwargs.get("payload") or {})


async def test_remote_turn_attaches_lifted_blob_ids_to_the_turn(
    mock_db, monkeypatch, tmp_path
):
    """Placed remote → the staged files become durable blobs and their ids ride
    the turn payload to the executing install."""
    _stub_installs(monkeypatch, [ME, PEER])
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-1", name="P", description=None, path=str(path)
    )
    _peer_owned_session("sess-1", placement={"where": f"runner:{PEER}"})

    with _Patches() as p:
        handler = _make_handler()
        handler._lift_uploads = AsyncMock(return_value=["ub-1", "ub-2"])
        await handler.handle_query({
            "prompt": "look at this",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": "proj-1",
            "upload_paths": ["/tmp/a.txt", "/tmp/b.txt"],
        })

    handler._lift_uploads.assert_awaited_once()
    assert p.submit.call_args.kwargs["payload"]["upload_blob_ids"] == ["ub-1", "ub-2"]
    assert p.submit.call_args.kwargs["no_kick"] is True


async def test_a_failed_lift_refuses_the_send(mock_db, monkeypatch, tmp_path):
    """Never half-send: a turn whose prompt names files the executing install
    cannot see is exactly the silent loss D8 exists to prevent."""
    _stub_installs(monkeypatch, [ME, PEER])
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-1", name="P", description=None, path=str(path)
    )
    _peer_owned_session("sess-1", placement={"where": f"runner:{PEER}"})

    with _Patches() as p:
        handler = _make_handler()
        handler._lift_uploads = AsyncMock(
            side_effect=RuntimeError("outside the project directory")
        )
        await handler.handle_query({
            "prompt": "look at this",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": "proj-1",
            "upload_paths": ["/etc/passwd"],
        })

    assert "outside the project directory" in _sent(handler, "error")[0]["message"]
    p.submit.assert_not_called()


async def test_remote_turn_without_local_project_refuses_attachments(
    mock_db, monkeypatch
):
    from app.api.common import ATTACHMENTS_NEED_LOCAL_CHECKOUT

    _stub_installs(monkeypatch, [ME, PEER])
    _peer_owned_session("sess-1", placement={"where": f"runner:{PEER}"})

    with _Patches() as p:
        handler = _make_handler()
        handler._lift_uploads = AsyncMock(
            side_effect=AssertionError("must not lift without a local project")
        )
        await handler.handle_query({
            "prompt": "look at this",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
            "upload_paths": ["/tmp/a.txt"],
        })

    assert _sent(handler, "error")[0]["message"] == ATTACHMENTS_NEED_LOCAL_CHECKOUT
    p.submit.assert_not_called()


async def test_auto_adopt_clears_cli_sdk_without_jsonl(mock_db, monkeypatch):
    """Auto-adopt uses the same keep-predicate: a CLI id with no local JSONL
    is cleared so the next turn does not resume a missing conversation file."""
    _stub_installs(monkeypatch, [ME, PEER])
    _peer_owned_session("sess-1")
    with database.get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET sdk_session_id = %s WHERE id = %s",
            ("cli-uuid-1", "sess-1"),
        )

    with patch(
        "app.core.provider.fluid_transcript.is_fluid_session", return_value=False
    ), patch(
        "app.core.jsonl_parser.get_session_jsonl_path", return_value=None
    ), _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "continue here",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    assert _row("sess-1")["sdk_session_id"] in (None, "")


async def test_auto_adopt_keeps_fluid_sdk(mock_db, monkeypatch):
    """Fluid resume ids survive auto-adopt — they live in shared Postgres."""
    _stub_installs(monkeypatch, [ME, PEER])
    _peer_owned_session("sess-1")
    fluid_id = "ses_deadbeefcafe"
    with database.get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET sdk_session_id = %s WHERE id = %s",
            (fluid_id, "sess-1"),
        )

    with patch(
        "app.core.provider.fluid_transcript.is_fluid_session", return_value=True
    ), _Patches():
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "continue here",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": None,
        })

    assert _row("sess-1")["sdk_session_id"] == fluid_id


async def test_missing_local_checkout_blocks_auto_adopt(mock_db, monkeypatch):
    """A project session whose base checkout is not materialized here keeps
    running on its owner (adopting would spawn against a missing path)."""
    _stub_installs(monkeypatch, [ME, PEER])
    database.create_project(
        project_id="proj-1", name="P", description=None, path="/tmp/p"
    )
    _peer_owned_session("sess-1")
    with database.get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET project_id = %s WHERE id = %s", ("proj-1", "sess-1")
        )

    from app.core import project_resolver

    monkeypatch.setattr(
        project_resolver, "has_local_base_checkout", lambda *a, **k: False
    )

    with _Patches() as p:
        handler = _make_handler()
        await handler.handle_query({
            "prompt": "continue here",
            "session_id": "sess-1",
            "profile": "claude-code",
            "project": "proj-1",
        })

    assert _row("sess-1")["instance_id"] == PEER
    assert p.submit.call_args.kwargs["no_kick"] is True

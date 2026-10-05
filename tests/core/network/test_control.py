"""Session catalog mirrors, transcript tails, and driving a remote owner."""

import httpx
import pytest

from app.core import placement
from app.db import database
from app.db import network as store
from app.db import session_events as events_db
from app.network import catalog, control, engine, tokens, transport
from tests.core.network.harness import converge, join
from tests.core.network.test_transport import ServedBy


def _new_session(worker, session_id, title, *, target=None):
    with worker.active():
        database.create_session(session_id=session_id, profile_id="default", project_id=None, title=title,
                                is_active=True, target_instance_id=target)
        events_db.append_events(session_id, [
            {"type": "message.user", "payload": {"type": "message.user", "content": "hello from " + worker.name}},
            {"type": "message.assistant", "payload": {"type": "message.assistant", "content": "hi!", "kind": "text"}},
        ])
        engine.scan_all()


def test_catalog_mirrors_sessions_and_events_to_peers(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "s1", "First on A")
    with a.active():
        row = database.get_session("s1")
        assert row["owner_device_id"] == a.device_id
        published = [o for o in store.ops_since({}) if o["collection"] in ("sessions", "session_events")]
        assert {o["collection"] for o in published} == {"sessions", "session_events"}
        assert sorted(o["order_seq"] for o in published if o["collection"] == "session_events") == [1, 2]
    converge([a, b])
    with b.active():
        mirror = database.get_session("s1")
        assert mirror is not None and mirror["owner_device_id"] == a.device_id and mirror["title"] == "First on A"
        assert mirror.get("instance_id") in (None, "")
        events = events_db.get_events("s1", 0)
        assert [e["seq"] for e in events] == [1, 2] and events[0]["payload"]["content"] == "hello from a"
        assert events_db.get_max_seq("s1") == 2
        # The mirror is read-only: B's scan publishes nothing for A's session.
        assert engine.scan_all() == 0
        decision = placement.resolve_placement(mirror)
        assert decision.kind == "none"  # A is not "online" from B's point of view here
        assert control.owner_of(mirror) == a.device_id
        listing = database.get_installs_map()
        assert a.device_id in listing and listing[a.device_id]["device_name"] == "A"
    # New events on A flow as a tail.
    with a.active():
        events_db.append_events("s1", [{"type": "message.assistant", "payload": {"type": "message.assistant", "content": "more", "kind": "text"}}])
        assert catalog.publish_events() == 1
    converge([a, b])
    with b.active():
        assert events_db.get_max_seq("s1") == 3
    # Enrichment on B marks the session remote with A's name.
    with b.active():
        from app.api.sessions_helpers import _annotate_cluster_fields as enrich_sessions_with_device_info
        rows = [dict(database.get_session("s1"))]
        enrich_sessions_with_device_info(rows)
        assert rows[0]["is_remote"] and rows[0]["device_name"] == "A"


def test_last_owned_session_deletion_retracts_children_but_preserves_peer_records(workers):
    a, b = workers("last-owner"), workers("remaining-peer")
    with a.active():
        engine.create_network("Final session")
    join(a, b)
    for worker, sid in ((a, "local-last"), (b, "peer-kept")):
        with worker.active():
            database.create_session(session_id=sid, profile_id="default", project_id=None, title=sid)
            with store.get_db() as conn:
                cur = conn.cursor()
                cur.execute("INSERT INTO tags(id,name) VALUES (%s,%s)", (sid, sid))
                cur.execute("INSERT INTO session_tags(session_id,tag_id) VALUES (%s,%s)", (sid, sid))
                cur.execute("INSERT INTO pending_interactions(id,session_id,kind,payload) VALUES (%s,%s,%s,%s)",
                            (sid, sid, "question", '{"question":"test"}'))
            engine.scan_all(strict=True)
    converge([a, b])
    with a.active():
        database.delete_session("local-last")
        catalog.publish_owned()
        with store.get_db() as conn:
            cur = conn.cursor()
            for collection, local_id, peer_id in (
                    ("session_tags", "local-last|local-last", "peer-kept|peer-kept"),
                    ("pending_interactions", "local-last", "peer-kept")):
                assert store.get_record(cur, collection, local_id)["deleted"]
                assert not store.get_record(cur, collection, peer_id)["deleted"]
                assert store.get_record(cur, collection, peer_id)["origin_device_id"] == b.device_id
        assert catalog.publish_owned() == 0
    converge([a, b])
    with b.active():
        assert database.get_session("local-last") is None
        assert database.get_session("peer-kept") is not None
        with store.get_db() as conn:
            cur = conn.cursor()
            assert store.get_record(cur, "session_tags", "local-last|local-last")["deleted"]
            assert store.get_record(cur, "pending_interactions", "local-last")["deleted"]


def test_quiet_catalog_uses_one_query_and_finds_later_appends(workers):
    worker = workers("quiet-catalog")
    with worker.active():
        engine.create_network("Quiet catalog")
        for index in range(32):
            session_id = f"quiet-{index}"
            database.create_session(session_id=session_id, profile_id="default", project_id=None, title=session_id)
            events_db.append_events(session_id, [{"type": "message.user", "payload": {"content": "hello"}}])
        assert catalog.publish_events() == 32
        queries = []
        worker.raw.set_trace_callback(queries.append)
        try:
            assert catalog.publish_events() == 0
        finally:
            worker.raw.set_trace_callback(None)
        selects = [q for q in queries if q.lstrip().upper().startswith("SELECT")]
        assert len(selects) == 1
        # This is an actual append, not a fabricated dirty marker/cursor.
        events_db.append_events("quiet-31", [{"type": "message.user", "payload": {"content": "later"}}])
        assert catalog.publish_events() == 1
        assert catalog.publish_events() == 0
        # A reset cursor must remain eligible, even without events: PR #1240's
        # retraction path needs to see cleared transcripts on the next pass.
        with store.get_db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM network_catalog_cursors WHERE session_id = %s", ("quiet-31",))
            cur.execute("DELETE FROM session_events WHERE session_id = %s", ("quiet-31",))
            assert "quiet-31" in catalog._owned_session_ids(
                cur, worker.device_id, recent_only=True, include_held=True, unpublished_only=True)


@pytest.mark.parametrize("source, initial, prompt, expected", [
    ("worktree", "", "Fix remote session titles", "Fix remote session titles"),
    ("user", "   ", "Name this remote chat", "Name this remote chat"),
    ("user", "My custom title", "Do not rename me", "My custom title"),
    ("user", "", "   ", ""),
    ("wakeup", "", "Internal wake message", ""),
])
async def test_remote_first_turn_titles_owner_and_survives_catalog_sync(
    workers, monkeypatch, source, initial, prompt, expected,
):
    from app.core import turn_manager
    from app.db import turns

    a, b = workers("a"), workers("b")
    served = ServedBy(a, "http://a.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        await control.call(a.device_id, "sessions", {
            "session": {"id": "remote-title", "title": initial, "profile_id": "default"},
        })
        await control.submit_turn(a.device_id, "remote-title", source=source,
                                  prompt=prompt, profile_id="default")
    with a.active():
        assert database.get_session("remote-title")["title"] == expected
        assert turns.get_queued_turns("remote-title")[0]["prompt"] == prompt
        catalog.publish_owned()
    converge([a, b])
    with b.active():
        assert database.get_session("remote-title")["title"] == expected
        assert engine.scan_all() == 0  # the mirror cannot publish its own title


def test_placement_treats_own_device_id_as_me(workers):
    """A pin on our own device id resolves exactly like a pin on our instance id."""
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        from app.core.config import settings
        me = settings.effective_instance_id
        row = {"placement": {"where": f"runner:{a.device_id}"}, "instance_id": None, "owner_device_id": a.device_id}
        decision = placement.resolve_placement(row)
        assert (decision.kind, decision.runner_id) == ("runner", me)
        row = {"placement": None, "instance_id": None, "owner_device_id": a.device_id}
        decision = placement.resolve_placement(row)
        assert (decision.kind, decision.runner_id) == ("runner", me)
        row = {"placement": None, "instance_id": None, "owner_device_id": None}
        assert placement.resolve_placement(row).kind == "local"


async def test_remote_nonblocking_answer_is_durable_only_on_owner(workers, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core.user_question_handler import user_question_handler
    from app.db import turns, interactions
    a, b = workers("a"), workers("b")
    served = ServedBy(a, "http://a.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr("app.core.turn_manager._kick", lambda sid: None)
    monkeypatch.setattr("app.core.turn_claim.notify_turns_changed", lambda sid: None)
    monkeypatch.setattr("app.core.interaction_wait.notify_changed", lambda sid: None)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "s-question", "On A")
    with a.active():
        await user_question_handler.post_question(
            request_id="remote-q", tool_use_id="tu", session_id="s-question",
            questions=[{"question": "Color?"}], broadcast_func=AsyncMock(),
        )
        catalog.publish_owned()
    converge([a, b])
    with a.active():
        store.touch_member(b.device_id, endpoints=[])
    with b.active():
        assert user_question_handler.get_pending_questions("s-question")[0]["nonblocking"] is True
        store.touch_member(a.device_id, endpoints=[served.base_url])
        kwargs = dict(request_id="remote-q", session_id="s-question", answers={"Color?": "blue"},
                      answered_by_device=None, dismissed=False, skipped=None)
        assert (await control.answer_question(a.device_id, **kwargs))["success"] is True
        assert (await control.answer_question(a.device_id, **kwargs))["success"] is False
        assert turns.get_queued_turns("s-question") == []
        with pytest.raises(control.ControlRefused):
            await control.answer_question(a.device_id, **{**kwargs, "session_id": "not-owned"})
    with a.active():
        assert len(turns.get_queued_turns("s-question")) == 1
        assert interactions.get_answered_interactions("s-question")[0]["answered_by_device"] == b.device_id


async def test_control_reauthenticates_after_owner_token_reset(workers, monkeypatch):
    from unittest.mock import AsyncMock
    from app.core import turn_manager

    a, b = workers("token-owner"), workers("token-viewer")
    served = ServedBy(a, "http://a.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    interrupt = AsyncMock(return_value={"id": "turn_1"})
    monkeypatch.setattr(turn_manager, "interrupt_async", interrupt)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "s-token", "On A")
    converge([a, b])
    with b.active():
        link = transport.PeerLink(a.device_id, [served.base_url])
        transport._links[a.device_id] = link
        async with httpx.AsyncClient() as client:
            await link.authenticate(client, engine.require_context())
        # Real issuer invalidation, as on restart, while the cached token's
        # advertised expiry is still in the future. No fabricated 401 response.
        tokens.reset_for_tests()
        served.requests.clear()
        result = await control.interrupt(a.device_id, "s-token", "stop")
        assert result["interrupted"] is True
        assert served.requests == [
            ("POST", "/api/v1/network/peer/control/interrupt"),
            ("POST", "/api/v1/network/peer/auth/challenge"),
            ("POST", "/api/v1/network/peer/auth/token"),
            ("POST", "/api/v1/network/peer/control/interrupt"),
        ]
        interrupt.assert_awaited_once()


@pytest.mark.parametrize("failure, attempts, auth_calls", [
    (transport.PeerTokenRejected("token rejected"), 2, 2),
    (transport.PeerError("HTTP 403 forbidden"), 1, 1),
    (transport.PeerError("HTTP 500 failed"), 1, 1),
    (httpx.ReadTimeout("timeout"), 1, 1),
])
async def test_control_retry_is_bounded_and_only_for_rejected_tokens(monkeypatch, failure, attempts, auth_calls):
    from unittest.mock import AsyncMock

    link = transport.PeerLink("owner", ["http://a.local:8000"])
    link.authenticate = AsyncMock()
    link._request = AsyncMock(side_effect=failure)
    monkeypatch.setitem(transport._links, "owner", link)
    from types import SimpleNamespace
    monkeypatch.setattr(engine, "require_context", lambda: SimpleNamespace(member=lambda _device_id: None))
    with pytest.raises((control.RunnerUnreachable, control.ControlRefused)):
        await control.call("owner", "session-git", {"action": "get_git_status"})
    assert link._request.await_count == attempts
    assert link.authenticate.await_count == auth_calls


async def test_remote_owner_receives_turns_interrupts_and_answers(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = ServedBy(a, "http://a.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    submitted, interrupted, permissions, questions = [], [], [], []

    async def fake_submit(session_id, source, **kwargs):
        submitted.append((session_id, source, kwargs))
        return "turn_1"

    async def fake_interrupt(session_id, mode, stop_background=False, turn_id=None):
        interrupted.append((session_id, mode, stop_background, turn_id))
        return {"id": "turn_1"}

    async def fake_permission(**kwargs):
        permissions.append(kwargs)
        return {"ok": True}

    async def fake_question(**kwargs):
        questions.append(kwargs)
        return True

    from app.core import turn_manager
    from app.core import permission_handler as ph_mod
    from app.core import user_question_handler as uq_mod
    monkeypatch.setattr(turn_manager, "submit_async", fake_submit)
    monkeypatch.setattr(turn_manager, "interrupt_async", fake_interrupt)
    monkeypatch.setattr(ph_mod.permission_handler, "respond", fake_permission)
    monkeypatch.setattr(uq_mod.user_question_handler, "respond", fake_question)

    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "s1", "On A")
    converge([a, b])
    with a.active():
        store.touch_member(b.device_id, endpoints=[])
    with b.active():
        mirror = database.get_session("s1")
        owner = control.owner_of(mirror)
        assert owner == a.device_id
        # Presence tells B where A lives.
        store.touch_member(a.device_id, endpoints=["http://a.local:8000"])
        result = await control.submit_turn(owner, "s1", prompt="do it", profile_id="default")
        assert result["turn_id"] == "turn_1"
        assert submitted[0][0] == "s1" and submitted[0][2]["payload"]["forwarded_by_device"] == b.device_id
        result = await control.interrupt(owner, "s1", "stop", stop_background=False)
        assert result["interrupted"] and interrupted[0][1] == "stop"
        await control.answer_permission(owner, request_id="req1", session_id="s1", decision="allow",
                                        remember=None, pattern=None, answered_by_device="phone")
        # The answering device is the authenticated caller, never a claimed id.
        assert permissions[0]["answered_by_device"] == b.device_id and permissions[0]["decision"] == "allow"
        await control.answer_question(owner, request_id="q1", session_id="s1", answers={"Q": "A"},
                                      answered_by_device=None, dismissed=False, skipped=None)
        assert questions[0]["answered_by_device"] == b.device_id
        # Invalid profiles are refused by the peer API before inserting a row.
        with pytest.raises(control.ControlRefused, match="Unknown profile: missing-profile"):
            await control.create_session(owner, {"id": "s-invalid", "profile_id": "missing-profile"}, None)
        # A session created on B but owned by A is created on A too.
        result = await control.create_session(owner, {"id": "s2", "profile_id": "default", "title": "Born on B"},
                                              {"prompt": "first", "source": "user"})
        assert result["session_id"] == "s2" and result["turn"]["turn_id"] == "turn_1"
        # Sessions A does not own are refused.
        with pytest.raises(control.ControlRefused):
            await control.submit_turn(owner, "s-unknown", prompt="x")
        hydrated = await control.hydrate(owner, "s1")
        assert hydrated["published"] == 0  # already published
    with a.active():
        assert database.get_session("s-invalid") is None
        assert database.get_session("s2")["owner_device_id"] == a.device_id
        assert database.get_session("s2")["title"] == "Born on B"
    # Unreachable owner surfaces as RunnerUnreachable.
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://nowhere.local:9"])
        transport.reset_for_tests()
        with pytest.raises(control.RunnerUnreachable):
            await control.submit_turn(a.device_id, "s1", prompt="again")


@pytest.mark.parametrize("aware", [False, True], ids=["naive", "timezone-aware"])
async def test_create_remote_session_serializes_database_timestamps(workers, monkeypatch, aware):
    """Keep the DB producer and HTTP encoder; the SQLite shim returns date strings."""
    from datetime import datetime, timezone
    import json

    a, b = workers("a"), workers("b")
    served = ServedBy(a, "http://a.local:8000")
    real_async = httpx.AsyncClient
    sent = []

    def handle(request):
        if request.url.path.endswith("/control/sessions"):
            sent.append(json.loads(request.content))
        return served.handler(request)

    def async_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handle)
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        database.create_session(session_id="remote-dates", profile_id="default", project_id=None,
                                title="Remote branch", is_active=True, target_instance_id=a.device_id)
        database.set_session_branch("remote-dates", "fix/remote-dates", None)
        row = dict(database.get_session("remote-dates"))
        # PostgreSQL yields datetime objects for these actual database columns.
        for key in ("created_at", "updated_at"):
            value = row[key]
            timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
            row[key] = timestamp.replace(tzinfo=timezone.utc if aware else None)
        original = dict(row)
        result = await control.call(a.device_id, "sessions", {"session": row, "uploads": []})
        assert result["session_id"] == row["id"]
        assert row == original  # Encoding must not mutate the caller's database row.

    assert sent[0]["session"]["created_at"] == row["created_at"].isoformat()
    assert sent[0]["session"]["updated_at"] == row["updated_at"].isoformat()
    assert sent[0]["session"]["project_id"] is None
    assert sent[0]["uploads"] == []
    with a.active():
        received = database.get_session("remote-dates")
        assert received["owner_device_id"] == a.device_id
        assert received["title"] == "Remote branch"
        assert received["branch"] == "fix/remote-dates"


def test_fork_here_seeds_a_local_session_from_the_mirror(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    _new_session(a, "s1", "On A")
    converge([a, b])
    with b.active():
        result = control.fork_here("s1")
        forked = database.get_session(result["session_id"])
        assert forked["parent_session_id"] == "s1" and forked["owner_device_id"] == b.device_id
        assert forked["title"].endswith("(forked)") and result["context_chars"] > 0
        messages = database.get_session_messages(result["session_id"])
        assert messages and "hello from a" in messages[0]["content"]
        assert database.get_session("s1")["owner_device_id"] == a.device_id  # untouched
        with pytest.raises(LookupError):
            control.fork_here("nope")


def _served_client(monkeypatch, server):
    served = ServedBy(server, "http://a.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    return served


async def test_control_turns_whitelist_overrides_and_use_the_sessions_project(workers, monkeypatch):
    """NET-06: a member cannot move the owner's agent (``cwd``) or re-home the turn."""
    a, b = workers("a"), workers("b")
    served = _served_client(monkeypatch, a)
    submitted = []

    async def fake_submit(session_id, source, **kwargs):
        submitted.append(kwargs)
        return "turn_1"

    from app.core import turn_manager
    monkeypatch.setattr(turn_manager, "submit_async", fake_submit)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        database.create_project(project_id="proj-a", name="A's project", description=None, path="/srv/a")
        database.create_session(session_id="s-proj", profile_id="default", project_id="proj-a", title="On A",
                                is_active=True)
        engine.scan_all()
    converge([a, b])
    with a.active():
        store.touch_member(b.device_id, endpoints=[])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        await control.submit_turn(a.device_id, "s-proj", prompt="go", project_id="proj-elsewhere",
                                  overrides={"cwd": "/", "worktree_info": {"x": 1}, "model": "x"})
        assert submitted[-1]["overrides"] == {"model": "x"}
        assert submitted[-1]["project_id"] == "proj-a"
        await control.submit_turn(a.device_id, "s-proj", prompt="go", overrides={"cwd": "/", "permission_mode": "acceptEdits"})
        assert submitted[-1]["overrides"] == {"permission_mode": "acceptEdits"}
        await control.submit_turn(a.device_id, "s-proj", prompt="go", overrides={"cwd": "/"})
        assert submitted[-1]["overrides"] is None
        await control.submit_turn(a.device_id, "s-proj", prompt="go", overrides={"effort": "high"})
        assert submitted[-1]["overrides"] == {"effort": "high"}
        # An effort this worker does not know is dropped, never refused.
        await control.submit_turn(a.device_id, "s-proj", prompt="go", overrides={"effort": "turbo"})
        assert submitted[-1]["overrides"] is None
        with pytest.raises(control.ControlRefused):
            await control.submit_turn(a.device_id, "s-proj", prompt="go", overrides={"permission_mode": "plan-evil"})
    assert len(submitted) == 5


async def test_control_turns_carry_the_context_window_to_the_owner(workers, monkeypatch):
    """A window picked mid-chat on a session another worker runs reaches that
    worker's session row, which every later turn of the session (a restart, a
    wake, a limit-reset resume) and its subagents read. It used to be dropped
    here, so the owner kept the window the session was created with and a
    Claude leg created at 1M stayed at 1M whatever the user picked after."""
    a, b = workers("a"), workers("b")
    served = _served_client(monkeypatch, a)
    from app.core import turn_manager
    real_submit = turn_manager.submit_async

    async def submit_without_running(session_id, source, **kwargs):
        return await real_submit(session_id, source, no_kick=True, **kwargs)

    monkeypatch.setattr(turn_manager, "submit_async", submit_without_running)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        database.create_session(session_id="s-window", profile_id="default", project_id=None, title="On A",
                                is_active=True, model="opus", context_window=1_000_000)
        engine.scan_all()
    converge([a, b])
    with a.active():
        store.touch_member(b.device_id, endpoints=[])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        await control.submit_turn(a.device_id, "s-window", prompt="go",
                                  overrides={"model": "opus", "context_window": 200_000})
        for bad in (0, -1, "200000", True, 10**9):
            with pytest.raises(control.ControlRefused):
                await control.submit_turn(a.device_id, "s-window", prompt="go",
                                          overrides={"context_window": bad})
    with a.active():
        assert database.get_session("s-window")["context_window"] == 200_000
        from app.db import turns as turns_db
        assert turns_db.get_latest_turn("s-window")["overrides"]["context_window"] == 200_000


async def test_control_turns_steers_a_message_into_the_owners_live_turn(workers, monkeypatch):
    """#1786: a send forwarded from another device while the owner's turn runs
    is steered in at the next tool boundary, like a local send, instead of
    waiting for the turn to end (or for the owner to press Interrupt)."""
    a, b = workers("a"), workers("b")
    served = _served_client(monkeypatch, a)
    order: list = []
    live = {"s-steer": None}

    async def fake_submit(session_id, source, **kwargs):
        order.append(("submit", session_id))
        return "turn_q"

    async def fake_steer(session_id):
        order.append(("steer", session_id))
        return 1

    from app.core import session_runtime, turn_manager
    from app.db import turns as turns_db
    monkeypatch.setattr(turn_manager, "submit_async", fake_submit)
    monkeypatch.setattr(session_runtime, "try_steer_pending", fake_steer)
    monkeypatch.setattr(turns_db, "get_live_turn", lambda sid: live.get(sid))
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        database.create_session(session_id="s-steer", profile_id="default", project_id=None,
                                title="On A", is_active=True)
        engine.scan_all()
    converge([a, b])
    with a.active():
        store.touch_member(b.device_id, endpoints=[])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        # Idle owner: the send starts its own turn; nothing to steer into.
        await control.submit_turn(a.device_id, "s-steer", prompt="first")
        assert order == [("submit", "s-steer")]
        # A turn is live on the owner: the queued row is handed to it.
        live["s-steer"] = {"id": "turn_live", "state": "running"}
        await control.submit_turn(a.device_id, "s-steer", prompt="mid-turn remark")
        assert order[1:] == [("submit", "s-steer"), ("steer", "s-steer")]


class _SteerAdapter:
    def __init__(self):
        self.calls = []

    async def steer(self, text, *, id=None, origin="user"):
        self.calls.append((text, id))


async def _forward_into_live_turn(workers, monkeypatch, session_fields, live_turn, *, prompt,
                                  profile_id, overrides):
    """Start ``live_turn`` on owner A's session, then send ``prompt`` to it from
    member B through the real control route, with the real submit and steer.
    Returns ``(steered calls, queued row)``."""
    from unittest.mock import MagicMock, patch

    from app.core import session_runtime, turn_manager
    from app.db import turns as turns_db

    a, b = workers("a"), workers("b")
    served = _served_client(monkeypatch, a)
    monkeypatch.setattr(turn_manager, "_kick", lambda session_id: None)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    session_id = session_fields["session_id"]
    with a.active():
        database.create_session(project_id=None, is_active=True, **session_fields)
        engine.scan_all()
    converge([a, b])
    with a.active():
        store.touch_member(b.device_id, endpoints=[])
        turn = turns_db.enqueue_turn(session_id, live_turn.pop("source"), prompt="the live one", **live_turn)
        turns_db.start_turn(turn["id"])
        live = turns_db.get_turn(turn["id"])
    adapter = _SteerAdapter()
    pool = MagicMock(name="session_pool")
    pool.peek.return_value = MagicMock(adapter=adapter)
    session_runtime._live_runtimes[session_id] = session_runtime.SessionRuntime(live)
    try:
        with b.active(), patch("app.core.session_pool.get_session_pool", return_value=pool):
            store.touch_member(a.device_id, endpoints=[served.base_url])
            result = await control.submit_turn(a.device_id, session_id, prompt=prompt,
                                               profile_id=profile_id, overrides=overrides)
    finally:
        session_runtime._live_runtimes.pop(session_id, None)
    with a.active():
        row = turns_db.get_turn(result["turn_id"])
    return adapter.calls, row, live


async def test_control_turns_steer_an_owner_message_into_a_running_subagent(workers, monkeypatch):
    """The owner's phone is on member B; the Task worker runs on owner A. The
    forwarded message carries B's composer (another agent, a model, an effort
    and no permission mode, and the route itself keeps only some of those). It
    must take the worker's binding on A and steer into the worker's turn."""
    from app.core import provider_agent

    overrides = provider_agent.worker_turn_overrides(
        provider_id=None, model_id="opus", effort="high", cwd="/work/task-checkout",
        permission_mode="bypassPermissions", system_prompt_append="You are a coding subagent.",
    )
    calls, row, live = await _forward_into_live_turn(
        workers, monkeypatch,
        {"session_id": "s-worker", "profile_id": "default", "title": "Task worker",
         "model": "opus", "effort": "high", "hidden": True, "is_worker": True},
        {"source": provider_agent.WORKER_TURN_SOURCE, "profile_id": "general-purpose",
         "overrides": overrides},
        prompt="also check the tests", profile_id="coding",
        overrides={"model": "sonnet", "effort": "low", "fast": False, "workspace_project_ids": []},
    )

    assert calls == [("also check the tests", row["id"])]
    assert row["profile_id"] == "general-purpose"
    assert row["overrides"] == live["overrides"]


async def test_control_turns_steer_into_a_main_chat_turn_that_carries_an_effort(workers, monkeypatch):
    """A main chat whose running turn was sent with an effort (from the owner's
    own UI, or a wake that replays it): the same picks sent from another device
    must steer in, not wait the turn out because the route dropped the effort."""
    calls, row, _live = await _forward_into_live_turn(
        workers, monkeypatch,
        {"session_id": "s-main", "profile_id": "default", "title": "On A", "model": "opus",
         "effort": "high"},
        {"source": "user", "profile_id": "default",
         "overrides": {"model": "opus", "effort": "high", "permission_mode": "default", "fast": False}},
        prompt="mid-turn remark", profile_id="default",
        overrides={"model": "opus", "effort": "high", "permission_mode": "default", "fast": False,
                   "workspace_project_ids": []},
    )

    assert calls == [("mid-turn remark", row["id"])]
    assert row["overrides"]["effort"] == "high"


async def test_control_session_creation_validates_its_fields(workers, monkeypatch):
    """NET-24: api_user_id, hidden, permission_mode and branch are not taken on faith."""
    a, b = workers("a"), workers("b")
    served = _served_client(monkeypatch, a)
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        base = {"id": "s-new", "profile_id": "default", "title": "Born on B", "api_user_id": "u-b",
                "hidden": "yes", "permission_mode": "acceptEdits"}
        for branch in ("../etc", "-x", "a b", "feature/..", "a//b", "refs/heads/x.lock", "a\tb"):
            with pytest.raises(control.ControlRefused):
                await control.create_session(a.device_id, {**base, "branch": branch}, None)
        with pytest.raises(control.ControlRefused):
            await control.create_session(a.device_id, {**base, "permission_mode": "plan-evil"}, None)
        result = await control.create_session(a.device_id, {**base, "branch": "fix/remote"}, None)
        assert result["session_id"] == "s-new"
        # Unknown actions are 404; a KeyError raised inside a handler is not masked as one.
        with pytest.raises(control.ControlRefused, match="Unknown control action"):
            await control.call(a.device_id, "nope", {})

        async def broken(body, from_device):
            raise KeyError("request_id")

        monkeypatch.setitem(control._HANDLERS, "broken", broken)
        with pytest.raises(control.ControlRefused, match="Missing field: request_id"):
            await control.call(a.device_id, "broken", {})
    with a.active():
        row = database.get_session("s-new")
        assert row["api_user_id"] is None
        assert row["hidden"] in (True, 1)
        assert row["permission_mode"] == "acceptEdits" and row["branch"] == "fix/remote"
        assert row["owner_device_id"] == a.device_id


def test_cleared_transcript_republishes_and_mirrors_replace_not_freeze(workers):
    """NET-07: clearing on the owner resets the catalog cursor; mirrors converge on the new transcript."""
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    with a.active():
        database.create_session(session_id="s-clear", profile_id="default", project_id=None, title="Clear me",
                                is_active=True)
        events_db.append_events("s-clear", [
            {"type": "message.user", "payload": {"type": "message.user", "content": f"old {i}"}} for i in range(5)])
        engine.scan_all()
    converge([a, b])
    with b.active():
        assert [e["payload"]["content"] for e in events_db.get_events("s-clear", 0)] == [f"old {i}" for i in range(5)]
    with a.active():
        database.clear_session_conversation("s-clear")
        with store.get_db() as conn:
            assert store.catalog_cursor(conn.cursor(), "s-clear") == 0
        events_db.append_events("s-clear", [
            {"type": "message.user", "payload": {"type": "message.user", "content": "new 0"}},
            {"type": "message.assistant", "payload": {"type": "message.assistant", "content": "new 1", "kind": "text"}}])
        assert [e["seq"] for e in events_db.get_events("s-clear", 0)] == [1, 2]
        assert catalog.publish_events() > 0
    converge([a, b])
    with b.active():
        events = events_db.get_events("s-clear", 0)
        assert [(e["seq"], e["payload"]["content"]) for e in events] == [(1, "new 0"), (2, "new 1")]
        assert events_db.get_max_seq("s-clear") == 2
    # The next tail continues from the new counter on both sides.
    with a.active():
        events_db.append_events("s-clear", [{"type": "message.user", "payload": {"type": "message.user", "content": "new 2"}}])
        assert catalog.publish_events() == 1
    converge([a, b])
    with b.active():
        assert [e["payload"]["content"] for e in events_db.get_events("s-clear", 0)] == ["new 0", "new 1", "new 2"]
        assert events_db.get_max_seq("s-clear") == 3


def test_oversized_event_is_skipped_without_freezing_later_sessions(workers):
    """NET-12: one event over the envelope cap advances the cursor and the loop goes on."""
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        for sid in ("s-big", "s-after"):
            database.create_session(session_id=sid, profile_id="default", project_id=None, title=sid, is_active=True)
        events_db.append_events("s-big", [
            {"type": "message.user", "payload": {"type": "message.user", "content": "small"}},
            {"type": "message.assistant", "payload": {"type": "message.assistant", "content": "x" * (5 * 1024 * 1024), "kind": "text"}},
            {"type": "message.user", "payload": {"type": "message.user", "content": "after"}}])
        events_db.append_events("s-after", [{"type": "message.user", "payload": {"type": "message.user", "content": "later"}}])
        assert catalog.publish_events() == 3
        with store.get_db() as conn:
            assert store.catalog_cursor(conn.cursor(), "s-big") == 3
            assert store.catalog_cursor(conn.cursor(), "s-after") == 1
        published = sorted((o["record_id"], o["order_seq"]) for o in store.ops_since({}) if o["collection"] == "session_events")
        assert published == [("s-after|1", 1), ("s-big|1", 1), ("s-big|3", 3)]
        assert catalog.publish_events() == 0  # nothing left behind, nothing retried


def test_fork_context_uses_the_latest_events(workers):
    """NET-16: a long session forks with its recent turns, in order."""
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_session(session_id="s-long", profile_id="default", project_id=None, title="Long", is_active=True)
        events_db.append_events("s-long", [
            {"type": "message.user", "payload": {"type": "message.user", "content": f"msg {i:03d}"}} for i in range(500)])
        text = catalog.transcript_text("s-long")
        lines = text.split("\n\n")
        assert len(lines) == 400
        assert lines[0] == "User: msg 100" and lines[-1] == "User: msg 499"
        assert "msg 099" not in text
        assert lines == sorted(lines)

async def test_existing_remote_session_attachments_reach_owner(workers, monkeypatch, tmp_path):
    """Independent DBs: stage/lift on viewer, authenticated transfer, owner materialization."""
    from app.core import turn_manager, upload_transfer
    from app.core.confined_files import upload
    import app.core.config as config
    from app.db import projects_v2, upload_blobs

    a, b = workers("upload-owner"), workers("upload-viewer")
    served = ServedBy(a, "http://a.local:8000")
    real_async = httpx.AsyncClient

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    with a.active():
        engine.create_network("Uploads")
    join(a, b)
    with a.active():
        project = projects_v2.upsert_project("https://github.com/example/uploads.git")
        database.create_session(session_id="upload-session", profile_id="default", title="Uploads",
                                project_id=None, project_v2_id=project["id"])
        catalog.publish_owned()
    converge([a, b])
    landed = []

    async def receive(session_id, source, **kwargs):
        blob_ids = kwargs["payload"]["upload_blob_ids"]
        assert upload_blobs.get_blob_data(blob_ids[0]) == b"worker bytes"
        owner_dir = tmp_path / "owner-checkout"
        owner_dir.mkdir()
        mapping = upload_transfer.materialize_blobs_for_turn(blob_ids, str(owner_dir))
        landed.append(upload_transfer.rewrite_prompt(kwargs["prompt"], mapping))
        return "turn-with-file"

    monkeypatch.setattr(turn_manager, "submit_async", receive)
    with b.active():
        row = dict(database.get_session("upload-session"))
        assert row["project_id"] is None  # no local checkout on viewer
        stage = upload_transfer.session_upload_dir(row["id"]).relative_to(tmp_path).as_posix()
        path = upload(tmp_path, stage, "notes.txt", b"worker bytes")
        ids = upload_transfer.lift_session_paths_to_blobs([path.as_posix()], row)
        store.touch_member(a.device_id, endpoints=[served.base_url])
        result = await control.submit_turn(a.device_id, row["id"], prompt=f"Read @{path.as_posix()}",
                                           payload={"upload_blob_ids": ids})
        assert result["turn_id"] == "turn-with-file"
        assert upload_blobs.get_blob_data(ids[0]) == b"worker bytes"  # independent sender DB
    assert (tmp_path / "owner-checkout" / "uploads" / "notes.txt").read_bytes() == b"worker bytes"
    assert landed == [f"Read @{tmp_path / 'owner-checkout' / 'uploads' / 'notes.txt'}"]


async def test_remote_turn_refuses_missing_upload_bytes_before_enqueue(workers, monkeypatch):
    from app.core import turn_manager
    from unittest.mock import AsyncMock
    a = workers("upload-validation")
    with a.active():
        engine.create_network("Uploads")
        database.create_session(session_id="upload-session", profile_id="default", title="Uploads")
        submit = AsyncMock()
        monkeypatch.setattr(turn_manager, "submit_async", submit)
        with pytest.raises(ValueError, match="Invalid upload transfer"):
            await control.handle("turns", {"session_id": "upload-session", "prompt": "file",
                                           "payload": {"upload_blob_ids": ["missing"]}}, from_device="peer")
        submit.assert_not_awaited()

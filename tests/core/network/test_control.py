"""Session catalog mirrors, transcript tails, and driving a remote owner."""

import httpx
import pytest

from app.core import placement
from app.db import database
from app.db import network as store
from app.db import session_events as events_db
from app.network import catalog, control, engine, tokens, transport
from tests.core.network.harness import Worker, converge, join
from tests.core.network.test_transport import ServedBy


@pytest.fixture
def workers(tmp_path):
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


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
        assert permissions[0]["answered_by_device"] == "phone" and permissions[0]["decision"] == "allow"
        await control.answer_question(owner, request_id="q1", session_id="s1", answers={"Q": "A"},
                                      answered_by_device=None, dismissed=False, skipped=None)
        assert questions[0]["answered_by_device"] == b.device_id
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
